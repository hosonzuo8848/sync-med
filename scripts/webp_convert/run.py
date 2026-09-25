# -*- coding: utf-8 -*-
"""Cloud PDF/JPG -> page_NNNN.webp pipeline for 123pan (MVP).

  python run.py token   --forge DIR --books SPEC --target main
  python run.py run     --forge DIR --books SPEC --target main --shard 0 --shards 1 ...
  python run.py summary --forge DIR --books SPEC
  python run.py --selftest          (stubbed 123 + download, no network)

Book list CSV lives in the PRIVATE repo (data/webp/*.csv), never in this repo:
  book_id,src_type,src_acct,src_path,src_file_id,dst_path,title
  src_type  pdf | jpg   (jpg: src_path is a folder of jpg/jpeg/png/tif images)
  src_acct  main | guji (the account holding the source; target comes from --target)
  dst_path  full 123 folder path of the volume folder; its name starts with book_id

Shared token: 123 allows at most 3 live tokens per account and other jobs
(site, pan-register, inventory) hold some. So only `token` may log in; it keeps
ONE token per account in the private repo (data/webp/tok/<acct>.enc, Fernet,
key derived from that account's client secret), reuses it while it has > 8h
left and passes a cheap user/info check, and logs in again only otherwise.
Shards read that file from their checkout and never log in; on 401 they stop.

Checkpoint: per-book ledger rows go to data/webp/ledger/ in the private repo
(book_id + numbers only). Rows with status=ok are skipped by later runs; inside
a book, pages already present in the target folder are not converted again.

Logs print counters only: no titles, no paths, no book ids, no tokens.
"""
import argparse
import base64
import csv
import glob
import hashlib
import io
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))          # scripts/ (gh_issue)
import pan as panmod                               # noqa: E402

# Same render parameters as the local v8 engine, so cloud pages match the
# pages already on the site: fitz 120 dpi, clamp to WebP max side, q80 m0.
PDF_DPI, WEBP_Q, WEBP_METHOD, WEBP_MAX = 120, 80, 0, 16383
PAGE_RE = re.compile(r"^page_(\d{4,})\.webp$")
IMG_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
CRED_ENV = {"main": ("PAN_CID_MAIN", "PAN_SEC_MAIN"), "guji": ("PAN_CID_GUJI", "PAN_SEC_GUJI")}
TOK_PATH = "data/webp/tok/%s.enc"
LEDGER_DIR = "data/webp/ledger"
STATS_DIR = "data/webp/stats"
MIN_TOKEN_LIFE = 8 * 3600
LEDGER_COLS = ["book_id", "status", "pages_expected", "pages_uploaded", "pages_reused",
               "pages_present", "dst_dir_id", "seconds", "attempts", "err"]
ISSUE_TITLE = "\u4e91\u7aef\u8f6c\u56fe\u8fdb\u5ea6"   # progress issue title (zh)


class SrcMissing(Exception):
    pass


class Stopped(Exception):
    pass


def page_name(i):
    return "page_%04d.webp" % i


def natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------- private repo
class Forge:
    """Private repo. Reads come from the local checkout. Writes go to the local
    checkout and, when repo+token are set, to GitHub through the contents API.
    Each shard writes its own file, so shards never conflict with each other."""

    def __init__(self, root, repo="", token=""):
        self.root, self.repo, self.token = root, repo, token
        self.sha = {}

    def path(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def read(self, rel):
        p = self.path(rel)
        return open(p, "rb").read() if os.path.exists(p) else None

    def _api(self, method, rel, body=None):
        import requests
        return requests.request(
            method, "https://api.github.com/repos/%s/contents/%s" % (self.repo, urllib.parse.quote(rel)),
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json"}, json=body, timeout=60)

    def fetch(self, rel):
        """Freshest copy straight from GitHub (the token broker needs the latest)."""
        if not self.repo:
            return self.read(rel)
        r = self._api("GET", rel)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        j = r.json()
        self.sha[rel] = j["sha"]
        return base64.b64decode(j["content"])

    def write(self, rel, data, msg):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        if not self.repo:
            return True
        status = None
        for i in range(5):
            body = {"message": msg, "content": base64.b64encode(data).decode()}
            if rel in self.sha:
                body["sha"] = self.sha[rel]
            r = self._api("PUT", rel, body)
            status = r.status_code
            if status in (200, 201):
                self.sha[rel] = r.json()["content"]["sha"]
                return True
            if status in (409, 422):                 # branch moved / stale sha: refresh, retry
                g = self._api("GET", rel)
                if g.status_code == 200:
                    self.sha[rel] = g.json()["sha"]
            time.sleep(2 * (i + 1))
        print("forge write failed http=%s" % status, flush=True)
        return False


def forge_of(a):
    return Forge(a.forge, os.environ.get("FORGE_REPO", ""), os.environ.get("FORGE_TOKEN", ""))


# ---------------------------------------------------------------- token broker
def _fernet(secret):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("webp-tok:" + secret).encode()).digest()))


