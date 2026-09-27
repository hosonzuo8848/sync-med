# -*- coding: utf-8 -*-
"""Cloud PDF/JPG -> page_NNNN.webp pipeline for 123pan (MVP).

  python run.py plan    --forge DIR --shards N --concurrency C ...  (workflow outputs; in-flight cap;
                                                                     hourly schedule = auto resume)
  python run.py token   --forge DIR --books SPEC --target main
  python run.py run     --forge DIR --books SPEC --target main --shard 0 --shards 1 ...
  python run.py summary --forge DIR --books SPEC --target main
  python run.py --selftest          (stubbed 123 + download, no network)

SPEC: data/webp/x.csv, comma-separated book_ids, @event (read the value from
the workflow_dispatch payload so it never shows up in the public log) or @auto
(the list named in the private data/webp/auto.json, used by the schedule).

Book list CSV lives in the PRIVATE repo (data/webp/*.csv), never in this repo:
  book_id,src_type,src_acct,src_path,src_file_id,dst_path,title
  src_type  pdf | jpg   (jpg: src_path is a folder of jpg/jpeg/png/tif images)
  src_acct  main | guji (the account holding the source; target comes from --target)
  dst_path  full 123 folder path of the volume folder; its name starts with book_id

Shared token: 123 keeps at most 3 live tokens and a new login pushes out the
oldest. Only `token` (prep) logs in as a rule; it keeps ONE token per account in
the private repo (data/webp/tok/<acct>.enc, Fernet, key derived from that
account's client secret) and reuses it while it has > 8h left and passes a
user/info check. Optional data/webp/accounts.json {"main": "<uid>"} makes prep
stop when a secret points at the wrong 123 account. On 401 a shard first
re-reads the token file (someone may have refreshed it); only shard 0 may log
in again, once per run, storing the token only if the file is unchanged since
read. Otherwise the shard stops with exit 1.

Uploads in flight per account must stay under 123's red line of 20 (all writers
together): shards x concurrency <= WEBP_MAX_INFLIGHT (default 16) per run.

Checkpoint: per-book ledger rows go to data/webp/ledger/ in the private repo
(book_id, target account, numbers only). Rows with status=ok for the same target
are skipped by later runs; inside a book, pages already present in the target
folder are not converted again.

Logs print counters only: no titles, no paths, no book ids, no tokens.
"""
import argparse
import base64
import collections
import csv
import glob
import hashlib
import io
import json
import math
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

# Page size rule (CTO, 2026-09-26, tier D): long side = min(source long side,
# WEBP_MAX), WebP q80 method 4, and the source is never upscaled. The source of a
# PDF page is its largest raster image (the scan): the page is rendered so that
# image lands 1:1, then shrunk with LANCZOS. The old fixed 120 dpi render blew a
# 6408 px scan up to 8010 px (~7 MB a page). A page without a usable raster
# image (vector / text) falls back to PDF_DPI.
PDF_DPI, WEBP_Q, WEBP_METHOD, WEBP_MAX = 120, 80, 4, 3200
RENDER_MAX = 16383     # WebP's side limit; bounds the 1:1 render of a small scan on a huge page
SCAN_MIN = 64          # an image thinner than this is an icon / spacer, never the page scan
PAGE_RE = re.compile(r"^page_(\d{4,})\.webp$")
IMG_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
CRED_ENV = {"main": ("PAN_CID_MAIN", "PAN_SEC_MAIN"), "guji": ("PAN_CID_GUJI", "PAN_SEC_GUJI")}
TOK_PATH = "data/webp/tok/%s.enc"
ACCOUNTS_PATH = "data/webp/accounts.json"
LEDGER_DIR = "data/webp/ledger"
STATS_DIR = "data/webp/stats"
# Hourly schedule = automatic resume (cmd_plan). AUTO_PATH names what it resumes:
# {"books": "data/webp/x.csv", "shards": 5, "concurrency": 2, "target": "main"};
# without it the schedule does nothing. After a suspected write ban (breaker, or a
# refusal in 123's answer) a run writes COOL_DIR/<target>.json and the schedule
# waits COOLDOWN_S. A pushed-out token or a deadline just resumes next hour. A
# volume that really failed in GIVE_UP runs is left for a manual dispatch.
AUTO_PATH = "data/webp/auto.json"
COOL_DIR = "data/webp/cooldown"
COOLDOWN_S = 9000
GIVE_UP = 2
REFUSE_WORDS = ("\u62d2\u7edd", "\u7981\u6b62", "\u5c01\u7981", "denied", "forbidden")  # refused / forbidden / banned
MIN_TOKEN_LIFE = 8 * 3600
# fails: every failed attempt of this volume as "<api>:<kind>:<count>", e.g.
#   "upload:net:2 list:429:1 volume:download:1". api/kind from pan.py (CATEGORY;
#   net / http<status> / 401 / 429 / api<code>), or volume:<download | verify |
#   src_missing | badname | ExceptionName> from here.
# fail_msgs: per key the latest raw answer, "<key>=code=<c> <message>", <= 200
#   chars, no token, titles and path segments replaced by <name>. Private ledger only.
LEDGER_COLS = ["book_id", "target", "status", "pages_expected", "pages_uploaded",
               "pages_present", "dst_dir_id", "seconds", "attempts", "err", "fails", "fail_msgs"]
ISSUE_TITLE = "\u4e91\u7aef\u8f6c\u56fe\u8fdb\u5ea6"   # progress issue title (zh)
RETRY_SLEEP = 5
RENEW_TRIES, RENEW_WAIT = 6, 20     # a shard waits up to ~2 min for a refreshed token


class NoRetry(Exception):
    """Book-level failure that a retry cannot fix; str() is the ledger err."""


class Stopped(Exception):
    pass


def page_name(i):
    return "page_%04d.webp" % i


def natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def mask(secret):
    if os.environ.get("GITHUB_ACTIONS"):
        print("::add-mask::" + secret, flush=True)


# ---------------------------------------------------------------- private repo
class Forge:
    """Private repo. Reads come from the local checkout. Writes go to GitHub
    through the contents API when repo+token are set, and to the checkout.
    Each shard writes its own ledger/stats file, so shards never conflict."""

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
        """Freshest copy straight from GitHub (token handling needs the latest)."""
        if not self.repo:
            return self.read(rel)
        r = self._api("GET", rel)
        if r.status_code == 404:
            self.sha.pop(rel, None)
            return None
        r.raise_for_status()
        j = r.json()
        self.sha[rel] = j["sha"]
        return base64.b64decode(j["content"])

    def _local(self, rel, data):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def write(self, rel, data, msg, if_unchanged=False):
        """if_unchanged: store only if the file is still the version last fetched
        (GitHub sha check); a conflict returns False at once, no overwrite."""
        if not self.repo:
            self._local(rel, data)
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
                self._local(rel, data)
                return True
            if status in (409, 422):                 # branch moved / stale sha
                if if_unchanged:
                    return False
                g = self._api("GET", rel)
                if g.status_code == 200:
                    self.sha[rel] = g.json()["sha"]
            time.sleep(2 * (i + 1))
        print("forge write failed http=%s" % status, flush=True)
        return False


def forge_of(a):
    return Forge(a.forge, os.environ.get("FORGE_REPO", ""), os.environ.get("FORGE_TOKEN", ""))


# ---------------------------------------------------------------- shared token
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


def refresh_token(forge, acct, stale):
    """Log in once and store the token only if the token file is unchanged since
    it was last fetched, so a newer token stored by another run is never
    overwritten. Returns the token now in the file, or None."""
    cid, sec = creds(acct)
    t, exp = LOGIN(cid, sec)
    blob = _fernet(sec).encrypt(json.dumps({"token": t, "exp": exp, "at": int(time.time())}).encode())
    if forge.write(TOK_PATH % acct, blob, "webp: refresh %s token" % acct, if_unchanged=True):
        return t
    tok = read_token(forge, acct, fresh=True)         # lost the race: take the winner's token
    return tok["token"] if tok and tok["token"] != stale else None


