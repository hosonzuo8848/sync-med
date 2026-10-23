# scan 123 page-image folders and register dir ids into D1 via the narrow sync endpoint.
# MODE=scan     -> enumerate folders under /<root>/GufangP/yaofang, dump CSV, peek one sample folder
# MODE=register -> same enumeration, then POST each (book_id, fileId) to SYNC_URL (idempotent)
import os, sys, csv, time, threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
SYNC_URL = os.environ.get("SYNC_URL", "https://gufangai.com/api/admin/asset/pan-sync")
MODE = os.environ.get("MODE", "scan")
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
# 并发两处,2026-10-23 创始人澄清:买的 123 API 是 20 线程配额,上传线占几个,剩下的给扫描读。
#   SCAN_CONC:扫描 123 的并发(默认 12)。原来是全串行,一页一页翻 file/list,654 万文件要 6 小时。
#   CONC     :扫描完之后往 D1 pan-sync 写入的并发(默认 12,原 6)。
SCAN_CONC = int(os.environ.get("SCAN_CONC", "12") or 12)
CONC = int(os.environ.get("CONC", "12") or 12)
# path segments from drive root; first segment is CJK, kept escaped (public repo opsec)
PATHS = ([p.split(",") for p in os.environ["PAN_PATH"].split(";") if p.strip()]
         if os.environ.get("PAN_PATH") else None)  # None -> auto-discover under GufangP

# book_id 规范：**全仓唯一一份**在 scripts/book_id.py（零依赖，可安全 import）。
# 2026-08-28 收敛：此处原有一份、pan_inventory.py 又抄了一份，
# 两份一旦漂移，清点算出的"缺口"就和真实注册行为对不上，而没人会发现。
import os as _os_bi, sys as _sys_bi
_sys_bi.path.insert(0, _os_bi.path.dirname(_os_bi.path.abspath(__file__)))
from book_id import to_book_id                       # noqa: E402


S = requests.Session()
_tok = {"v": None}
# 2026-10-23 扫描并发化:12 个线程同时读 123,共享的 Session/token 必须线程安全。
#   ① Session 不保证跨线程安全 → 每线程一个(线程池会复用线程,所以仍是长连接复用)
#   ② token 冷启动时 12 个线程会同时发现为空 → 加锁 + 双检,只取一次
_tlock = threading.Lock()
_local = threading.local()


def _sess():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        _local.s = s
    return s


def token():
    v = _tok["v"]
    if v:
        return v
    with _tlock:
        if _tok["v"]:                     # 双检:并发冷启动只打一次 access_token
            return _tok["v"]
        r = _sess().post(PAN + "/api/v1/access_token", headers={"Platform": "open_platform"},
                         json={"clientID": PCID, "clientSecret": PSEC}, timeout=30)
        v = (r.json().get("data") or {}).get("accessToken")
        if not v:
            # 注意:子线程里 sys.exit() 只结束那个线程、不会让整个任务失败 —— 必须抛异常,
            #   由主线程 f.result() 接住后整任务红掉,否则会静默扫出一份空清单。
            raise RuntimeError("access_token failed: " + r.text[:200])
        _tok["v"] = v
        return v


def pan(method, path, body=None):
    for att in range(8):
        h = {"Authorization": "Bearer " + token(), "Platform": "open_platform"}
        try:
            r = _sess().request(method, PAN + path, headers=h, json=body, timeout=60)
            j = r.json()
            if j.get("code") == 0:
                return j
            msg = str(j.get("message", ""))
            if r.status_code == 401 or "token" in msg.lower():
                with _tlock:
                    _tok["v"] = None
        except Exception:
            pass
        time.sleep(min(60, 2 * (2 ** att)))
    return None


def iter_children(parent):
    last = 0
    while True:
        j = pan("GET", f"/api/v2/file/list?parentFileId={parent}&limit=100&lastFileId={last}")
        if not j:
            print(f"list failed at parent={parent} last={last}", flush=True)
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