def creds(acct):
    return tuple(os.environ.get(n, "") for n in CRED_ENV[acct])


def read_token(forge, acct, fresh=False):
    sec = creds(acct)[1]
    blob = forge.fetch(TOK_PATH % acct) if fresh else forge.read(TOK_PATH % acct)
    if not blob or not sec:
        return None
    try:
        return json.loads(_fernet(sec).decrypt(blob))
    except Exception:                                   # noqa: BLE001  wrong key / corrupt
        return None


def seconds_left(tok):
    try:
        t = datetime.fromisoformat(str(tok.get("exp", "")).replace("Z", "+00:00"))
        if t.tzinfo is None:                            # 123 speaks Beijing time
            t = t.replace(tzinfo=timezone(timedelta(hours=8)))
        return (t - datetime.now(timezone.utc)).total_seconds()
    except Exception:                                   # noqa: BLE001
        return 0


def load_books(forge_root, spec):
    spec = (spec or "").strip()
    if ".." in spec:
        sys.exit("bad books spec")
    if spec.endswith(".csv"):
        files, ids = [os.path.join(forge_root, *spec.split("/"))], None
    else:
        files = sorted(glob.glob(os.path.join(forge_root, "data", "webp", "*.csv")))
        ids = {x.strip() for x in spec.split(",") if x.strip()}
    rows, seen = [], set()
    for fp in files:
        with open(fp, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                b = (r.get("book_id") or "").strip()
                if not b or b in seen or (ids is not None and b not in ids):
                    continue
                seen.add(b)
                rows.append(r)
    if ids is not None and len(rows) < len(ids):
        print("book ids not found in any list: %d" % (len(ids) - len(rows)), flush=True)
    rows.sort(key=lambda r: r["book_id"])
    return rows


def accounts_needed(books, target):
    return {target} | {(b.get("src_acct") or "guji").strip() for b in books}


def cmd_token(a):
    forge = forge_of(a)
    books = load_books(a.forge, a.books)
    for acct in sorted(accounts_needed(books, a.target)):
        cid, sec = creds(acct)
        if not (cid and sec):
            sys.exit("token: credentials missing for acct=%s" % acct)
        tok, state = read_token(forge, acct, fresh=True), "missing"
        if tok and seconds_left(tok) > MIN_TOKEN_LIFE:
            try:
                PAN_FACTORY(tok["token"], {"misc": 1}).user_info()
                state = "reused"
            except panmod.TokenInvalid:
                state = "revoked"
            except panmod.PanError:
                state = "reused"                        # 123 hiccup: keep it, shards will tell
        elif tok:
            state = "expiring"
        if state != "reused":
            t, exp = LOGIN(cid, sec)
            blob = _fernet(sec).encrypt(json.dumps({"token": t, "exp": exp,
                                                    "at": int(time.time())}).encode())
            if not forge.write(TOK_PATH % acct, blob, "webp: refresh %s token" % acct):
                sys.exit("token: could not store the shared token")
            state = "refreshed(was %s)" % state
        print("token acct=%s %s" % (acct, state), flush=True)


# ---------------------------------------------------------------- conversion
def to_webp(img):
    from PIL import Image
    if img.mode != "RGB":
        img = img.convert("RGB")
    if max(img.size) > WEBP_MAX:
        r = WEBP_MAX / max(img.size)
        img = img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "webp", quality=WEBP_Q, method=WEBP_METHOD)
    return buf.getvalue()


def pdf_page_webp(doc, idx0):
    from PIL import Image
    pix = doc[idx0].get_pixmap(dpi=PDF_DPI)
    return to_webp(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))


def direct_url(acct, path):
    """Signed URL on the purchased 123 direct-link space (zero API calls).
    Same signing rule as the site's pan123.js signDirectLink. Unconfigured -> None."""
    uid = os.environ.get("PAN_UID_" + acct.upper(), "")
    key = os.environ.get("PAN_DIRECT_KEY_" + acct.upper(), "")
    if not (uid and key and path):
        return None
    p = "/" + uid + "/" + path.lstrip("/")
    exp, rnd = int(time.time()) + 900, random.randint(0, 999999)
    h = hashlib.md5(("%s-%d-%d-%s-%s" % (p, exp, rnd, uid, key)).encode()).hexdigest()
    return "https://%s.cdn.123clouddisk.com%s?auth_key=%d-%d-%s-%s" % (
        uid, urllib.parse.quote(p), exp, rnd, uid, h)