class Renewer:
    """on_401 hook of one shard for one account (see module doc)."""

    def __init__(self, forge, acct, shard, token):
        self.forge, self.acct, self.shard = forge, acct, shard
        self.cur, self.relogged, self.dead = token, False, False
        self.lock = threading.Lock()

    def __call__(self, bad):
        with self.lock:
            if self.cur != bad:
                return self.cur                         # another thread already renewed
            if self.dead:
                return None
            for _ in range(RENEW_TRIES):
                tok = read_token(self.forge, self.acct, fresh=True)
                new = tok["token"] if tok and tok["token"] != bad else None
                if not new and self.shard == 0 and not self.relogged:
                    self.relogged = True
                    new = refresh_token(self.forge, self.acct, bad)
                if new:
                    mask(new)
                    self.cur = new
                    print("token acct=%s renewed" % self.acct, flush=True)
                    return new
                time.sleep(RENEW_WAIT)
            self.dead = True
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
    if spec == "@event":                                # keeps the list name out of the public log
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
            spec = str((json.load(f).get("inputs") or {}).get("books", "")).strip()
    elif spec == "@auto":                               # the hourly resume: list named in the private repo
        spec = str(json.loads(Forge(forge_root).read(AUTO_PATH) or b"{}").get("books", "")).strip()
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
    want_uid = json.loads(forge.read(ACCOUNTS_PATH) or b"{}")
    for acct in sorted(accounts_needed(books, a.target)):
        cid, sec = creds(acct)
        if not (cid and sec):
            sys.exit("token: credentials missing for acct=%s" % acct)
        tok, state, info = read_token(forge, acct, fresh=True), "missing", None
        if tok and seconds_left(tok) > MIN_TOKEN_LIFE:
            try:
                info = PAN_FACTORY(tok["token"], {"misc": 1}).user_info()
                state = "reused"
            except panmod.TokenInvalid:
                state = "revoked"                       # pushed out by a newer login
            except panmod.PanError as e:
                # only a network failure or rate limiting is a hiccup worth keeping
                # the token for; anything else means it is not usable
                state = "reused" if e.code in ("net", 429) else "error"
        elif tok:
            state = "expiring"
        t = tok["token"] if state == "reused" else refresh_token(forge, acct, tok and tok["token"])
        if not t:
            sys.exit("token: could not store the shared token")
        if state != "reused":
            state, info = "refreshed(was %s)" % state, None
        uid, check = str(want_uid.get(acct) or ""), "unset"
        if uid:
            info = info if info is not None else PAN_FACTORY(t, {"misc": 1}).user_info()
            check = "ok" if str(info.get("uid", "")) == uid else "MISMATCH"
        print("token acct=%s %s uid_check=%s" % (acct, state, check), flush=True)
        if check == "MISMATCH":
            sys.exit("token: acct=%s is not the expected 123 account, check PAN_CID/PAN_CID2" % acct)


def check_inflight(shards, conc):
    """123 red line: at most 20 uploads in flight per ACCOUNT, all writers and
    repos together (2026-07-02: ~60 in flight -> account-wide write ban 2.5 h).
    One run gets WEBP_MAX_INFLIGHT of them; each book thread uploads one page
    at a time, so in flight = shards x concurrency."""
    cap = int(os.environ.get("WEBP_MAX_INFLIGHT", "16"))
    if shards < 1 or conc < 1 or shards * conc > cap:
        sys.exit("shards x concurrency = %d x %d exceeds WEBP_MAX_INFLIGHT=%d" % (shards, conc, cap))


def cmd_plan(a):
    """Workflow outputs (key=value lines for $GITHUB_OUTPUT). A dispatch runs its
    inputs. The hourly schedule resumes AUTO_PATH unless a suspected ban is still
    cooling down or nothing is left; then go=0 and no job starts."""
    p = {"go": 1, "books": "@event", "shards": a.shards, "conc": a.concurrency, "target": a.target,
         "dry": a.dry_run, "limit": a.limit_pages}
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        forge = Forge(a.forge)
        auto = json.loads(forge.read(AUTO_PATH) or b"{}")
        p.update(books="@auto", shards=int(auto.get("shards", 1)), conc=int(auto.get("concurrency", 2)),
                 target=auto.get("target", "main"), dry=0, limit=0)
        cool = json.loads(forge.read("%s/%s.json" % (COOL_DIR, p["target"])) or b"{}")
        left = 0
        if auto.get("books"):
            done, failed = done_ids(a.forge, p["target"]), failed_runs(a.forge, p["target"])
            left = sum(1 for b in load_books(a.forge, "@auto")
                       if b["book_id"] not in done and failed[b["book_id"]] < GIVE_UP)
        why = ("no auto list" if not auto.get("books") else
               "cooling down" if cool.get("until", 0) > time.time() else
               "nothing left" if not left else "")
        print("plan: schedule, %d volumes left, %s" % (left, why or "resume"), file=sys.stderr, flush=True)
        p["go"] = 0 if why else 1
    check_inflight(p["shards"], p["conc"])
    for k in ("go", "books", "conc", "target", "dry", "limit"):
        print("%s=%s" % (k, p[k]))
    print("shards=" + json.dumps(list(range(p["shards"]))))


# ---------------------------------------------------------------- conversion
def to_webp(img, max_side=None):
    """Shrink (never enlarge) to max_side (default WEBP_MAX) with LANCZOS; encode."""
    from PIL import Image
    if img.mode != "RGB":
        img = img.convert("RGB")
    cap = max_side or WEBP_MAX
    if max(img.size) > cap:
        r = cap / max(img.size)
        img = img.resize((max(1, round(img.width * r)), max(1, round(img.height * r))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "webp", quality=WEBP_Q, method=WEBP_METHOD)
    return buf.getvalue()


def native_zoom(page):
    """Pixels per point at which the page's largest raster image (the scan) is
    drawn 1:1. Pages without one fall back to PDF_DPI."""
    best, zoom = 0, PDF_DPI / 72.0
    for im in page.get_image_info():
        a, b, c, d = im["transform"][:4]
        sx, sy = math.hypot(a, b), math.hypot(c, d)       # drawn size in points (rotation-proof)
        w, h = im["width"], im["height"]
        if min(w, h) >= SCAN_MIN and sx > 0 and sy > 0 and w * h > best:
            best, zoom = w * h, min(w / sx, h / sy)
    return zoom


def pdf_page_webp(doc, idx0):
    import fitz
    from PIL import Image
    page = doc[idx0]
    long_pt = max(page.rect.width, page.rect.height)
    zoom = native_zoom(page)
    want = min(round(long_pt * zoom), WEBP_MAX)         # never above the scan's own size
    zoom = min(zoom, RENDER_MAX / long_pt)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    return to_webp(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), want)


def direct_url(acct, path):
    """Signed URL on the purchased 123 direct-link space (zero API calls), same
    signing rule as the site's pan123.js signDirectLink. Off unless WEBP_DIRECT=1:
    it draws on the paid direct-link traffic package the site serves images from."""
    uid = os.environ.get("PAN_UID_" + acct.upper(), "")
    key = os.environ.get("PAN_DIRECT_KEY_" + acct.upper(), "")
    if os.environ.get("WEBP_DIRECT") != "1" or not (uid and key and path):
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
        self.why = ""
        self.lock = threading.Lock()
        self.counts = {"src_direct": 0, "src_api": 0}
        self.fails = collections.Counter()              # failed attempts by kind, whole shard
        self.refused = False                            # some answer read like a refusal (suspected ban)
        self.direct_fail = {}

    def halt(self, why):
        with self.lock:
            self.why = self.why or why
        self.stop.set()

    def bump(self, k):
        with self.lock:
            self.counts[k] = self.counts.get(k, 0) + 1

    def fetch(self, acct, path, file_id, dest, size=0):
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
            file_id, size = pan.find_file(path)
            if not file_id:
                raise NoRetry("src_missing")
        u = pan.download_url(file_id)
        # the size 123 lists for the source is the independent reference for "complete"
        if not (u and DOWNLOAD(u, dest)) or (size and os.path.getsize(dest) != size):
            raise IOError("download")
        self.bump("src_api")


