#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trending 算子 —— **月榜全量入库**,不靠关键词猜

立此因(2026-08-04 创始人):
  「你现在还用落后的关键字搜索?那怎么可以,不如**直接看月榜,把所有的搜集到我们的后台**,
    按照那来内部消化!」
  「获取信息,之前不是给了你 GitHub 上一些优秀的应用,那获取的信息效果更好」

关键词搜索的死穴(前一版就栽在这):
  **你只能搜到你想得到的词。想不到的,永远进不了视野。**
  而且 GitHub 仓库搜索是关键词 AND 匹配,写长一点就直接搜不到 ——
  实测 28 条检索式只捞回 13 个仓,还多是垃圾。
  更根本的是:靠模型写检索式 = 把鹰眼的视野押在"它这次发挥得怎么样"上。

月榜的路子不一样:**不需要预先知道要找什么**。
  GitHub Search 支持**纯限定符查询**(一个关键词都不带),实测:
    created:>近30天 stars:>100   → 1124 个   ← 本月新爆款,最有价值的一路
    pushed:>近30天  stars:>500   → 33789 个  ← 老项目里还在快速前进的
  单条查询上限 1000 条,所以**按星段切片**绕过去,每段都能取完。

全量进我们自己的库(gh_repo_pool),**采集端不做任何筛选** ——
  筛选在后台内部消化(adopt.py 的两级筛),这样:
    ① 采集端不丢东西,今天用不上的明天可能用得上;
    ② 筛选判据改了可以**重新消化历史全量**,不用重新采集;
    ③ 两条路线(月榜 / 缺口驱动)的采纳率可以直接对比。

铁律:GitHub Search 用 Actions 自带 GITHUB_TOKEN(零成本、限流 30 次/分);
      本算子**一个 AI 调用都不发**(纯采集,零推理成本)。