def http_download(url, dest):
    import requests
    try:
        with requests.get(url, stream=True, timeout=(20, 300)) as r:
            if r.status_code != 200:
                return False
            with open(dest, "wb") as f:
                for ch in r.iter_content(1 << 20):
                    f.write(ch)
        return os.path.getsize(dest) > 0
    except Exception:                                   # noqa: BLE001
        return False


# test hooks (selftest swaps these for stubs)
PAN_FACTORY = panmod.Pan
LOGIN = panmod.login
DOWNLOAD = http_download


class Ctx:
    def __init__(self, a, pans):
        self.a, self.pans = a, pans
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.counts = {"src_direct": 0, "src_api": 0}
        self.direct_fail = {}

    def bump(self, k):
        with self.lock:
            self.counts[k] = self.counts.get(k, 0) + 1

    def fetch(self, acct, path, file_id, dest):
        pan = self.pans[acct]
        if self.direct_fail.get(acct, 0) < 3:
            u = direct_url(acct, path)
            if u:
                if DOWNLOAD(u, dest):
                    self.bump("src_direct")
                    return
                with self.lock:                         # 3 misses: stop trying direct
                    self.direct_fail[acct] = self.direct_fail.get(acct, 0) + 1
        if not file_id:
            file_id, _ = pan.find_file(path)
            if not file_id:
                raise SrcMissing()
        u = pan.download_url(file_id)
        if not (u and DOWNLOAD(u, dest)):
            raise IOError("download")
        self.bump("src_api")


def list_images(pan, folder_path):
    fid = pan.walk(folder_path)
    if fid is None:
        return []
    return sorted([f for f in pan.list_all(fid)
                   if f["type"] == 0 and f["name"].lower().endswith(IMG_EXT)],
                  key=lambda f: natkey(f["name"]))


def _do_book(ctx, row, rec):
    a = ctx.a
    tgt = ctx.pans[a.target]
    src_acct = (row.get("src_acct") or "guji").strip()
    src = ctx.pans[src_acct]
    stype = (row.get("src_type") or "pdf").strip().lower()
    dst_id = tgt.walk(row["dst_path"], create=not a.dry_run)
    existing = set()
    if dst_id is not None:
        rec["dst_dir_id"] = dst_id
        existing = {f["name"] for f in tgt.list_all(dst_id, fresh=True)
                    if f["type"] == 0 and PAGE_RE.match(f["name"])}
    if a.dry_run:                                       # read-only plan: no download, no write
        if stype == "pdf":
            ok = row.get("src_file_id") or src.find_file(row["src_path"])[0]
        else:
            ok = list_images(src, row["src_path"])
        if not ok:
            raise SrcMissing()
        rec.update(status="dry", pages_present=len(existing))
        return
    with tempfile.TemporaryDirectory() as td:
        doc = None
        if stype == "pdf":
            pdf = os.path.join(td, "src.pdf")
            fid = int(row["src_file_id"]) if (row.get("src_file_id") or "").strip() else None
            ctx.fetch(src_acct, row["src_path"], fid, pdf)
            import fitz
            doc = fitz.open(pdf)
            total = doc.page_count

            def make(i):
                return pdf_page_webp(doc, i - 1)
        else:
            imgs = list_images(src, row["src_path"])
            if not imgs:
                raise SrcMissing()
            total = len(imgs)

            def make(i):
                from PIL import Image
                p = os.path.join(td, "img")
                f = imgs[i - 1]
                ctx.fetch(src_acct, row["src_path"].rstrip("/") + "/" + f["name"], f["id"], p)
                with Image.open(p) as im:
                    return to_webp(im)
        try:
            want = min(total, a.limit_pages) if a.limit_pages else total
            rec["pages_expected"] = want
            for i in range(1, want + 1):
                if ctx.stop.is_set():
                    raise Stopped()
                if page_name(i) in existing:
                    continue
                how = tgt.upload(dst_id, page_name(i), make(i), try_reuse=not a.no_reuse)
                rec["pages_reused" if how == "reuse" else "pages_uploaded"] += 1
        finally:
            if doc is not None:
                doc.close()
    present = {f["name"] for f in tgt.list_all(dst_id, fresh=True) if f["type"] == 0}
    rec["pages_present"] = sum(1 for i in range(1, want + 1) if page_name(i) in present)
    if rec["pages_present"] != want:
        raise IOError("verify")
    rec["status"] = "ok" if want == total else "limit"