def main():
    d1map = None
    if MODE == "auto":
        tok = os.environ["ASSET_SYNC_TOKEN"]
        r = requests.get(SYNC_URL + "?mode=map", headers={"X-Asset-Sync-Token": tok}, timeout=180)
        j = r.json() if r.status_code == 200 else {}
        d1map = j.get("map") or (j.get("data") or {}).get("map")
        if d1map is None:
            sys.exit(f"map query failed: http{r.status_code} {str(j)[:150]}")
        print(f"D1 visible books: {len(d1map)}", flush=True)

    # auto-discover scan roots under GufangP when PAN_PATH unset (download line keeps reshaping 123:
    # guji->guji1/guji2, added \u53e4\u65b9webp/\u53e4\u7c4dwebp...). Scan every child dir except transit/PDF/archive.
    global PATHS
    if PATHS is None:
        gp = 0
        for seg in ["\u53e4\u7c4d", "GufangP"]:
            gp = find_child_folder(gp, seg)
            if gp is None:
                sys.exit("GufangP root not found for auto-discover")
        roots = []
        for it in iter_children(gp):
            if it.get("type") != 1:
                continue
            nm = it.get("filename") or ""
            if nm in ("\u8f6c\u79fb", "fodian") or "\u539f\u6863" in nm or "\u5f52\u6863" in nm or "PDF" in nm:
                continue  # skip \u8f6c\u79fb / fodian / *\u539f\u6863* / *\u5f52\u6863* / *PDF*
            roots.append(["\u53e4\u7c4d", "GufangP", nm])
        PATHS = roots
        print("auto-discovered %d roots: %s" % (len(roots), [r[-1] for r in roots]), flush=True)

    # recursive discovery (2026-07-14): book folder = first token resolves in D1 map (or raw known id);
    # unknown folders are category layers -> descend, up to depth 5. Never descend into book folders.
    known = set(d1map.keys()) if d1map is not None else None
    folders, files_seen = [], 0
    unrecognized = []

    import re as _re
    FULL_ID = _re.compile(r"^([a-z]{1,8}\d{2,3}-\d{4}-\d+|(ndl|nijl|osaka)-\d+|fuji-RB\d+|[a-z]{2,8}[_-]?\d{1,6}-\d+)$")
    WORK_ID = _re.compile(r"^[a-z]{1,8}\d{2,3}-\d{4}$")

    def trailing_vol(name):
        m = _re.search(r"(\d+)\s*$", str(name))
        return int(m.group(1)) if m else None

    # ── 并发 BFS(2026-10-23)──────────────────────────────────────────────
    # 语义与原来的递归 walk 逐步对齐,只把「一个目录一个目录排队列」换成「12 个目录同时在飞」:
    #   · process_dir 是纯函数:读一个目录 → 返回 (挂上的, 认不出的, 待继续的子目录, 散文件数)。
    #     它不碰任何共享状态。
    #   · folders / unrecognized / files_seen 只由主线程改写 → 不需要锁。
    def process_dir(item):
        fid, depth, trail, agg = item
        subdirs = []
        nfiles = 0
        for it in iter_children(fid):
            if it.get("type") == 1:
                subdirs.append((it.get("filename"), it.get("fileId")))
            else:
                nfiles += 1
        found, unrec, nxt = [], [], []
        for name, cid in subdirs:
            rid = to_book_id(name)
            if agg is not None:
                # inside aggregate -> volume folder
                if (known is not None and rid in known) or FULL_ID.match(rid):
                    found.append((rid, cid))                          # volume named with full id
                    continue
                v = trailing_vol(name)
                if v is not None:
                    found.append(("%s-%02d" % (agg, v), cid))         # title+volnum -> agg-05
                else:
                    unrec.append(("%s/%s" % (trail, name), cid))
                continue
            # not inside aggregate. ORDER MATTERS: known full-id first, then two-seg reqno as
            # aggregate (WORK before FULL so 'zi300-0074' descends instead of matching FULL's wide arm).
            if known is not None and rid in known:
                found.append((rid, cid))                              # known full id (old flat layout)
            elif WORK_ID.match(rid):
                nxt.append((cid, depth + 1, "%s/%s" % (trail, name), rid))    # book-aggregate -> volumes
            elif FULL_ID.match(rid):
                found.append((rid, cid))                              # full id, scan mode (known=None)
            elif depth < 6:
                nxt.append((cid, depth + 1, "%s/%s" % (trail, name), None))   # category layer
            else:
                unrec.append(("%s/%s" % (trail, name), cid))
        return found, unrec, nxt, nfiles

    def scan_tree(root_fid):
        """并发扫一棵树。返回散文件数;挂上的/认不出的由主线程 extend 进外层列表。"""
        nfiles = 0
        last_bump = len(folders)
        ex = ThreadPoolExecutor(max_workers=SCAN_CONC)
        try:
            futures = {ex.submit(process_dir, (root_fid, 0, "~", None))}
            while futures:
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for f in done:
                    fo, un, nx, nf = f.result()     # worker 抛错(token 失败等)在这里冒出来 → 整任务失败
                    folders.extend(fo)
                    unrecognized.extend(un)
                    nfiles += nf
                    for it in nx:
                        futures.add(ex.submit(process_dir, it))
                    if len(folders) - last_bump >= 2000:
                        print("..%d folders" % len(folders), flush=True)
                        last_bump = len(folders)
        finally:
            ex.shutdown(wait=True)
        return nfiles

    for segs in PATHS:
        cur = 0
        ok = True
        for seg in segs:
            nxt = find_child_folder(cur, seg)
            if nxt is None:
                # tolerate: a path may be renamed/moved while download line reorganizes 123.
                # skip this path (do NOT abort whole run) + print real siblings to locate new layout.
                sibs = [it.get("filename") for it in iter_children(cur) if it.get("type") == 1][:40]
                print("path seg %r not found under parent %s; siblings: %s" % (seg, cur, sibs), flush=True)
                ok = False
                break
            cur = nxt
        if not ok:
            continue
        print(f"path ok ({len(segs)} segs) -> {cur}", flush=True)
        n0 = len(folders)
        files_seen += scan_tree(cur)
        print(f"  subtotal this path: {len(folders) - n0}", flush=True)
    print(f"SCAN total folders={len(folders)} loose_files={files_seen} unrecognized={len(unrecognized)}", flush=True)
    os.makedirs("out", exist_ok=True)
    with open("out/unrecognized.csv", "w", newline="", encoding="utf-8") as f0:
        w0 = csv.writer(f0)
        w0.writerow(["path", "fileId"])
        for row in unrecognized:
            w0.writerow(row)

    os.makedirs("out", exist_ok=True)
    with open("out/pan_scan.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id", "fileId"])
        for bid, fid in folders:
            w.writerow([bid, fid])

    if folders:
        name0, fid0 = folders[0]
        sample = [it.get("filename") for _, it in zip(range(5), iter_children(fid0))]
        print(f"sample folder {name0} ({fid0}) -> {sample}", flush=True)

    if MODE not in ("register", "auto"):
        return

    tok = os.environ["ASSET_SYNC_TOKEN"]
    # dedup same book across paths (a book may exist in both yaofang and guji): last wins,
    # matching final write order, so the next align run sees it as stable instead of flip-flopping
    _d = {}
    for bid, f in folders:
        _d[bid] = (bid, f)
    folders = list(_d.values())
    print(f"after dedup: {len(folders)}", flush=True)
    if d1map is not None:
        # full alignment: 123 is the source of truth. write when D1 lacks pan_dir_id or it differs.
        seen_fids = set()
        todo2 = []
        n_new = n_changed = n_same = n_nod1 = 0
        for bid, f in folders:
            seen_fids.add(str(f))
            if bid not in d1map:
                n_nod1 += 1
                continue
            cur = str(d1map.get(bid) or "")
            if cur == str(f):
                n_same += 1
            elif cur == "":
                n_new += 1
                todo2.append((bid, f))
            else:
                n_changed += 1
                todo2.append((bid, f))
        stale = [b for b, v in d1map.items() if v and str(v) not in seen_fids]
        print(f"align: new={n_new} changed={n_changed} same={n_same} not-in-D1={n_nod1} stale-in-D1={len(stale)}", flush=True)
        os.makedirs("out", exist_ok=True)
        with open("out/stale_pan_in_d1.csv", "w", newline="", encoding="utf-8") as f2:
            w2 = csv.writer(f2)
            w2.writerow(["book_id", "pan_dir_id_not_seen_in_scan"])
            for b in stale:
                w2.writerow([b, d1map[b]])
        folders = todo2
        if not folders:
            print("aligned: nothing to write; exit", flush=True)
            return
    todo = folders[:LIMIT] if LIMIT > 0 else folders
    stats = {"ok": 0, "notfound": 0, "err": 0}
    results = []

    def reg(item):
        bid, fid = item
        for att in range(4):
            try:
                r = requests.post(SYNC_URL, headers={"X-Asset-Sync-Token": tok},
                                  json={"book_id": bid, "table": "books_assets_v2",
                                        "pan_dir_id": str(fid), "frontend_visible": 1}, timeout=30)
                if r.status_code == 200:
                    return bid, fid, "ok"
                if r.status_code == 404:
                    return bid, fid, "notfound"
                if r.status_code in (401, 400):
                    return bid, fid, f"http{r.status_code}"
            except Exception:
                pass
            time.sleep(2 * (att + 1))
        return bid, fid, "err"

    done = 0
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        for res in ex.map(reg, todo):
            results.append(res)
            st = res[2]
            stats["ok" if st == "ok" else ("notfound" if st == "notfound" else "err")] += 1
            done += 1
            if done % 1000 == 0:
                print(f"progress {done}/{len(todo)} {stats}", flush=True)

    print(f"DONE register total={len(todo)} {stats}", flush=True)
    with open("out/pan_register.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id", "fileId", "status"])
        for row in results:
            w.writerow(row)


if __name__ == "__main__":
    main()