def list_images(pan, folder_path):
    fid = pan.walk(folder_path)
    if fid is None:
        return []
    return sorted([f for f in pan.list_all(fid)
                   if f["type"] == 0 and f["name"].lower().endswith(IMG_EXT)],
                  key=lambda f: natkey(f["name"]))


def present_pages(tgt, dst_id):
    return {f["name"] for f in tgt.list_all(dst_id, fresh=True)
            if f["type"] == 0 and PAGE_RE.match(f["name"])}


def _do_book(ctx, row, rec, st):
    """One attempt at one volume. st = {"td": temp dir, "pdf": open doc or None}
    lives across attempts, so a retry never downloads the source again."""
    a = ctx.a
    # the reader and the D1 registration key volume folders by book_id
    if row["dst_path"].rstrip("/").rpartition("/")[2].split(" ", 1)[0] != row["book_id"]:
        raise NoRetry("badname")
    tgt = ctx.pans[a.target]
    src_acct = (row.get("src_acct") or "guji").strip()
    src = ctx.pans[src_acct]
    stype = (row.get("src_type") or "pdf").strip().lower()
    if a.dry_run:                                       # read-only plan: no download, no write
        dst_id = tgt.walk(row["dst_path"])
        if dst_id is not None:
            rec["dst_dir_id"] = dst_id
            rec["pages_present"] = len(present_pages(tgt, dst_id))
        if stype == "pdf":
            ok = row.get("src_file_id") or src.find_file(row["src_path"])[0]
        else:
            ok = list_images(src, row["src_path"])
        if not ok:
            raise NoRetry("src_missing")
        rec["status"] = "dry"
        return
    # source first: a broken source must not leave an empty folder on 123
    if stype == "pdf":
        if st["pdf"] is None:                           # one good download per volume
            import fitz
            pdf = os.path.join(st["td"], "src.pdf")
            fid = int(row["src_file_id"]) if (row.get("src_file_id") or "").strip() else None
            ctx.fetch(src_acct, row["src_path"], fid, pdf)
            doc = fitz.open(pdf)
            if not (doc.is_pdf and doc.page_count > 0):
                doc.close()
                raise IOError("download")               # e.g. an HTML error page saved as .pdf
            st["pdf"] = doc
        doc = st["pdf"]
        total = doc.page_count

        def make(i):
            return pdf_page_webp(doc, i - 1)
    else:
        imgs = list_images(src, row["src_path"])
        if not imgs:
            raise NoRetry("src_missing")
        total = len(imgs)

        def make(i):
            from PIL import Image
            p = os.path.join(st["td"], "img")
            f = imgs[i - 1]
            ctx.fetch(src_acct, row["src_path"].rstrip("/") + "/" + f["name"], f["id"], p, f["size"])
            with Image.open(p) as im:
                return to_webp(im)
    dst_id = tgt.walk(row["dst_path"], create=True)
    rec["dst_dir_id"] = dst_id
    existing = present_pages(tgt, dst_id)
    want = min(total, a.limit_pages) if a.limit_pages else total
    rec["pages_expected"] = want
    for i in range(1, want + 1):
        if ctx.stop.is_set():
            raise Stopped()
        if page_name(i) in existing:
            continue
        tgt.upload(dst_id, page_name(i), make(i))
        rec["pages_uploaded"] += 1
    present = present_pages(tgt, dst_id)
    rec["pages_present"] = sum(1 for i in range(1, want + 1) if page_name(i) in present)
    if rec["pages_present"] != want:
        raise IOError("verify")
    rec["status"] = "ok" if want == total else "limit"


def process_book(ctx, row):
    t0 = time.time()
    rec = dict.fromkeys(LEDGER_COLS, 0)
    rec.update(book_id=row["book_id"], target=ctx.a.target, status="fail", dst_dir_id="", err="")
    fails, msgs = collections.Counter(), {}
    for p in ctx.pans.values():
        p.tl.sink, p.tl.msgs = fails, msgs              # this thread works on this volume only
    try:
        with tempfile.TemporaryDirectory() as td:
            st = {"td": td, "pdf": None}
            for attempt in range(1, 4):
                rec["attempts"] = attempt
                try:
                    _do_book(ctx, row, rec, st)
                    rec["err"] = ""
                    break
                except panmod.TokenInvalid:
                    rec.update(status="fail", err="token")
                    ctx.halt("token")
                    break
                except panmod.Tripped as e:
                    rec.update(status="fail", err="pan_%s" % e.code)
                    ctx.halt("breaker")
                    break
                except Stopped:
                    rec.update(status="fail", err="stopped")
                    break
                except NoRetry as e:
                    rec.update(status="fail", err=str(e))
                    fails["volume:" + str(e)] += 1
                    break
                except panmod.PanError as e:
                    rec.update(status="fail", err="pan_%s" % e.code)
                    if e.code == "net":
                        # the call was already retried in place (NET_BACKOFF); the volume
                        # stops here and the next dispatch resumes at the missing pages
                        break
                except Exception as e:                  # noqa: BLE001
                    err = (str(e) if isinstance(e, IOError) and str(e) in ("download", "verify")
                           else type(e).__name__)[:30]
                    rec.update(status="fail", err=err)
                    fails["volume:" + err] += 1
                    if err not in ("download", "verify"):
                        msgs["volume:" + err] = str(e)[:200]
                if attempt < 3 and not ctx.stop.is_set():
                    time.sleep(RETRY_SLEEP * attempt)
                elif ctx.stop.is_set():
                    break
            if st["pdf"] is not None:
                st["pdf"].close()
    finally:
        for p in ctx.pans.values():
            p.tl.sink = p.tl.msgs = None
    rec["seconds"] = int(time.time() - t0)
    rec["fails"] = " ".join("%s:%d" % kv for kv in sorted(fails.items()))
    # the ledger keeps ids and numbers only: blank out any title / path segment 123 echoed back
    names = sorted({s for s in [row.get("title") or ""] + (row.get("dst_path") or "").split("/")
                    + (row.get("src_path") or "").split("/") if len(s) >= 4}, key=len, reverse=True)
    said = []
    for k, v in sorted(msgs.items()):
        for s in names:
            v = v.replace(s, "<name>")
        said.append("%s=%s" % (k, v))
    rec["fail_msgs"] = " | ".join(said)
    with ctx.lock:
        ctx.fails.update(fails)
        ctx.refused = ctx.refused or any(w in rec["fail_msgs"] for w in REFUSE_WORDS)
    return rec


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