def process_book(ctx, row):
    t0 = time.time()
    rec = dict.fromkeys(LEDGER_COLS, 0)
    rec.update(book_id=row["book_id"], status="fail", dst_dir_id="", err="")
    for attempt in range(1, 4):
        rec["attempts"] = attempt
        try:
            _do_book(ctx, row, rec)
            rec["err"] = ""
            break
        except panmod.TokenInvalid:
            rec.update(status="fail", err="token")
            ctx.stop.set()
            break
        except Stopped:
            rec.update(status="fail", err="stopped")
            break
        except SrcMissing:
            rec.update(status="fail", err="src_missing")
            break
        except panmod.PanError as e:
            rec.update(status="fail", err="pan_%s" % e.code)
        except Exception as e:                          # noqa: BLE001
            rec.update(status="fail", err=(str(e) if isinstance(e, IOError) and str(e) in
                                           ("download", "verify") else type(e).__name__)[:30])
        if attempt < 3:
            time.sleep(RETRY_SLEEP * attempt)
    rec["seconds"] = int(time.time() - t0)
    return rec


RETRY_SLEEP = 5


class Ledger:
    def __init__(self, forge, rel, every=600):
        self.forge, self.rel, self.every = forge, rel, every
        self.rows, self.last, self.ok = [], time.time(), True

    def add(self, rec):
        self.rows.append(rec)
        if time.time() - self.last > self.every:
            self.flush()

    def flush(self):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=LEDGER_COLS, lineterminator="\n")
        w.writeheader()
        w.writerows(self.rows)
        self.ok = self.forge.write(self.rel, buf.getvalue().encode(), "webp: ledger") and self.ok
        self.last = time.time()


def done_ids(forge_root):
    ok = set()
    for fp in glob.glob(os.path.join(forge_root, *LEDGER_DIR.split("/"), "*.csv")):
        with open(fp, encoding="utf-8", newline="") as f:
            ok |= {r["book_id"] for r in csv.DictReader(f) if r.get("status") == "ok"}
    return ok


def cmd_run(a):
    t_start = time.time()
    forge = forge_of(a)
    books = load_books(a.forge, a.books)
    mine = [b for i, b in enumerate(books) if i % a.shards == a.shard]
    done = set() if a.force else done_ids(a.forge)
    pending = [b for b in mine if b["book_id"] not in done]
    todo = pending[:a.max_books] if a.max_books else pending
    print("shard %d/%d listed=%d mine=%d skip_done=%d todo=%d dry=%d limit_pages=%d"
          % (a.shard, a.shards, len(books), len(mine), len(mine) - len(pending), len(todo),
             a.dry_run, a.limit_pages), flush=True)
    qps = {k: v / a.shards for k, v in
           (("list", a.list_qps), ("dl", a.dl_qps), ("up", a.up_qps), ("misc", a.misc_qps))}
    pans = {}
    for acct in accounts_needed(todo, a.target):
        tok = read_token(forge, acct)
        if not tok:
            sys.exit("no shared token for acct=%s: run the token step first" % acct)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::add-mask::" + tok["token"], flush=True)
        pans[acct] = PAN_FACTORY(tok["token"], qps)
    ctx = Ctx(a, pans)
    run_id = os.environ.get("GITHUB_RUN_ID") or "L%d" % int(time.time() * 1000)
    tag = "%s-%s-%s-s%d%s" % (time.strftime("%Y%m%d"), run_id,
                              os.environ.get("GITHUB_RUN_ATTEMPT", "1"), a.shard,
                              "-dry" if a.dry_run else "")
    ledger = Ledger(forge, "%s/%s.csv" % (LEDGER_DIR, tag))
    deadline = t_start + a.deadline_min * 60
    tally, idx, futs = {}, 0, set()
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        while idx < len(todo) or futs:
            while (idx < len(todo) and len(futs) < a.concurrency and not ctx.stop.is_set()
                   and time.time() < deadline):
                futs.add(ex.submit(process_book, ctx, todo[idx]))
                idx += 1
            if not futs:
                break                                   # deadline / stop: leave the rest
            fin, futs = wait(futs, return_when=FIRST_COMPLETED)
            for f in fin:
                rec = f.result()
                ledger.add(rec)
                tally[rec["status"]] = tally.get(rec["status"], 0) + 1
                print("progress %d/%d %s" % (len(ledger.rows), len(todo),
                                             " ".join("%s=%d" % kv for kv in sorted(tally.items()))),
                      flush=True)
    ledger.flush()
    wall = int(time.time() - t_start)
    calls = {}
    for acct, p in pans.items():
        for k, v in p.stats.items():
            calls["%s_%s" % (acct, k)] = v
    stats = {"shard": a.shard, "shards": a.shards, "wall_s": wall, "books_done": len(ledger.rows),
             "books_deferred": len(todo) - len(ledger.rows), "concurrency": a.concurrency,
             "pages_uploaded": sum(r["pages_uploaded"] for r in ledger.rows),
             "pages_reused": sum(r["pages_reused"] for r in ledger.rows),
             "dry": a.dry_run, "limit_pages": a.limit_pages, **ctx.counts, **calls}
    forge.write("%s/%s.json" % (STATS_DIR, tag), json.dumps(stats, indent=1).encode(), "webp: stats")
    print("end " + " ".join("%s=%s" % kv for kv in sorted(stats.items())), flush=True)
    if ctx.stop.is_set() or not ledger.ok:
        sys.exit(1)


