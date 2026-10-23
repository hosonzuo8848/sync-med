# enroll no-record books: read roster from R2, locate their 123 folders, count pages, INSERT via narrow endpoint.
# Roster (R2 _cc/*.json.gz): { "<book_id>": {"t": "<title>", "a": "<author>", "r": "<req_no>"}, ... }
import os, sys, csv, json, time, re
from concurrent.futures import ThreadPoolExecutor
import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
ENROLL_URL = os.environ.get("ENROLL_URL", "https://gufangai.com/api/admin/asset/pan-enroll")
TOKEN = os.environ["ASSET_SYNC_TOKEN"]
ROSTER_FILE = os.environ.get("ROSTER_FILE", "scripts/fuji_roster.json")
SOURCE_ROOT = os.environ.get("SOURCE_ROOT", "fujikawa")
LIBRARY = os.environ.get("LIBRARY", "")
PREFIX = os.environ.get("FOLDER_PREFIX", "")  # only consider 123 folders whose name starts with this
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
# ── CSV 直通模式(2026-10-23)──────────────────────────────────────────────
# 背景:pan-register 扫出的 not-in-D1 缺口里,书和 123 fileId 都已经确定,
# 不需要再靠 roster + 文件夹名匹配。GAP_CSV 指向 {book_id,fileId,book_title,source_root,library,req_no}。
GAP_CSV = os.environ.get("GAP_CSV", "")
CONC = int(os.environ.get("CONC", "8") or 8)
# 与 pan_audit --FIX 同一门槛:页数 < 4 视为未传完/残件,不入档,等下一轮。
MIN_PAGES = int(os.environ.get("MIN_PAGES", "4") or 4)
PAGE_RE = re.compile(r"^page_(\d+)\.webp$")
PATHS = [p.split(",") for p in (os.environ.get("PAN_PATH")
         or "\u53e4\u7c4d,GufangP,yaofang;\u53e4\u7c4d,GufangP,guji").split(";") if p.strip()]

S = requests.Session()
_tok = {"v": None}


def token():
    if _tok["v"]:
        return _tok["v"]
    r = S.post(PAN + "/api/v1/access_token", headers={"Platform": "open_platform"},
               json={"clientID": PCID, "clientSecret": PSEC}, timeout=30)
    _tok["v"] = (r.json().get("data") or {}).get("accessToken")
    if not _tok["v"]:
        sys.exit("access_token failed")
    return _tok["v"]


def pan(path):
    for att in range(8):
        h = {"Authorization": "Bearer " + token(), "Platform": "open_platform"}
        try:
            r = S.get(PAN + path, headers=h, timeout=60)
            j = r.json()
            if j.get("code") == 0:
                return j
            if r.status_code == 401:
                _tok["v"] = None
        except Exception:
            pass
        time.sleep(min(60, 2 * (2 ** att)))
    return None


def iter_children(parent):
    last = 0
    while True:
        j = pan(f"/api/v2/file/list?parentFileId={parent}&limit=100&lastFileId={last}")
        if not j:
            return
        d = j.get("data") or {}
        for it in d.get("fileList") or []:
            if it.get("trashed") in (1, True):
                continue
            yield it
        last = d.get("lastFileId")
        if last in (None, -1, 0, ""):
            return


def find_child_folder(parent, name):
    for it in iter_children(parent):
        if it.get("filename") == name and it.get("type") == 1:
            return it.get("fileId")
    return None


def page_nums(fid):
    """与 pan_audit.page_nums 同口径:只认 page_(\\d+)\\.webp 且 size>0,按页号去重。

    返回 set 或 None。None = list 失败,与「目录空」严格区分 —— 否则 list 出错会被当成
    0 页跳过,缺口静默消失(这正是本轮要修的漏洞类型)。
    """
    nums, last = set(), 0
    while True:
        j = pan(f"/api/v2/file/list?parentFileId={fid}&limit=100&lastFileId={last}")
        if not j:
            return None
        d = j.get("data") or {}
        for it in d.get("fileList") or []:
            if it.get("trashed") in (1, True) or it.get("type") == 1:
                continue
            m = PAGE_RE.match(str(it.get("filename", "")))
            if m and it.get("size"):
                nums.add(int(m.group(1)))
        last = d.get("lastFileId")
        if last in (None, -1, 0, ""):
            return nums


def count_pages(fid):
    nums = page_nums(fid)
    return 0 if nums is None else len(nums)


def should_enroll(pages):
    """未传完/残件不入档。pages=None 表示 list 失败,同样不能入档。"""
    return pages is not None and pages >= MIN_PAGES