"""
import os, sys, json, time, argparse, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
from _ai import d1, q                                       # noqa: E402

# 星段切片 —— 每段的 total 都要压在 1000 以下,否则取不完。
#   实测 created:>30d 各段规模:100..200 最大,>2000 很少。
STAR_BANDS = ["100..200", "200..500", "500..1000", "1000..3000", ">3000"]
# Pushed floor is stars:>500 inside sweep. Same numbers as before; a live
# total above the search window is bisected, the floors are not raised.
PUSHED_STAR_BANDS = ["500..1000", "1000..5000", ">5000"]
# Search API hard window is 10 pages x 100. Not a collector star floor.
GITHUB_SEARCH_WINDOW = 1000
# A range of width 2^64 cannot occur for a star count. Past this, stop and
# record the shortfall instead of recursing on a query that never shrinks.
MAX_BAND_SPLITS = 64


def search_cap(max_pages, per_page):
    """How many hits one query may keep.

    min(max_pages, pages_needed) used to clip a 2500-hit band to 1000.
    sort=stars desc then stores the head and drops the low end, and the
    process still exited 0. The API also will not return past 1000, so a
    bigger max_pages does not widen this.
    """
    pages = int(max_pages)
    size = int(per_page)
    if pages <= 0 or size <= 0:
        return 0
    return min(pages * size, GITHUB_SEARCH_WINDOW)


def parse_star_band(band):
    """'500..1000' -> ('range', 500, 1000); '>5000' -> ('gt', 5000, None);
    '750' -> ('eq', 750, 750). None when the slice cannot be cut.
    """
    s = str(band).strip()
    if s.startswith(">"):
        rest = s[1:]
        if rest.isdigit():
            return ("gt", int(rest), None)
        return None
    if ".." in s:
        left, right = s.split("..", 1)
        if left.isdigit() and right.isdigit():
            return ("range", int(left), int(right))
        return None
    if s.isdigit():
        n = int(s)
        return ("eq", n, n)
    return None


def split_star_band(band):
    """Cut one overflowing slice into two strictly narrower slices.

    GitHub star ranges are inclusive, so the right half starts at mid+1.
    Otherwise the midpoint is fetched twice or a one-star gap is dropped.
    '>N' becomes 'N+1..(N+span)' plus a higher open tail (span=N, or 1
    when N is 0). A single star count returns None: the caller records
    the shortfall instead of keeping the first 1000 and exiting 0.
    """
    parsed = parse_star_band(band)
    if not parsed:
        return None
    kind, lo, hi = parsed
    if kind == "range":
        if hi <= lo:
            return None
        mid = (lo + hi) // 2
        right_lo = mid + 1
        if mid < lo or right_lo > hi:
            return None
        left = "%d..%d" % (lo, mid)
        right = "%d..%d" % (right_lo, hi)
    elif kind == "gt":
        span = lo if lo > 0 else 1
        left_lo = lo + 1
        left_hi = lo + span
        if left_hi < left_lo:
            return None
        left = "%d..%d" % (left_lo, left_hi)
        right = ">%d" % left_hi
    else:
        return None
    if left == band or right == band:
        return None
    return (left, right)


def band_action(total, band, cap, per_page, depth, max_depth):
    """Decide one slice: fetch, split, short, or empty.

    'fetch' is only legal when total <= cap. A band of 2500 must come
    back as ('split', pair) or ('short', total-cap). It must not look
    like a successful 10-page fetch.
    """
    total = int(total or 0)
    cap = int(cap or 0)
    per_page = int(per_page or 0)
    depth = int(depth or 0)
    if total <= 0 or per_page <= 0:
        return ("empty", 0)
    if total > cap:
        parts = split_star_band(band) if depth < int(max_depth) else None
        if parts:
            return ("split", parts)
        missed = total - cap if cap > 0 else total
        return ("short", missed if missed > 0 else 0)
    pages = (total + per_page - 1) // per_page
    return ("fetch", pages)


def exit_code(missed, write_failed=False):
    """0 only when nothing in the sweep was dropped and the write landed.

    The adopt workflow treats exit 0 as a finished month-board. Keeping
    1000 of a 2500-hit band used to take that path. A failed insert that
    returned 0 used to take it too.
    """
    if write_failed:
        return 1
    try:
        n = int(missed or 0)
    except (TypeError, ValueError):
        return 1
    return 1 if n > 0 else 0


def report_payload(rows, saved, pool_total, fresh, gap, write_failed=False):
    """Same four keys as before, plus how many hits the sweep did not keep."""
    missed = 0
    short_bands = []
    if gap:
        missed = int(gap.get("missed") or 0)
        short_bands = list(gap.get("bands") or [])
    return {
        "swept": len(rows) if rows is not None else 0,
        "saved": saved,
        "write_failed": bool(write_failed),
        "pool_total": pool_total,
        "new": fresh,
        "missed": missed,
        "short_bands": short_bands,
    }


def qualify(track, since, band, floor):
    if band == floor:
        return "%s:>%s %s" % (track, since, floor)
    return "%s:>%s stars:%s" % (track, since, band)


def gh(q_str, page=1, per_page=100):
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    url = ("https://api.github.com/search/repositories?q=%s&sort=stars&order=desc"
           "&per_page=%d&page=%d" % (urllib.parse.quote(q_str), per_page, page))
    h = {"Accept": "application/vnd.github+json", "User-Agent": "gufangai-trending"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    for attempt in range(3):
        try:
            j = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=45).read())
            return j.get("total_count", 0), (j.get("items") or [])
        except Exception as e:
            code = getattr(e, "code", 0)
            if code in (403, 429):          # 限流:退避重试,别放弃这一段
                nap = 20 * (attempt + 1)
                print(f"    限流 {code},退避 {nap}s", flush=True)
                time.sleep(nap)
                continue
            print(f"    查询失败({str(e)[:60]}):{q_str[:60]}", flush=True)
            return 0, []
    return 0, []


def ensure_table():
    d1("""CREATE TABLE IF NOT EXISTS gh_repo_pool (
            repo TEXT PRIMARY KEY, url TEXT, description TEXT, stars INTEGER,
            forks INTEGER, lang TEXT, topics TEXT, license TEXT,
            created_at TEXT, pushed_at TEXT, found_by TEXT,
            first_seen INTEGER, last_seen INTEGER)""")


def _row_from_item(it, track, prev):
    name = it.get("full_name")
    if not name:
        return None
    return {
        "repo": name, "url": it.get("html_url"),
        "description": (it.get("description") or "")[:600],
        "stars": it.get("stargazers_count") or 0,
        "forks": it.get("forks_count") or 0,
        "lang": it.get("language") or "",
        "topics": ",".join(it.get("topics") or [])[:300],
        "license": ((it.get("license") or {}) or {}).get("spdx_id") or "",
        "created_at": (it.get("created_at") or "")[:10],
        "pushed_at": (it.get("pushed_at") or "")[:10],
        "found_by": (prev or {}).get("found_by") or f"trending:{track}",
    }


def sweep(days=30, tracks=("created", "pushed"), max_pages=10, per_page=100,
          search=None, sleeper=None, bands_by_track=None):
    """把月榜全量扫下来。返回 ({repo: row}, gap)。

    gap['missed'] counts hits a slice did not store. Overflow is bisected
    until each query fits the search window. A slice that cannot be cut
    (one star count, or the split depth is exhausted) keeps the head it
    can see and records the rest. search/sleeper exist so tests never
    touch GitHub or the 2.2s pause; the default is still gh().
    """
    fn = search if search is not None else gh

    def pause():
        # Injected sleeper stays off the network path. Production still
        # waits between pages: search is limited to 30 requests a minute.
        if sleeper is not None:
            sleeper()
        else:
            time.sleep(2.2)

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    got = {}
    gap = {"missed": 0, "bands": []}
    seen = set()

    def walk(track, band, floor, depth):
        qs = qualify(track, since, band, floor)
        if qs in seen:
            # A split that echoes the parent used to recurse forever, or
            # return the same 1000 rows and call the band done.
            return
        seen.add(qs)
        total, _ = fn(qs, page=1, per_page=1)
        cap = search_cap(max_pages, per_page)
        action, payload = band_action(
            total, band, cap, per_page, depth, MAX_BAND_SPLITS)
        if action == "split":
            print(f"  [{track} {band}] total={total} cap={cap} split {payload[0]} + {payload[1]}",
                  flush=True)
            for sub in payload:
                walk(track, sub, floor, depth + 1)
            return
        if action == "fetch":
            pages = payload
        elif action == "short" and cap and per_page:
            pages = (min(total, cap) + per_page - 1) // per_page
        else:
            pages = 0
        print(f"  [{track} {band}] total={total} → 取 {pages} 页", flush=True)
        got_n = 0
        for p in range(1, pages + 1):
            _, items = fn(qs, page=p, per_page=per_page)
            items = items or []
            got_n += len(items)
            for it in items:
                row = _row_from_item(it, track, got.get(it.get("full_name")))
                if row is None:
                    continue
                got[row["repo"]] = row
            if not items:
                break
            pause()
        if action == "short":
            # total-cap under-counts when a page comes back short. The
            # hole is whatever the query claimed minus what we actually
            # received. Those missing rows are the low-star tail.
            missed = total - got_n
            if missed < 0:
                missed = 0
            if missed:
                gap["missed"] += missed
                gap["bands"].append({
                    "track": track,
                    "band": str(band),
                    "total": total,
                    "kept": got_n,
                    "missed": missed,
                })
                print(f"  [{track} {band}] short total={total} kept={got_n} missed={missed}",
                      flush=True)

    for track in tracks:
        # created = 本月新出现的爆款(最有价值);pushed = 老项目里还在快速前进的
        floor = "stars:>500" if track == "pushed" else "stars:>100"
        if bands_by_track and track in bands_by_track:
            bands = list(bands_by_track[track])
        elif track == "created":
            bands = list(STAR_BANDS)
        else:
            bands = list(PUSHED_STAR_BANDS)
        for band in bands:
            walk(track, band, floor, 0)
    return got, gap


def save(rows, batch=60):
    """全量写进我们自己的后台。**采集端不筛**,筛在内部消化那一步。"""
    ensure_table()
    now = int(time.time())
    vals, n, failed = [], 0, False
    for r in rows.values():
        vals.append("(" + ",".join([
            q(r["repo"]), q(r["url"]), q(r["description"]), str(r["stars"]), str(r["forks"]),
            q(r["lang"]), q(r["topics"]), q(r["license"]), q(r["created_at"]), q(r["pushed_at"]),
            q(r["found_by"]), str(now), str(now)]) + ")")
        if len(vals) >= batch:
            added, bad = _flush(vals, now)
            n += added
            failed = failed or bad
            vals = []
    if vals:
        added, bad = _flush(vals, now)
        n += added
        failed = failed or bad
    return n, failed


def _flush(vals, now):
    try:
        # 老行只更新星数与 last_seen,**first_seen 不动** —— 那是"我们什么时候第一次看见它",
        #   星速要靠这个算,覆盖掉就丢了时间维度。
        d1("INSERT INTO gh_repo_pool (repo,url,description,stars,forks,lang,topics,license,"
           "created_at,pushed_at,found_by,first_seen,last_seen) VALUES " + ",".join(vals) +
           f" ON CONFLICT(repo) DO UPDATE SET stars=excluded.stars, forks=excluded.forks, "
           f"description=excluded.description, topics=excluded.topics, "
           f"pushed_at=excluded.pushed_at, last_seen={now}")
        return len(vals), False
    except Exception as e:
        print(f"  写库失败({len(vals)} 条):{str(e)[:130]}", flush=True)
        return 0, True


def _ensure_delta_cols():
    """确保 gh_repo_pool 上有星速相关列 —— **三次报 400 说明它压根没建上**。

    后果不是"少个字段":adopt.py 想按 star_delta 排序拿新料,列不存在就退回总星排序,
      于是**每天从同一批高星头部捞**,新入库的高星速项目永远轮不到 ——
      本轮精判只剩 3 个、采纳 0,收件箱没新料。这是采集端最后一个堵点。
    ALTER 失败分两种:列已存在(正常,忽略) / 其它错(要明说,不许静默)。
    """
    for col, typ in (("star_delta", "INTEGER"), ("rank_best", "INTEGER"),
                     ("rank_hits", "INTEGER")):
        try:
            d1(f"ALTER TABLE gh_repo_pool ADD COLUMN {col} {typ}")
            print(f"  [表] gh_repo_pool 建列 {col}", flush=True)
        except Exception as e:
            msg = str(e)
            if "duplicate column" in msg.lower():
                continue
            print(f"  [表] 建列 {col} 失败:{msg[:90]}", flush=True)


def main():
    _ensure_delta_cols()
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="回看几天(30=月榜)")
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    print(f"=== Trending 算子 · 月榜全量({a.days} 天)· 零 AI 调用 ===", flush=True)
    rows, gap = sweep(days=a.days, max_pages=a.max_pages)
    n, write_failed = save(rows)
    try:
        tot = d1("SELECT COUNT(*) c FROM gh_repo_pool")[0]["c"]
        fresh = d1(f"SELECT COUNT(*) c FROM gh_repo_pool WHERE first_seen>={int(time.time())-3600}")[0]["c"]
        pool_failed = False
    except Exception as e:
        tot = fresh = None
        pool_failed = True
        print("  [pool] count unread: %s" % str(e)[:80], flush=True)
    missed = gap["missed"]
    tot_show = "unread" if pool_failed else tot
    fresh_show = "unread" if pool_failed else fresh
    print(f"\n=== 本轮扫到 {len(rows)} 个,写库 {n} 条 → 后台累计 {tot_show} 个(本轮新增 {fresh_show}) missed={missed} ===", flush=True)
    for r in sorted(rows.values(), key=lambda x: -x["stars"])[:10]:
        print(f"  {r['stars']:6d}★ {r['repo'][:44]:44s} {r['description'][:50]}", flush=True)
    if a.report:
        json.dump(report_payload(rows, n, tot_show, fresh_show, gap, write_failed),
                  open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return exit_code(missed, write_failed)


if __name__ == "__main__":
    sys.exit(main())