# ---------------------------------------------------------------- summary issue
def cmd_summary(a):
    books = load_books(a.forge, a.books)
    ids = {b["book_id"] for b in books}
    last = {}
    for fp in sorted(glob.glob(os.path.join(a.forge, *LEDGER_DIR.split("/"), "*.csv"))):
        with open(fp, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r["book_id"] in ids and r["status"] != "dry" and last.get(r["book_id"]) != "ok":
                    last[r["book_id"]] = r["status"]
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    runs = []
    for fp in glob.glob(os.path.join(a.forge, *STATS_DIR.split("/"), "*-%s-*.json" % run_id)):
        with open(fp, encoding="utf-8") as f:
            runs.append(json.load(f))
    ok = sum(1 for v in last.values() if v == "ok")
    fail = sum(1 for v in last.values() if v == "fail")
    up = sum(r.get("pages_uploaded", 0) for r in runs)
    reuse = sum(r.get("pages_reused", 0) for r in runs)
    wall = max([r.get("wall_s", 0) for r in runs] or [0])
    rate = sum(v for r in runs for k, v in r.items() if k.endswith("rate_limited"))
    L = ["| \u4e66\u5355\u518c\u6570 | \u5df2\u5b8c\u6210 | \u5931\u8d25 | \u5192\u70df\u622a\u65ad"
         " | \u5269\u4f59 |", "|---|---|---|---|---|",
         "| %d | %d | %d | %d | %d |" % (len(ids), ok, fail, sum(1 for v in last.values() if v == "limit"),
                                     len(ids) - ok),
         "", "**\u672c\u6b21\u8fd0\u884c** run %s \u00b7 \u5206\u7247 %d \u00b7 "
         "\u4e0a\u4f20\u9875 %d \u00b7 \u79d2\u4f20\u9875 %d \u00b7 wall %ds \u00b7 "
         "\u6bcf\u79d2\u9875\u6570 %.2f \u00b7 \u9650\u6d41\u6b21\u6570 %d"
         % (run_id, len(runs), up, reuse, wall, (up + reuse) / wall if wall else 0, rate),
         "", "\u66f4\u65b0\u4e8e %s UTC" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")]
    body = "\n".join(L)
    print(body.encode("ascii", "backslashreplace").decode(), flush=True)
    token, repo = os.environ.get("FORGE_TOKEN", ""), os.environ.get("FORGE_REPO", "")
    if token and repo:
        import gh_issue
        gh_issue.upsert(repo, "webp-progress", ISSUE_TITLE,
                        "%s %d/%d" % (ISSUE_TITLE, ok, len(ids)), body, token=token,
                        state="fail:%d" % fail)


# ---------------------------------------------------------------- selftest
class _FakeResp:
    def __init__(self, j):
        self._j = j

    def json(self):
        return self._j


class _FakeCloud:
    """In-memory 123: folders, files, 429 injection, instant upload by md5."""

    def __init__(self):
        self.nodes = {0: {"name": "", "type": 1, "parent": None}}
        self.next_id = 100
        self.fail_once = {}
        self.calls = {}

    def add(self, parent, name, data=None):
        self.next_id += 1
        self.nodes[self.next_id] = {"name": name, "type": 0 if data is not None else 1,
                                    "parent": parent, "data": data}
        return self.next_id

    def mkpath(self, path):
        cur = 0
        for seg in [s for s in path.split("/") if s]:
            hit = [i for i, n in self.nodes.items() if n["parent"] == cur and n["name"] == seg]
            cur = hit[0] if hit else self.add(cur, seg)
        return cur

    def kids(self, pid):
        return [(i, n) for i, n in sorted(self.nodes.items()) if n["parent"] == pid]

    def request(self, method, url, **kw):
        ep = "/" + url.split("/", 3)[-1] if url.startswith("fake://") else urllib.parse.urlparse(url).path
        self.calls[ep] = self.calls.get(ep, 0) + 1
        if self.fail_once.get(ep):
            self.fail_once[ep] -= 1
            return _FakeResp({"code": 429, "message": "too frequent"})
        p, j, d = kw.get("params") or {}, kw.get("json") or {}, kw.get("data") or {}
        if ep.endswith("/api/v2/file/list"):
            fl = [{"filename": n["name"], "type": n["type"], "fileId": i, "trashed": 0,
                   "size": len(n.get("data") or b"")} for i, n in self.kids(int(p["parentFileId"]))]
            return _FakeResp({"code": 0, "data": {"fileList": fl, "lastFileId": -1}})
        if ep.endswith("/download_info"):
            return _FakeResp({"code": 0, "data": {"downloadUrl": "fake://%d" % p["fileId"]}})
        if ep.endswith("/file/mkdir"):
            return _FakeResp({"code": 0, "data": {"dirID": self.add(int(j["parentID"]), j["name"])}})
        if ep.endswith("/file/domain"):
            return _FakeResp({"code": 0, "data": ["fake://up"]})
        if ep.endswith("/file/create"):
            md5s = {hashlib.md5(n["data"]).hexdigest() for n in self.nodes.values() if n.get("data")}
            if j["etag"] in md5s:
                src = next(n for n in self.nodes.values()
                           if n.get("data") and hashlib.md5(n["data"]).hexdigest() == j["etag"])
                self._put(int(j["parentFileID"]), j["filename"], src["data"])
                return _FakeResp({"code": 0, "data": {"reuse": True, "fileID": self.next_id}})
            return _FakeResp({"code": 0, "data": {"reuse": False, "preuploadID": "x"}})
        if ep.endswith("/single/create"):
            name, data = kw["files"]["file"][0], kw["files"]["file"][1]
            assert hashlib.md5(data).hexdigest() == d["etag"]
            self._put(int(d["parentFileID"]), name, data)
            return _FakeResp({"code": 0, "data": {"completed": True, "fileID": self.next_id}})
        if ep.endswith("/user/info"):
            return _FakeResp({"code": 0, "data": {}})
        return _FakeResp({"code": 1, "message": "unknown endpoint"})

    def _put(self, parent, name, data):
        for i, n in self.kids(parent):
            if n["name"] == name:
                del self.nodes[i]                       # duplicate=2 overwrites
        self.add(parent, name, data)

    def pages(self, path):
        pid = self.mkpath(path)
        return {n["name"]: n["data"] for _, n in self.kids(pid) if n["type"] == 0}


def selftest():
    global PAN_FACTORY, LOGIN, DOWNLOAD, RETRY_SLEEP
    import contextlib
    import fitz
    from PIL import Image

    RETRY_SLEEP = 0
    td = tempfile.mkdtemp(prefix="webp_selftest_")
    cloud = _FakeCloud()
    # sources: a 2-page PDF, and a folder of 3 JPGs named 1, 2, 10 (natural order)
    doc = fitz.open()
    for k in range(2):
        pg = doc.new_page(width=200, height=300)
        pg.insert_text((20, 60), "page %d" % (k + 1), fontsize=20)
    pdf_bytes = doc.tobytes()
    doc.close()
    cloud.add(cloud.mkpath("/SRC/lib/cat"), "Book One secret title.pdf", pdf_bytes)
    jdir = cloud.mkpath("/SRC/lib/jpgbook")
    colors = {"1.jpg": (200, 0, 0), "2.jpg": (0, 200, 0), "10.jpg": (0, 0, 200)}
    for nm in ("10.jpg", "2.jpg", "1.jpg"):
        b = io.BytesIO()
        Image.new("RGB", (120, 160), colors[nm]).save(b, "jpeg", quality=95)
        cloud.add(jdir, nm, b.getvalue())
    # target already has page_0001 of book 1 (resume inside a book)
    pre = cloud.mkpath("/DST/GufangP/lib/cat/tst-0001-01 Book One secret title")
    first = fitz.open(stream=pdf_bytes, filetype="pdf")
    cloud.add(pre, "page_0001.webp", pdf_page_webp(first, 0))
    first.close()

    forge_root = os.path.join(td, "forge")
    os.makedirs(os.path.join(forge_root, "data", "webp"))
    cols = ["book_id", "src_type", "src_acct", "src_path", "src_file_id", "dst_path", "title"]
    rows = [
        ["tst-0001-01", "pdf", "guji", "/SRC/lib/cat/Book One secret title.pdf", "",
         "/DST/GufangP/lib/cat/tst-0001-01 Book One secret title", "Book One secret title"],
        ["tst-0002-01", "jpg", "guji", "/SRC/lib/jpgbook", "",
         "/DST/GufangP/lib/cat/tst-0002-01 Jpg secret title", "Jpg secret title"],
        ["tst-0003-01", "pdf", "guji", "/SRC/lib/cat/Book One secret title.pdf", "",
         "/DST/GufangP/lib/cat/tst-0003-01 Same bytes secret title", "Same bytes secret title"],
        ["tst-0004-01", "pdf", "guji", "/SRC/lib/cat/missing.pdf", "",
         "/DST/GufangP/lib/cat/tst-0004-01 Missing secret title", "Missing secret title"],
    ]
    with open(os.path.join(forge_root, "data", "webp", "t.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)

    os.environ.update(PAN_CID_MAIN="cid-m", PAN_SEC_MAIN="sec-m", PAN_CID_GUJI="cid-g",
                      PAN_SEC_GUJI="sec-g", FORGE_REPO="", FORGE_TOKEN="")
    for k in ("PAN_UID_GUJI", "PAN_DIRECT_KEY_GUJI", "PAN_UID_MAIN", "PAN_DIRECT_KEY_MAIN",
              "GITHUB_ACTIONS", "GITHUB_RUN_ID"):
        os.environ.pop(k, None)
    logins = []

    def fake_login(cid, sec):
        logins.append(cid)
        exp = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%S+08:00")
        return "TOKEN-" + cid, exp

    def fake_pan(token, qps):
        p = panmod.Pan(token, {"list": 0, "dl": 0, "up": 0, "misc": 0})
        p.s = cloud
        return p

    def fake_download(url, dest):
        if not url.startswith("fake://"):
            return False
        with open(dest, "wb") as f:
            f.write(cloud.nodes[int(url[7:])]["data"])
        return True

    PAN_FACTORY, LOGIN, DOWNLOAD = fake_pan, fake_login, fake_download
    ap = build_parser()

    def run(*argv):
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out):
            try:
                a = ap.parse_args(list(argv))
                a.func(a)
            except SystemExit as e:
                code = e.code or 0
        return out.getvalue(), code

    base = ["--forge", forge_root, "--books", "data/webp/t.csv", "--target", "main"]
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # 1. token broker: first run logs in, second reuses; stored blob is encrypted
    out1, _ = run("token", *base)
    out2, _ = run("token", *base)
    blob = open(os.path.join(forge_root, "data", "webp", "tok", "main.enc"), "rb").read()
    check("token: one login per account on first run", sorted(logins) == ["cid-g", "cid-m"])
    check("token: second run reuses, no new login", len(logins) == 2 and out2.count("reused") == 2)
    check("token: stored file is encrypted", b"TOKEN-" not in blob)
    check("token: right secret decrypts", (read_token(Forge(forge_root), "main") or {}).get(
        "token") == "TOKEN-cid-m")
    try:
        _fernet("wrong-secret").decrypt(blob)
        check("token: wrong secret cannot decrypt", False)
    except Exception:                                   # noqa: BLE001
        check("token: wrong secret cannot decrypt", True)

    # 2. dry run: no writes to the cloud
    n_nodes = len(cloud.nodes)
    out, code = run("run", *base, "--shard", "0", "--shards", "1", "--dry-run", "1")
    check("dry: cloud untouched", len(cloud.nodes) == n_nodes and code == 0)
    check("dry: ledger rows written",
          len(glob.glob(os.path.join(forge_root, *LEDGER_DIR.split("/"), "*-dry.csv"))) == 1)

    # 3. first real run stops after 1 book (simulated interruption) + one 429
    cloud.fail_once["/upload/v2/file/single/create"] = 1
    out_a, code = run("run", *base, "--shard", "0", "--shards", "1", "--dry-run", "0",
                      "--max-books", "1", "--concurrency", "1")
    p1 = cloud.pages("/DST/GufangP/lib/cat/tst-0001-01 Book One secret title")
    check("run1: book 1 has page_0001..0002", sorted(p1) == ["page_0001.webp", "page_0002.webp"])
    check("run1: existing page_0001 was skipped (one single upload call ok + one 429)",
          cloud.calls.get("/upload/v2/file/single/create") == 2)
    check("run1: pages are real webp",
          all(Image.open(io.BytesIO(b)).format == "WEBP" for b in p1.values()))
    check("run1: only book 1 processed", "progress 1/1 ok=1" in out_a)

    # 4. resume: book 1 skipped by ledger, book 2 (jpg), 3 (instant upload), 4 (missing)
    before = dict(cloud.calls)
    out_b, code = run("run", *base, "--shard", "0", "--shards", "1", "--dry-run", "0",
                      "--concurrency", "2")
    p2 = cloud.pages("/DST/GufangP/lib/cat/tst-0002-01 Jpg secret title")
    p3 = cloud.pages("/DST/GufangP/lib/cat/tst-0003-01 Same bytes secret title")
    check("run2: skip_done=1 todo=3", "skip_done=1 todo=3" in out_b)
    check("run2: jpg natural order 1,2,10",
          [Image.open(io.BytesIO(p2["page_%04d.webp" % i])).convert("RGB").getpixel((5, 5))[k] > 150
           for i, k in ((1, 0), (2, 1), (3, 2))] == [True, True, True])
    check("run2: identical pages go by instant upload",
          len(p3) == 2 and cloud.calls.get("/upload/v2/file/single/create", 0)
          - before.get("/upload/v2/file/single/create", 0) == 3)
    check("run2: missing source -> fail src_missing", "fail=1" in out_b and "ok=2" in out_b)

    # 5. third run: nothing left except the missing one; no uploads
    before = dict(cloud.calls)
    out_c, _ = run("run", *base, "--shard", "0", "--shards", "1", "--dry-run", "0")
    check("run3: only the failed book is retried, zero uploads",
          "todo=1" in out_c and cloud.calls.get("/upload/v2/file/single/create") ==
          before.get("/upload/v2/file/single/create"))

    # 6. limit_pages smoke on a fresh id is not marked done
    out_d, _ = run("run", "--forge", forge_root, "--books", "tst-0003-01", "--target", "main",
                   "--shard", "0", "--shards", "1", "--dry-run", "0", "--limit-pages", "1",
                   "--force")
    check("limit: status limit (not ok)", "limit=1" in out_d)

    # 7. sharding covers every book exactly once
    got = []
    for s in range(3):
        o, _ = run("run", *base, "--shard", str(s), "--shards", "3", "--dry-run", "1", "--force")
        got.append(int(re.search(r"mine=(\d+)", o).group(1)))
    check("shards: 3 shards split 4 books", sum(got) == 4)

    # 8. logs never leak titles / paths / ids / tokens
    logs = out1 + out2 + out + out_a + out_b + out_c + out_d
    check("logs: no titles, paths, ids, tokens",
          not any(s in logs for s in ("secret title", "/SRC", "/DST", "tst-0", "TOKEN-")))

    # 9. ledger has only ids and numbers
    led = open(sorted(glob.glob(os.path.join(forge_root, *LEDGER_DIR.split("/"), "*.csv")))[-1],
               encoding="utf-8").read()
    check("ledger: no titles or paths", "secret" not in led and "/DST" not in led)

    # 10. direct-link signature follows the site's rule
    os.environ.update(PAN_UID_GUJI="42", PAN_DIRECT_KEY_GUJI="k")
    u = urllib.parse.urlparse(direct_url("guji", "/a/b.pdf"))
    exp, rnd, uid, h = urllib.parse.parse_qs(u.query)["auth_key"][0].split("-")
    check("direct: md5(path-exp-rand-uid-key)", h == hashlib.md5(
        ("/42/a/b.pdf-%s-%s-42-k" % (exp, rnd)).encode()).hexdigest() and u.netloc == "42.cdn.123clouddisk.com")

    for name, ok in checks:
        print("%s  %s" % ("PASS" if ok else "FAIL", name))
    bad = [n for n, ok in checks if not ok]
    print("selftest: %d/%d passed" % (len(checks) - len(bad), len(checks)))
    return 1 if bad else 0


# ---------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    for name, fn in (("token", cmd_token), ("run", cmd_run), ("summary", cmd_summary)):
        p = sub.add_parser(name)
        p.set_defaults(func=fn)
        p.add_argument("--forge", required=True, help="private repo checkout dir")
        p.add_argument("--books", required=True, help="data/webp/x.csv or comma-separated ids")
        p.add_argument("--target", choices=("main", "guji"), default="main")
        if name != "run":
            continue
        p.add_argument("--shard", type=int, default=0)
        p.add_argument("--shards", type=int, default=1)
        p.add_argument("--concurrency", type=int, default=2)
        p.add_argument("--dry-run", type=int, default=1)
        p.add_argument("--limit-pages", type=int, default=0)
        p.add_argument("--deadline-min", type=float, default=320,
                       help="stop starting new books after this many minutes")
        p.add_argument("--force", action="store_true", help="ignore ok rows in the ledger")
        p.add_argument("--no-reuse", action="store_true", help="skip the instant-upload probe")
        # totals per ACCOUNT across all shards; each shard gets total/shards
        p.add_argument("--list-qps", type=float, default=float(os.environ.get("WEBP_LIST_QPS", 1)))
        p.add_argument("--dl-qps", type=float, default=float(os.environ.get("WEBP_DL_QPS", 2)))
        p.add_argument("--up-qps", type=float, default=float(os.environ.get("WEBP_UP_QPS", 4)))
        p.add_argument("--misc-qps", type=float, default=float(os.environ.get("WEBP_MISC_QPS", 1)))
        p.add_argument("--max-books", type=int, default=0, help=argparse.SUPPRESS)
    return ap


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    a = build_parser().parse_args()
    if not getattr(a, "func", None):
        build_parser().print_help()
        sys.exit(2)
    a.func(a)


if __name__ == "__main__":
    main()