def ledger_rows(forge_root, target):
    for fp in sorted(glob.glob(os.path.join(forge_root, *LEDGER_DIR.split("/"), "*.csv"))):
        with open(fp, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("target") == target:           # ok on one account is not ok on the other
                    yield r


def done_ids(forge_root, target):
    return {r["book_id"] for r in ledger_rows(forge_root, target) if r.get("status") == "ok"}


def failed_runs(forge_root, target):
    """book_id -> runs in which it really failed (a deadline stop or a pushed-out token is no failure)."""
    n = collections.Counter()
    for r in ledger_rows(forge_root, target):
        if r.get("status") == "fail" and r.get("err") not in ("stopped", "token"):
            n[r["book_id"]] += 1
    return n


def cmd_run(a):
    t_start = time.time()
    check_inflight(a.shards, a.concurrency)
    forge = forge_of(a)
    books = load_books(a.forge, a.books)
    mine = [b for i, b in enumerate(books) if i % a.shards == a.shard]
    done = set() if a.force else done_ids(a.forge, a.target)
    failed = failed_runs(a.forge, a.target)
    pending = [b for b in mine if b["book_id"] not in done
               and not (a.books == "@auto" and failed[b["book_id"]] >= GIVE_UP)]
    pending.sort(key=lambda b: (failed[b["book_id"]] > 0, b["book_id"]))   # earlier failures go last
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
        mask(tok["token"])
        pans[acct] = PAN_FACTORY(tok["token"], qps, on_401=Renewer(forge, acct, a.shard, tok["token"]))
    ctx = Ctx(a, pans)
    run_id = os.environ.get("GITHUB_RUN_ID") or "L%d" % int(time.time() * 1000)
    tag = "%s-%s-%s-s%d%s" % (time.strftime("%Y%m%d"), run_id,
                              os.environ.get("GITHUB_RUN_ATTEMPT", "1"), a.shard,
                              "-dry" if a.dry_run else "")
    ledger = Ledger(forge, "%s/%s.csv" % (LEDGER_DIR, tag))
    deadline = t_start + a.deadline_min * 60           # stop starting new books
    stop_at = t_start + a.stop_min * 60                # in-flight books stop at their next page
    tally, idx, futs = {}, 0, set()
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        while idx < len(todo) or futs:
            if time.time() >= stop_at:
                ctx.halt("deadline")
            while (idx < len(todo) and len(futs) < a.concurrency and not ctx.stop.is_set()
                   and time.time() < deadline):
                futs.add(ex.submit(process_book, ctx, todo[idx]))
                idx += 1
            if not futs:
                break                                   # deadline / stop: leave the rest
            fin, futs = wait(futs, timeout=30, return_when=FIRST_COMPLETED)
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
    stats = {"shard": a.shard, "shards": a.shards, "target": a.target, "wall_s": wall,
             "books_done": len(ledger.rows), "books_deferred": len(todo) - len(ledger.rows),
             "concurrency": a.concurrency, "stop": ctx.why or "-",
             "pages_uploaded": sum(r["pages_uploaded"] for r in ledger.rows),
             "dry": a.dry_run, "limit_pages": a.limit_pages, **ctx.counts, **calls,
             **{"fail_" + k: v for k, v in ctx.fails.items()}}
    forge.write("%s/%s.json" % (STATS_DIR, tag), json.dumps(stats, indent=1).encode(), "webp: stats")
    print("end " + " ".join("%s=%s" % kv for kv in sorted(stats.items())), flush=True)
    if ctx.why == "breaker" or ctx.refused:             # suspected write ban: the schedule waits COOLDOWN_S
        forge.write("%s/%s.json" % (COOL_DIR, a.target), json.dumps(
            {"until": int(time.time()) + COOLDOWN_S, "why": ctx.why or "refused"}).encode(), "webp: cooldown")
    # a deadline stop is the normal end of a long run; token / breaker stops are not
    if ctx.why in ("token", "breaker") or not ledger.ok:
        sys.exit(1)


# ---------------------------------------------------------------- summary issue
def cmd_summary(a):
    books = load_books(a.forge, a.books)
    ids = {b["book_id"] for b in books}
    last = {}
    for r in ledger_rows(a.forge, a.target):
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
    fails = sum(v for r in runs for k, v in r.items() if k.startswith("fail_"))
    wall = max([r.get("wall_s", 0) for r in runs] or [0])
    rate = sum(v for r in runs for k, v in r.items() if k.endswith("rate_limited"))
    stops = ",".join(sorted({r.get("stop", "-") for r in runs})) or "-"
    L = ["| \u76ee\u6807\u8d26\u53f7 | \u4e66\u5355\u518c\u6570 | \u5df2\u5b8c\u6210 | \u5931\u8d25"
         " | \u5192\u70df\u622a\u65ad | \u5269\u4f59 |", "|---|---|---|---|---|---|",
         "| %s | %d | %d | %d | %d | %d |" % (a.target, len(ids), ok, fail,
                                           sum(1 for v in last.values() if v == "limit"), len(ids) - ok),
         "", "**\u672c\u6b21\u8fd0\u884c** run %s \u00b7 \u5206\u7247 %d \u00b7 "
         "\u4e0a\u4f20\u9875 %d \u00b7 \u4e2d\u9014\u5931\u8d25 %d \u00b7 wall %ds \u00b7 "
         "\u6bcf\u79d2\u9875\u6570 %.2f \u00b7 \u9650\u6d41\u6b21\u6570 %d \u00b7 stop %s"
         % (run_id, len(runs), up, fails, wall, up / wall if wall else 0, rate, stops),
         "", "\u66f4\u65b0\u4e8e %s UTC" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")]
    body = "\n".join(L)
    print(body.encode("ascii", "backslashreplace").decode(), flush=True)
    token, repo = os.environ.get("FORGE_TOKEN", ""), os.environ.get("FORGE_REPO", "")
    if token and repo:
        import gh_issue
        prefix = "%s %s" % (ISSUE_TITLE, a.target)
        # founder's rule: never push to his phone -> body updates only, no comments,
        # no assignee, no @mention (the body is counters only)
        gh_issue.upsert(repo, "webp-progress", prefix, "%s %d/%d" % (prefix, ok, len(ids)), body,
                        token=token, notify=False)


# ---------------------------------------------------------------- selftest
class _FakeResp:
    def __init__(self, j):
        self._j = j

    def json(self):
        return self._j


class _FakeCloud:
    """In-memory 123: folders, paginated listing, trash flag, and fault injection
    (429 once, dead tokens, lost uploads, dropped connections, write ban)."""

    def __init__(self):
        self.nodes = {0: {"name": "", "type": 1, "parent": None}}
        self.next_id = 100
        self.page_size = 100
        self.fail_once = {}
        self.calls = {}
        self.dead = set()          # tokens answered with 401
        self.drop = {}             # (book_id, filename) -> single uploads to swallow
        self.net_drop = {}         # (book_id, filename) -> single uploads whose connection breaks
        self.err_once = {}         # endpoint suffix -> (code, message) answered once
        self.internal = {}         # (book_id, filename) -> uploads answered code=1 "internal error"
        self.ban_writes = False    # code=1 on every write, like 123's account-level ban
        self.up_log = []           # (book_id, filename) of every single upload that arrived
        self.lock = threading.Lock()

    def add(self, parent, name, data=None, trashed=0):
        self.next_id += 1
        self.nodes[self.next_id] = {"name": name, "type": 0 if data is not None else 1,
                                    "parent": parent, "data": data, "trashed": trashed}
        return self.next_id

    def mkpath(self, path):
        cur = 0
        for seg in [s for s in path.split("/") if s]:
            hit = [i for i, n in self.nodes.items() if n["parent"] == cur and n["name"] == seg]
            cur = hit[0] if hit else self.add(cur, seg)
        return cur

    def find(self, path):
        cur = 0
        for seg in [s for s in path.split("/") if s]:
            hit = [i for i, n in self.nodes.items() if n["parent"] == cur and n["name"] == seg]
            if not hit:
                return None
            cur = hit[0]
        return cur

    def kids(self, pid):
        return [(i, n) for i, n in sorted(self.nodes.items()) if n["parent"] == pid]

    def request(self, method, url, headers=None, **kw):
        with self.lock:                                 # shards run books in threads
            return self._request(method, url, headers, **kw)

    def _request(self, method, url, headers, **kw):
        ep = "/" + url.split("/", 3)[-1] if url.startswith("fake://") else urllib.parse.urlparse(url).path
        self.calls[ep] = self.calls.get(ep, 0) + 1
        tok = (headers or {}).get("Authorization", "")[7:]
        if tok in self.dead:
            return _FakeResp({"code": 401, "message": "tokens number has exceeded the limit"})
        if self.fail_once.get(ep):
            self.fail_once[ep] -= 1
            return _FakeResp({"code": 429, "message": "too frequent"})
        for suffix in [s for s in self.err_once if ep.endswith(s)]:
            code, msg = self.err_once.pop(suffix)
            return _FakeResp({"code": code, "message": msg})
        p, j, d = kw.get("params") or {}, kw.get("json") or {}, kw.get("data") or {}
        if self.ban_writes and ep.endswith(("/file/mkdir", "/single/create")):
            return _FakeResp({"code": 1, "message": "denied"})
        if ep.endswith("/api/v2/file/list"):
            rest = [(i, n) for i, n in self.kids(int(p["parentFileId"])) if i > int(p["lastFileId"])]
            page = rest[:self.page_size]
            fl = [{"filename": n["name"], "type": n["type"], "fileId": i, "trashed": n["trashed"],
                   "size": len(n.get("data") or b"")} for i, n in page]
            more = len(rest) > self.page_size
            return _FakeResp({"code": 0, "data": {"fileList": fl,
                                                  "lastFileId": page[-1][0] if more else -1}})
        if ep.endswith("/download_info"):
            return _FakeResp({"code": 0, "data": {"downloadUrl": "fake://%d" % p["fileId"]}})
        if ep.endswith("/file/mkdir"):
            return _FakeResp({"code": 0, "data": {"dirID": self.add(int(j["parentID"]), j["name"])}})
        if ep.endswith("/file/domain"):
            return _FakeResp({"code": 0, "data": ["fake://up"]})
        if ep.endswith("/single/create"):
            name, data = kw["files"]["file"][0], kw["files"]["file"][1]
            assert hashlib.md5(data).hexdigest() == d["etag"]
            parent = int(d["parentFileID"])
            key = (self.nodes[parent]["name"].split(" ")[0], name)
            if self.net_drop.get(key):
                self.net_drop[key] -= 1
                raise ConnectionError("connection reset")     # no answer at all
            if self.internal.get(key):
                self.internal[key] -= 1
                return _FakeResp({"code": 1, "message": panmod.INTERNAL_WORD})
            self._log(parent, name)
            if self.drop.get(key):
                self.drop[key] -= 1                    # "success" that never lands
                return _FakeResp({"code": 0, "data": {"completed": True, "fileID": 1}})
            self._put(int(d["parentFileID"]), name, data)
            return _FakeResp({"code": 0, "data": {"completed": True, "fileID": self.next_id}})
        if ep.endswith("/user/info"):
            return _FakeResp({"code": 0, "data": {"uid": 139001 if "cid-m" in tok else 136001}})
        return _FakeResp({"code": 1, "message": "unknown endpoint"})

    def _log(self, parent, name):
        key = (self.nodes[parent]["name"].split(" ")[0], name)
        self.up_log.append(key)
        return key

    def _put(self, parent, name, data):
        for i, n in self.kids(parent):
            if n["name"] == name:
                del self.nodes[i]                       # duplicate=2 overwrites
        self.add(parent, name, data)

    def pages(self, path):
        pid = self.find(path)
        return {} if pid is None else {n["name"]: n["data"] for _, n in self.kids(pid)
                                       if n["type"] == 0 and not n["trashed"]}


def selftest():
    global PAN_FACTORY, LOGIN, DOWNLOAD, RETRY_SLEEP, RENEW_WAIT
    import contextlib
    import fitz
    from PIL import Image

    RETRY_SLEEP = RENEW_WAIT = 0
    panmod.SLEEP = lambda s: None
    td = tempfile.mkdtemp(prefix="webp_selftest_")
    cloud = _FakeCloud()
    cloud.page_size = 1                                 # every listing below is paginated

    def make_pdf(n, label):
        doc = fitz.open()
        for k in range(n):
            pg = doc.new_page(width=200, height=300)
            pg.insert_text((20, 60), "%s %d" % (label, k + 1), fontsize=20)
        b = doc.tobytes()
        doc.close()
        return b

    def solid_webp(color):
        b = io.BytesIO()
        Image.new("RGB", (10, 10), color).save(b, "webp")
        return b.getvalue()

    # sources: a 2-page PDF, a different 3-page PDF, an HTML error page, 3 JPGs 1/2/10
    pdf_bytes, pdf3 = make_pdf(2, "page"), make_pdf(3, "other")
    cat = cloud.mkpath("/SRC/lib/cat")
    book1_url = "fake://%d" % cloud.add(cat, "Book One secret title.pdf", pdf_bytes)
    net_urls = ["fake://%d" % cloud.add(cat, "Net%d secret.pdf" % k, make_pdf(3, "net%d" % k)) for k in (1, 2)]
    cloud.add(cat, "Other secret.pdf", pdf3)
    cloud.add(cat, "Html secret.pdf", b"<html><body>error page</body></html>")
    trunc_id = cloud.add(cat, "Trunc secret.pdf", pdf3)
    jdir = cloud.mkpath("/SRC/lib/jpgbook")
    colors = {"1.jpg": (200, 0, 0), "2.jpg": (0, 200, 0), "10.jpg": (0, 0, 200)}
    for nm in ("10.jpg", "2.jpg", "1.jpg"):
        b = io.BytesIO()
        Image.new("RGB", (120, 160), colors[nm]).save(b, "jpeg", quality=95)
        cloud.add(jdir, nm, b.getvalue())
    # target already has page_0001 of book 1, with bytes the renderer never makes
    D = "/DST/GufangP/lib/cat/"
    seed = solid_webp((10, 200, 10))
    cloud.add(cloud.mkpath(D + "tst-0001-01 Book One secret title"), "page_0001.webp", seed)
    # book 6: a same-name page sits in the trash -> it must be uploaded again
    cloud.add(cloud.mkpath(D + "tst-0006-01 Trash secret"), "page_0001.webp", seed, trashed=1)

    forge_root = os.path.join(td, "forge")
    os.makedirs(os.path.join(forge_root, "data", "webp"))
    cols = ["book_id", "src_type", "src_acct", "src_path", "src_file_id", "dst_path", "title"]
    S = "/SRC/lib/cat/"

    def book(bid, src, leaf, stype="pdf"):
        return [bid, stype, "guji", src, "", D + leaf, "secret title"]

    lists = {
        "t.csv": [book("tst-0001-01", S + "Book One secret title.pdf", "tst-0001-01 Book One secret title"),
                  book("tst-0002-01", "/SRC/lib/jpgbook", "tst-0002-01 Jpg secret title", "jpg"),
                  book("tst-0003-01", S + "Book One secret title.pdf", "tst-0003-01 Same bytes secret"),
                  book("tst-0004-01", S + "missing.pdf", "tst-0004-01 Missing secret title")],
        "t2.csv": [book("tst-0005-01", S + "Other secret.pdf", "tst-0005-01 Lost secret"),
                   book("tst-0006-01", S + "Other secret.pdf", "tst-0006-01 Trash secret"),
                   book("tst-0007-01", S + "Html secret.pdf", "tst-0007-01 Html secret"),
                   book("tst-0008-01", S + "Trunc secret.pdf", "tst-0008-01 Trunc secret"),
                   book("tst-0009-01", S + "Other secret.pdf", "tst-0099-01 Wrong id secret")],
        "t3.csv": [book("tst-0010-01", S + "Other secret.pdf", "tst-0010-01 Kicked secret"),
                   book("tst-0011-01", S + "Other secret.pdf", "tst-0011-01 Kicked secret")],
        "t4.csv": [book("tst-00%d-01" % k, S + "Other secret.pdf", "tst-00%d-01 Ban secret" % k)
                   for k in (12, 13, 14)],
        "t5.csv": [book("tst-00%d-01" % k, S + "Net%d secret.pdf" % (k - 14), "tst-00%d-01 Net secret" % k)
                   for k in (15, 16)],
        "t6.csv": [book("tst-0017-01", S + "Net1 secret.pdf", "tst-0017-01 Err secret")],
        "t7.csv": [book("tst-00%d-01" % k, S + "Net2 secret.pdf", "tst-00%d-01 Auto secret" % k) for k in (18, 19)],
        "t8.csv": [book("tst-0020-01", S + "Net2 secret.pdf", "tst-0020-01 Given up secret")],
    }
    for fn, rows in lists.items():
        with open(os.path.join(forge_root, "data", "webp", fn), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)

    os.environ.update(PAN_CID_MAIN="cid-m", PAN_SEC_MAIN="sec-m", PAN_CID_GUJI="cid-g",
                      PAN_SEC_GUJI="sec-g", FORGE_REPO="", FORGE_TOKEN="")
    for k in ("PAN_UID_GUJI", "PAN_DIRECT_KEY_GUJI", "PAN_UID_MAIN", "PAN_DIRECT_KEY_MAIN",
              "GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_EVENT_PATH", "WEBP_DIRECT",
              "WEBP_MAX_INFLIGHT"):
        os.environ.pop(k, None)
    logins = []

    def fake_login(cid, sec):
        logins.append(cid)
        exp = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%S+08:00")
        return "TOKEN-%s-%d" % (cid, len(logins)), exp

    def fake_pan(token, qps, on_401=None):
        p = panmod.Pan(token, {"list": 0, "dl": 0, "up": 0, "misc": 0}, on_401=on_401)
        p.s = cloud
        return p

    dl_log = []                                         # every source download, by url

    def fake_download(url, dest):
        if not url.startswith("fake://"):
            return False
        dl_log.append(url)
        data = cloud.nodes[int(url[7:])]["data"]
        with open(dest, "wb") as f:
            f.write(data[:len(data) // 2] if int(url[7:]) == trunc_id else data)
        return True

    PAN_FACTORY, LOGIN, DOWNLOAD = fake_pan, fake_login, fake_download
    ap = build_parser()
    logs = []

    def run(*argv):
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                a = ap.parse_args(list(argv))
                a.func(a)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        logs.append(out.getvalue())
        return out.getvalue(), code

    def base(lst="t.csv", target="main"):
        return ["--forge", forge_root, "--books", "data/webp/" + lst, "--target", target]

    def shard(*extra, lst="t.csv", s=0, n=1, dry=0):
        return run("run", *base(lst), "--shard", str(s), "--shards", str(n), "--dry-run", str(dry),
                   *extra)

    def rows_of():
        got = {}
        for fp in sorted(glob.glob(os.path.join(forge_root, *LEDGER_DIR.split("/"), "*.csv"))):
            with open(fp, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    got[r["book_id"]] = r
        return got

    def ups(bid, name=None):
        return sum(1 for b, n in cloud.up_log if b == bid and (name is None or n == name))

    def cur_token(acct):
        return (read_token(Forge(forge_root), acct) or {}).get("token")

    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # 1. token broker: first run logs in, second reuses; stored blob is encrypted
    out1, _ = run("token", *base())
    out2, _ = run("token", *base())
    blob = open(os.path.join(forge_root, "data", "webp", "tok", "main.enc"), "rb").read()
    check("token: one login per account on first run", sorted(logins) == ["cid-g", "cid-m"])
    check("token: second run reuses, no new login", len(logins) == 2 and out2.count("reused") == 2)
    check("token: stored file is encrypted", b"TOKEN-" not in blob)
    check("token: right secret decrypts", cur_token("main") == "TOKEN-cid-m-2")
    try:
        _fernet("wrong-secret").decrypt(blob)
        check("token: wrong secret cannot decrypt", False)
    except Exception:                                   # noqa: BLE001
        check("token: wrong secret cannot decrypt", True)

    # 2. dry run: no writes to the cloud
    n_nodes = len(cloud.nodes)
    out, code = shard(dry=1)
    check("dry: cloud untouched", len(cloud.nodes) == n_nodes and code == 0 and not cloud.up_log)
    check("dry: ledger rows written",
          len(glob.glob(os.path.join(forge_root, *LEDGER_DIR.split("/"), "*-dry.csv"))) == 1)

    # 3. first real run stops after 1 book (simulated interruption) + one 429
    cloud.fail_once["/upload/v2/file/single/create"] = 1
    out_a, code = shard("--max-books", "1", "--concurrency", "1")
    p1 = cloud.pages(D + "tst-0001-01 Book One secret title")
    check("run1: book 1 has page_0001..0002", sorted(p1) == ["page_0001.webp", "page_0002.webp"])
    check("run1: existing page_0001 kept byte-for-byte, never uploaded",
          p1.get("page_0001.webp") == seed and ups("tst-0001-01", "page_0001.webp") == 0)
    check("run1: page_0002 sent once, one 429 absorbed, no probe",
          ups("tst-0001-01", "page_0002.webp") == 1
          and cloud.calls.get("/upload/v2/file/single/create") == 2
          and not cloud.calls.get("/upload/v2/file/create"))
    check("run1: pages are real webp",
          all(Image.open(io.BytesIO(b)).format == "WEBP" for b in p1.values()))
    check("run1: only book 1 processed", "progress 1/1 ok=1" in out_a)
    fails_429 = rows_of()["tst-0001-01"]["fails"]

    # 4. resume: book 1 skipped by ledger; 2 = jpg; 3 = one upload silently lost
    #    (verify must catch it, the retry re-sends that page); 4 = missing source
    cloud.drop[("tst-0003-01", "page_0002.webp")] = 1
    del dl_log[:]
    out_b, code = shard("--concurrency", "2")
    p2 = cloud.pages(D + "tst-0002-01 Jpg secret title")
    p3 = cloud.pages(D + "tst-0003-01 Same bytes secret")
    led = rows_of()
    check("run2: skip_done=1 todo=3", "skip_done=1 todo=3" in out_b)
    check("run2: jpg natural order 1,2,10",
          [Image.open(io.BytesIO(p2["page_%04d.webp" % i])).convert("RGB").getpixel((5, 5))[k] > 150
           for i, k in ((1, 0), (2, 1), (3, 2))] == [True, True, True])
    check("run2: lost upload caught by verify; the retry re-sends only that page, no 2nd download",
          len(p3) == 2 and led["tst-0003-01"]["status"] == "ok" and led["tst-0003-01"]["attempts"] == "2"
          and ups("tst-0003-01", "page_0002.webp") == 2 and ups("tst-0003-01", "page_0001.webp") == 1
          and dl_log.count(book1_url) == 1)
    fails_verify = led["tst-0003-01"]["fails"]
    check("run2: missing source -> fail src_missing, no folder left behind",
          led["tst-0004-01"]["err"] == "src_missing" and "ok=2" in out_b
          and cloud.find(D + "tst-0004-01 Missing secret title") is None)

    # 5. third run: nothing left except the missing one; no uploads
    n_up = len(cloud.up_log)
    out_c, _ = shard()
    check("run3: only the failed book is retried, zero uploads",
          "todo=1" in out_c and len(cloud.up_log) == n_up)

    # 6. the ledger is per target account: done on main is still to do on guji
    out_g, _ = run("run", *base(target="guji"), "--dry-run", "1")
    check("ledger: ok on main does not count for guji", "skip_done=0 todo=4" in out_g)

    # 7. limit_pages smoke on a fresh id is not marked done
    out_d, _ = run("run", "--forge", forge_root, "--books", "tst-0003-01", "--target", "main",
                   "--dry-run", "0", "--limit-pages", "1", "--force")
    check("limit: status limit (not ok)", "limit=1" in out_d)

    # 8. failure modes, each must end in its own err and never in ok
    cloud.drop[("tst-0005-01", "page_0001.webp")] = 99
    out_e, code_e = shard("--concurrency", "1", lst="t2.csv")
    led = rows_of()
    check("verify: an upload that never lands ends in err=verify",
          led["tst-0005-01"]["status"] == "fail" and led["tst-0005-01"]["err"] == "verify")
    check("trash: a trashed same-name page is uploaded again",
          led["tst-0006-01"]["status"] == "ok" and ups("tst-0006-01", "page_0001.webp") == 1
          and len(cloud.pages(D + "tst-0006-01 Trash secret")) == 3)
    check("source: an HTML page saved as .pdf is a failed download, nothing uploaded",
          led["tst-0007-01"]["err"] == "download" and ups("tst-0007-01") == 0
          and cloud.find(D + "tst-0007-01 Html secret") is None)
    fails_download = led["tst-0007-01"]["fails"]
    check("source: a download shorter than the listed size is a failed download",
          led["tst-0008-01"]["err"] == "download" and ups("tst-0008-01") == 0)
    check("name: folder not starting with book_id -> badname, no folder created",
          led["tst-0009-01"]["err"] == "badname" and cloud.find(D + "tst-0099-01 Wrong id secret") is None)
    check("failures do not stop the shard", code_e == 0)

    # 9. 401 = token pushed out by a newer login, never mistaken for rate limiting
    cloud.dead.add(cur_token("main"))
    out_t, _ = run("token", *base())
    check("token: revoked token -> prep logs in again", "acct=main refreshed(was revoked)" in out_t
          and logins.count("cid-m") == 2 and cur_token("main") not in cloud.dead)
    cloud.dead.add(cur_token("main"))
    n_login = len(logins)
    out_k, code_k = shard("--concurrency", "1", lst="t3.csv", s=1, n=2)
    check("401: shard 1 may not log in -> stops fast, err=token, exit 1, no rate-limit loop",
          code_k == 1 and rows_of()["tst-0011-01"]["err"] == "token" and len(logins) == n_login
          and "main_rate_limited" not in out_k and "stop=token" in out_k)
    out_k0, code_k0 = shard("--concurrency", "1", lst="t3.csv", s=0, n=2)
    check("401: shard 0 logs in once, stores the token and finishes",
          code_k0 == 0 and len(logins) == n_login + 1 and "renewed" in out_k0
          and rows_of()["tst-0010-01"]["status"] == "ok" and cur_token("main") not in cloud.dead)

    # 10. breaker: code=1 on every write (account-level write ban) stops the shard
    cloud.ban_writes = True
    n_mk = cloud.calls.get("/upload/v1/file/mkdir", 0)
    out_z, code_z = shard("--concurrency", "1", lst="t4.csv")
    cloud.ban_writes = False
    check("breaker: write ban -> stop after %d writes, exit 1, rest untouched" % panmod.BREAK_AFTER,
          code_z == 1 and cloud.calls.get("/upload/v1/file/mkdir", 0) - n_mk == panmod.BREAK_AFTER
          and "stop=breaker" in out_z and "tst-0014-01" not in rows_of())

    # 11. in-flight cap and matrix
    out_m, code_m = run("plan", "--forge", forge_root, "--shards", "8", "--concurrency", "2")
    _, code_m2 = run("plan", "--forge", forge_root, "--shards", "9", "--concurrency", "2")
    _, code_m3 = shard("--concurrency", "17", dry=1)
    check("inflight: 8x2 ok, 9x2 and 1x17 refused (cap 16)",
          code_m == 0 and "shards=[0, 1, 2, 3, 4, 5, 6, 7]" in out_m and "go=1" in out_m
          and "books=@event" in out_m and code_m2 == 1 and code_m3 == 1)

    # 12. @event keeps the list name out of the command line
    ev = os.path.join(td, "event.json")
    with open(ev, "w", encoding="utf-8") as f:
        json.dump({"inputs": {"books": "data/webp/t.csv"}}, f)
    os.environ["GITHUB_EVENT_PATH"] = ev
    out_v, _ = run("run", "--forge", forge_root, "--books", "@event", "--dry-run", "1", "--force")
    os.environ.pop("GITHUB_EVENT_PATH")
    check("event: --books @event reads the dispatch input", "listed=4" in out_v)

    # 13. account check: prep stops when a secret points at the wrong 123 account
    acc = os.path.join(forge_root, *ACCOUNTS_PATH.split("/"))
    with open(acc, "w", encoding="utf-8") as f:
        json.dump({"main": "139001", "guji": "136001"}, f)
    out_u, code_u = run("token", *base())
    with open(acc, "w", encoding="utf-8") as f:
        json.dump({"main": "136001"}, f)
    out_u2, code_u2 = run("token", *base())
    os.remove(acc)
    check("uid: right accounts pass, a swapped account stops prep",
          code_u == 0 and out_u.count("uid_check=ok") == 2 and code_u2 == 1 and "MISMATCH" in out_u2)

    # 14. sharding covers every book exactly once
    got = []
    for s in range(3):
        o, _ = shard("--force", s=s, n=3, dry=1)
        got.append(int(re.search(r"mine=(\d+)", o).group(1)))
    check("shards: 3 shards split 4 books", sum(got) == 4)

    # 19. network hiccups: the call is retried in place after 2 / 5 / 15 s; a
    #     volume is never restarted or downloaded again for them; nothing ever
    #     probes instant upload; every failed attempt is booked by kind
    sleeps = []
    panmod.SLEEP = sleeps.append
    cloud.net_drop[("tst-0015-01", "page_0002.webp")] = 3      # absorbed by the 3 retries
    cloud.net_drop[("tst-0016-01", "page_0002.webp")] = 4      # outlasts them
    del dl_log[:]
    out_n, code_n = shard("--concurrency", "1", lst="t5.csv")
    panmod.SLEEP = lambda s: None
    led = rows_of()
    n15, n16 = led["tst-0015-01"], led["tst-0016-01"]
    check("net: 3 drops on one page are retried in place after 2/5/15 s, volume ok at attempt 1",
          n15["status"] == "ok" and n15["attempts"] == "1" and sleeps[:3] == [2, 5, 15]
          and len(cloud.pages(D + "tst-0015-01 Net secret")) == 3)
    check("net: a 4th drop stops the volume (pan_net) with no restart and no 2nd download",
          n16["err"] == "pan_net" and n16["attempts"] == "1" and dl_log.count(net_urls[1]) == 1
          and sorted(cloud.pages(D + "tst-0016-01 Net secret")) == ["page_0001.webp"] and code_n == 0)
    check("probe: no instant-upload probe in any run, so no unfinished pre-upload sessions",
          not cloud.calls.get("/upload/v2/file/create"))
    check("fails: every failed attempt is booked as api:kind in the ledger and in the stats",
          (fails_429, fails_verify, fails_download, n15["fails"], n16["fails"])
          == ("upload:429:1", "volume:verify:1", "volume:download:3", "upload:net:3", "upload:net:4")
          and "fail_upload:net=7" in out_n)

    # 20. the raw answer is booked with its api, token-free, names blanked, <= 200 chars
    tok_now = cur_token("main")
    cloud.err_once["/file/mkdir"] = (1, "busy %s at tst-0017-01 Err secret %s" % (tok_now, "x" * 300))
    out_r, _ = shard("--concurrency", "1", lst="t6.csv")
    r17 = rows_of()["tst-0017-01"]
    said = r17["fail_msgs"]
    check("fail_msgs: mkdir code=1 booked with its message, no token, no name, <= 200 chars",
          r17["status"] == "ok" and r17["fails"] == "mkdir:api1:1"
          and said.startswith("mkdir:api1=code=1 busy <tok> at <name> xxx") and tok_now not in said
          and "secret" not in said and len(said) <= len("mkdir:api1=") + 200)

    # 21. code=1 "internal error" on an upload is retried in place like a network hiccup
    sleeps = []
    panmod.SLEEP = sleeps.append
    cloud.internal[("tst-0018-01", "page_0002.webp")] = 2
    shard("--concurrency", "1", lst="t7.csv")
    panmod.SLEEP = lambda s: None
    r18 = rows_of()["tst-0018-01"]
    check("internal: 2 x code=1 internal error on one upload retried in place (2/5 s), ok at attempt 1",
          r18["status"] == "ok" and r18["attempts"] == "1" and r18["fails"] == "upload:api1:2"
          and sleeps[:2] == [2, 5] and rows_of()["tst-0019-01"]["status"] == "ok")

    # 22. hourly resume: the breaker stop in 10. left a 2.5 h cooldown; the schedule
    #     idles in it and resumes after it, runs nothing when all is done, and leaves
    #     a volume that failed in GIVE_UP runs to a manual dispatch
    cool_p = os.path.join(forge_root, *COOL_DIR.split("/"), "main.json")
    os.makedirs(os.path.dirname(cool_p), exist_ok=True)
    cool = json.load(open(cool_p, encoding="utf-8")) if os.path.exists(cool_p) else {}
    auto_p = os.path.join(forge_root, *AUTO_PATH.split("/"))

    def plan_auto(lst):
        with open(auto_p, "w", encoding="utf-8") as f:
            json.dump({"books": "data/webp/" + lst, "shards": 1, "concurrency": 1, "target": "main"}, f)
        return run("plan", "--forge", forge_root)[0]

    os.environ["GITHUB_EVENT_NAME"] = "schedule"
    p_cool = plan_auto("t8.csv")
    with open(cool_p, "w", encoding="utf-8") as f:
        json.dump({"until": 0}, f)
    p_go, p_done = plan_auto("t8.csv"), plan_auto("t7.csv")
    with open(os.path.join(forge_root, *LEDGER_DIR.split("/"), "zz-given-up.csv"), "w", encoding="utf-8",
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_COLS)
        w.writeheader()
        w.writerows([dict(dict.fromkeys(LEDGER_COLS, 0), book_id="tst-0020-01", target="main",
                          status="fail", err="pan_1", fails="", fail_msgs="")] * GIVE_UP)
    p_gone = plan_auto("t8.csv")
    os.environ.pop("GITHUB_EVENT_NAME")
    out_man, _ = run("run", *base("t8.csv"), "--dry-run", "1")
    check("resume: breaker -> 2.5 h cooldown; the schedule idles in it and resumes after it",
          cool.get("why") == "breaker" and cool.get("until", 0) > time.time() + COOLDOWN_S - 3600
          and "go=0" in p_cool and "cooling down" in p_cool and "go=1" in p_go and "books=@auto" in p_go)
    check("resume: nothing left -> go=0; a volume failed in %d runs waits for a manual dispatch" % GIVE_UP,
          "go=0" in p_done and "nothing left" in p_done and "go=0" in p_gone and "todo=1" in out_man)

    # 23. progress issue: body only, never a comment / assignee / @mention (no phone push)
    import types
    sent = []
    sys.modules["gh_issue"] = types.SimpleNamespace(upsert=lambda *a, **k: sent.append((a, k)))
    os.environ.update(FORGE_TOKEN="x", FORGE_REPO="owner/repo")
    run("summary", *base("t7.csv"))
    os.environ.update(FORGE_TOKEN="", FORGE_REPO="")
    sys.modules.pop("gh_issue")
    check("issue: silent body update only (notify=False), no @ and no assignee",
          len(sent) == 1 and sent[0][1].get("notify") is False and "@" not in sent[0][0][4]
          and "assignees" not in sent[0][1])

    # 15. logs never leak titles / paths / ids / tokens
    alllog = "".join(logs)
    check("logs: no titles, paths, ids, tokens",
          not any(s in alllog for s in ("secret", "/SRC", "/DST", "tst-0", "TOKEN-")))

    # 16. ledger has only ids and numbers
    led_txt = "".join(open(fp, encoding="utf-8").read() for fp in
                      glob.glob(os.path.join(forge_root, *LEDGER_DIR.split("/"), "*.csv")))
    check("ledger: no titles or paths", "secret" not in led_txt and "/DST" not in led_txt)

    # 17. direct link: off by default; signature follows the site's rule
    os.environ.update(PAN_UID_GUJI="42", PAN_DIRECT_KEY_GUJI="k")
    off = direct_url("guji", "/a/b.pdf")
    os.environ["WEBP_DIRECT"] = "1"
    u = urllib.parse.urlparse(direct_url("guji", "/a/b.pdf"))
    exp, rnd, uid, h = urllib.parse.parse_qs(u.query)["auth_key"][0].split("-")
    check("direct: off unless WEBP_DIRECT=1; md5(path-exp-rand-uid-key)", off is None and h == hashlib.md5(
        ("/42/a/b.pdf-%s-%s-42-k" % (exp, rnd)).encode()).hexdigest() and u.netloc == "42.cdn.123clouddisk.com")
    for k in ("PAN_UID_GUJI", "PAN_DIRECT_KEY_GUJI", "WEBP_DIRECT"):
        os.environ.pop(k, None)

    # 18. page size: long side = min(scan long side, 3200) (CTO rule, pinned here on
    #     purpose). Each scan is drawn at 72 dpi (1 px per point), so the old fixed
    #     120 dpi render would have enlarged it 1.67x. 4281 px also guards the
    #     resize rounding: int(4281 * 3200 / 4281) is 3199. A 32 px stamp drawn
    #     300 pt wide after the scan must not be taken for the scan, and a page
    #     whose only image is a 1 px spacer renders like a vector page.
    def page_long(w, h, *imgs):                        # imgs: ((px_w, px_h), rect or None = full page)
        doc = fitz.open()
        pg = doc.new_page(width=w, height=h)
        for size, rect in imgs:
            b = io.BytesIO()
            Image.new("RGB", size, (120, 110, 100)).save(b, "png")
            pg.insert_image(rect or pg.rect, stream=b.getvalue())
        out = Image.open(io.BytesIO(pdf_page_webp(doc, 0)))
        doc.close()
        return max(out.size)

    stamp = ((32, 32), fitz.Rect(0, 0, 300, 300))
    check("render: 1000 px scan -> 1000 px page, never enlarged",
          page_long(1000, 600, ((1000, 600), None), stamp) == 1000)
    check("render: 4281 px scan -> 3200 px page", page_long(4281, 2400, ((4281, 2400), None), stamp) == 3200)
    check("render: page with only a 1 px spacer -> %d dpi like a vector page" % PDF_DPI,
          page_long(200, 300, ((1, 1), None)) == 500)

    for name, ok in checks:
        print("%s  %s" % ("PASS" if ok else "FAIL", name))
    bad = [n for n, ok in checks if not ok]
    print("selftest: %d/%d passed" % (len(checks) - len(bad), len(checks)))
    return 1 if bad else 0


# ---------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    m = sub.add_parser("plan")
    m.set_defaults(func=cmd_plan)
    m.add_argument("--forge", required=True, help="private repo checkout dir")
    m.add_argument("--shards", type=int, default=1)
    m.add_argument("--concurrency", type=int, default=2)
    m.add_argument("--target", choices=("main", "guji"), default="main")
    m.add_argument("--dry-run", type=int, default=1)
    m.add_argument("--limit-pages", type=int, default=0)
    for name, fn in (("token", cmd_token), ("run", cmd_run), ("summary", cmd_summary)):
        p = sub.add_parser(name)
        p.set_defaults(func=fn)
        p.add_argument("--forge", required=True, help="private repo checkout dir")
        p.add_argument("--books", required=True, help="data/webp/x.csv, comma-separated ids or @event")
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
        p.add_argument("--stop-min", type=float, default=335,
                       help="stop in-flight books at their next page (job timeout is 350)")
        p.add_argument("--force", action="store_true", help="ignore ok rows in the ledger")
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