def main_from_csv():
    """CSV 直通:book_id / fileId 已由 pan-register 扫定,只需数页 + 建档。

    与 roster 模式的区别:不再按文件夹名去 123 里找书(那要求目录名 == book_id),
    直接用扫描得到的 fileId。书名的来源也不再是 roster,而是上游用 D1 模板推出来的。
    """
    rows = []
    with open(GAP_CSV, encoding="utf-8") as fh:
        for x in csv.DictReader(fh):
            rows.append(x)
    print(f"gap csv: {len(rows)} books, conc={CONC}, min_pages={MIN_PAGES}", flush=True)
    if LIMIT > 0:
        rows = rows[:LIMIT]
        print(f"limited to {len(rows)}", flush=True)

    stats = {"ok": 0, "exists": 0, "empty": 0, "listfail": 0, "bad": 0, "err": 0}
    results = []

    def one(x):
        bid, fid = x["book_id"], x["fileId"]
        nums = page_nums(fid)
        if nums is None:
            return bid, fid, -1, "listfail"
        pages = len(nums)
        if pages < MIN_PAGES:
            return bid, fid, pages, "empty"      # 未传完/残件,不入档,等下一轮
        body = {
            "book_id": bid,
            "book_title": x["book_title"],
            "author": "",
            "req_no": x.get("req_no", ""),
            "library": x.get("library", ""),
            "source_root": x["source_root"],
            "page_count": pages,
            "pan_dir_id": str(fid),
        }
        for att in range(4):
            try:
                r = requests.post(ENROLL_URL, headers={"X-Asset-Sync-Token": TOKEN},
                                  json=body, timeout=30)
                if r.status_code == 200:
                    return bid, fid, pages, "ok"
                if r.status_code == 409:
                    return bid, fid, pages, "exists"   # 已有档案,不覆盖
                if r.status_code == 400:
                    return bid, fid, pages, "bad:" + r.text[:100]
            except Exception:
                pass
            time.sleep(2 * (att + 1))
        return bid, fid, pages, "err"

    done = 0
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        for res in ex.map(one, rows):
            results.append(res)
            st = res[3]
            key = st.split(":", 1)[0] if st.startswith("bad") else st
            stats[key] = stats.get(key, 0) + 1
            done += 1
            if done % 100 == 0:
                print(f"progress {done}/{len(rows)} {stats}", flush=True)

    print(f"DONE csv-enroll total={len(rows)} {stats}", flush=True)
    os.makedirs("out", exist_ok=True)
    with open("out/enroll_result.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id", "fileId", "pages", "status"])
        for row in results:
            w.writerow(row)
    # 明确列出没建成的,避免像之前那样只留一个数字、缺口静默消失
    with open("out/enroll_skipped.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id", "fileId", "pages", "status"])
        for row in results:
            if row[3] not in ("ok", "exists"):
                w.writerow(row)


def main():
    if GAP_CSV:
        return main_from_csv()

    roster = json.load(open(ROSTER_FILE, encoding="utf-8"))
    print(f"roster: {len(roster)} books", flush=True)

    # locate folders across both paths (last wins, same as aligner)
    fmap = {}
    for segs in PATHS:
        cur = 0
        for seg in segs:
            cur = find_child_folder(cur, seg)
            if cur is None:
                sys.exit(f"path segment not found: {seg!r}")
        for it in iter_children(cur):
            if it.get("type") == 1:
                name = it.get("filename")
                if not PREFIX or str(name).startswith(PREFIX):
                    fmap[name] = it.get("fileId")
    print(f"123 candidate folders (prefix={PREFIX!r}): {len(fmap)}", flush=True)

    todo = [(bid, fmap[bid]) for bid in roster if bid in fmap]
    missing_folder = [bid for bid in roster if bid not in fmap]
    print(f"to enroll: {len(todo)}  roster-without-folder: {len(missing_folder)}", flush=True)
    if LIMIT > 0:
        todo = todo[:LIMIT]

    stats = {"ok": 0, "exists": 0, "empty": 0, "err": 0}
    results = []

    def one(item):
        bid, fid = item
        pages = count_pages(fid)
        if pages < 1:
            return bid, fid, 0, "empty"
        meta = roster[bid]
        for att in range(4):
            try:
                r = requests.post(ENROLL_URL, headers={"X-Asset-Sync-Token": TOKEN},
                                  json={"book_id": bid, "book_title": meta.get("t"),
                                        "author": meta.get("a", ""), "req_no": meta.get("r", ""),
                                        "library": meta.get("l", LIBRARY), "source_root": SOURCE_ROOT,
                                        "page_count": pages, "pan_dir_id": str(fid)}, timeout=30)
                if r.status_code == 200:
                    return bid, fid, pages, "ok"
                if r.status_code == 409:
                    return bid, fid, pages, "exists"
                if r.status_code == 400:
                    return bid, fid, pages, "bad:" + r.text[:60]
            except Exception:
                pass
            time.sleep(2 * (att + 1))
        return bid, fid, pages, "err"

    done = 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        for res in ex.map(one, todo):
            results.append(res)
            st = res[3]
            key = st if st in stats else ("err" if st.startswith("bad") else st)
            stats[key] = stats.get(key, 0) + 1
            done += 1
            if done % 100 == 0:
                print(f"progress {done}/{len(todo)} {stats}", flush=True)

    print(f"DONE enroll total={len(todo)} {stats}", flush=True)
    os.makedirs("out", exist_ok=True)
    with open("out/enroll_result.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id", "fileId", "pages", "status"])
        for row in results:
            w.writerow(row)
    with open("out/roster_without_folder.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id"])
        for b in missing_folder:
            w.writerow([b])


if __name__ == "__main__":
    main()
