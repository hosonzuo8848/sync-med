#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Search term table builder (search_terms). The v1 FTS half is retired.

2026-09-26: v1 trigram FTS (books_fts + books_fts_state) is no longer created or
written here. Retrieval switched to books_fts_v2 (bigram, full text) on
2026-08-16; v1 only indexed preview_120 and no reader is left. Do NOT add a
CREATE ... IF NOT EXISTS for it back: once the tables are dropped, that line
would silently rebuild the whole v1 index on the next cron run.

What is left: fill search_terms (exact table for 2-char terms) from
sue_graph_nodes / biocomp_entries / herb_compare. INSERT OR IGNORE on the
composite primary key, so a rerun only adds new rows. Zero R2, zero AI calls.
Runs in GitHub Actions (.github/workflows/search-index.yml).

Usage:
  python scripts/search/build_fts.py            # fill search_terms, then verify
  python scripts/search/build_fts.py --verify   # count only, no writes

Historical rationale (2026-08-03, kept as written; the FTS table it mentions is
the retired v1):

立此因(2026-08-03 实测):
  平台检索**只有向量一条路**,D1 里零 FTS 索引。问「《伤寒论》哪一条讲桂枝去芍药」
  这种带确定字面的查询,只能靠向量去撞 —— 撞不撞得上全看运气,而且撞不上时
  没有任何补救通道。补上 FTS5 之后才谈得上 RRF 融合(见 hybrid.js)。

两条实测结论决定了本脚本的形状(创始人已亲自在 D1 上验过,不是推测):
  · D1 支持 FTS5,`tokenize='trigram'` 可用,3 字查询「去芍药」能命中;
  · **trigram 查 2 字打不中**(trigram 需要 ≥3 字才有一个完整 gram),而中医术语
    大量是 2 字(桂枝/芍药/白术/黄芩)→ 纯 FTS 会漏掉一半查询。
  所以本脚本建**两张东西**:trigram FTS 表兜 ≥3 字的自由文本,
  `search_terms` 术语表兜 2 字术语的精确/前缀匹配。缺任何一张,2 字查询都是黑洞。
"""
import os, sys, time, argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "content_factory"))
from _ai import d1, q          # noqa: E402  复用公共底座,不再抄一份 D1 访问

def d1r(sql, tries=4):
    """d1() 外面套一层重试 —— **不重写访问代码**,只处理瞬时故障。

    立此因(2026-08-03 本地实测):术语灌库跑到第 40 批时 `TimeoutError: read operation
    timed out`,整轮 exit 1。这种偶发在 Actions 里会把一个跑了半小时的 job 直接打掉,
    而重跑是安全的(全部写入都是 INSERT OR IGNORE / 反连接取增量),所以就地退避重试。
    重试仍失败才抛 —— 不吞异常、不假装成功。
    """
    for i in range(tries):
        try:
            return d1(sql)
        except Exception as e:
            if i == tries - 1:
                raise
            wait = 2 ** i
            print(f"  [重试 {i+1}/{tries-1}] {type(e).__name__}: {str(e)[:80]} → {wait}s 后再来", flush=True)
            time.sleep(wait)


TERM_TABLE  = "search_terms"

# sue_graph_nodes.node_kind → 术语表的 kind(中文)。实测分布:
#   herb 41434 / formula 26303 / syndrome 994 / book 783 / concept 30 / dynasty 6 / person 5
KIND_MAP = {
    "herb": "本草", "formula": "方剂", "syndrome": "证候",
    "book": "书名", "concept": "概念", "person": "人物", "dynasty": "朝代",
}
# 朝代(清/明)全是 1 字、书名不是"术语"意义上的检索词 —— 收进来只会污染 2 字兜底。
# 这里明确只收三类真正的中医术语,其余留给 FTS 那一路。
TERM_KINDS = ("herb", "formula", "syndrome")


# ────────────────────────────── 建表 ──────────────────────────────

def ensure_tables():
    """建表一律 IF NOT EXISTS —— 重跑不重建,也绝不 DROP 任何东西。"""
    # v1 books_fts / books_fts_state are intentionally NOT created here (retired 2026-09-26).
    d1r(f"""CREATE TABLE IF NOT EXISTS {TERM_TABLE} (
             term       TEXT NOT NULL,
             kind       TEXT NOT NULL,
             ref_type   TEXT NOT NULL,
             ref_id     TEXT NOT NULL,
             term_len   INTEGER NOT NULL,
             created_at INTEGER,
             PRIMARY KEY (term, ref_type, ref_id))""")
    # 前缀匹配 `term LIKE '桂枝%'` 走得到这个索引(默认 BINARY collation)
    d1r(f"CREATE INDEX IF NOT EXISTS idx_{TERM_TABLE}_term ON {TERM_TABLE}(term)")
    d1r(f"CREATE INDEX IF NOT EXISTS idx_{TERM_TABLE}_kind ON {TERM_TABLE}(kind, term_len)")


# ─────────────────────── Phase 2 · 术语精确表 ───────────────────────

def _clean(term):
    """术语规整:去空白;只收 2 字及以上、不超过 40 字的。

    为什么是 2:trigram 打不中 2 字,这张表存在的全部意义就是兜住它们;
    为什么设 40 上限:label 里混着整句描述,收进来会把前缀匹配拖成噪音。
    """
    t = (term or "").strip().replace("　", "")
    if len(t) < 2 or len(t) > 40:
        return None
    return t


def _push(bucket, term, kind, ref_type, ref_id):
    t = _clean(term)
    if t and ref_id:
        bucket[(t, ref_type, str(ref_id))] = (kind, len(t))


def collect_terms():
    """三个数据源汇总。列名全部按 pragma_table_info 实测,不按习惯猜。"""
    bucket = {}

    off, page = 0, 2000
    while True:
        rows = d1r("SELECT id, node_kind, label FROM sue_graph_nodes "
                  f"WHERE node_kind IN ({', '.join(q(k) for k in TERM_KINDS)}) "
                  f"ORDER BY id LIMIT {page} OFFSET {off}")
        if not rows:
            break
        for r in rows:
            _push(bucket, r.get("label"), KIND_MAP.get(r.get("node_kind"), "其他"),
                  "graph_node", r.get("id"))
        off += page
        print(f"    …sue_graph_nodes 已扫 {off} 行,累计术语 {len(bucket)}", flush=True)

    for r in d1r("SELECT entry_id, kind, name_cn FROM biocomp_entries"):
        _push(bucket, r.get("name_cn"), r.get("kind") or "生物成分", "biocomp", r.get("entry_id"))
    for r in d1r("SELECT herb_id, name_cn FROM herb_compare"):
        _push(bucket, r.get("name_cn"), "本草", "herb_compare", r.get("herb_id"))

    return bucket


def phase_terms(batch):
    t0 = time.time()
    before = d1r(f"SELECT COUNT(*) AS n FROM {TERM_TABLE}")[0]["n"]
    bucket = collect_terms()
    print(f"[术语] 三源汇总去重后 {len(bucket)} 条(表内已有 {before} 条)", flush=True)

    now, items, n = int(time.time()), list(bucket.items()), 0
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        vals = ", ".join(
            f"({q(t)}, {q(kind)}, {q(rt)}, {q(rid)}, {ln}, {now})"
            for (t, rt, rid), (kind, ln) in chunk)
        # OR IGNORE + 复合主键 = 重跑只补新增,既有行一个字节都不动
        d1r(f"INSERT OR IGNORE INTO {TERM_TABLE}(term, kind, ref_type, ref_id, term_len, created_at) "
           f"VALUES {vals}")
        n += len(chunk)
        print(f"  术语批 {i//batch + 1} · 累计提交 {n}/{len(items)} · {time.time()-t0:.1f}s", flush=True)

    after = d1r(f"SELECT COUNT(*) AS n FROM {TERM_TABLE}")[0]["n"]
    el = time.time() - t0
    print(f"[术语] 新增 {after - before} 条,表内共 {after} 条,耗时 {el:.1f}s", flush=True)
    return after - before, after, el


# ───────────────────────────── 对账 ─────────────────────────────

def verify():
    """Count search_terms. Exit 1 (job goes red) if the table is unreadable or empty."""
    try:
        terms = d1r(f"SELECT COUNT(*) AS n FROM {TERM_TABLE}")[0]["n"]
        t2 = d1r(f"SELECT COUNT(*) AS n FROM {TERM_TABLE} WHERE term_len = 2")[0]["n"]
    except Exception as e:
        print(f"[verify] cannot read {TERM_TABLE}: {e}")
        return 1
    print(f"[verify] {TERM_TABLE} rows {terms} (2-char {t2})", flush=True)
    if terms <= 0:
        print(f"[verify] FAIL: {TERM_TABLE} is empty")
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    # 术语行只有 term+两个 id ≈ 80 字节,比 preview_120 短一个量级,批可以大得多
    ap.add_argument("--term-batch", type=int, default=800, help="rows per search_terms insert")
    ap.add_argument("--verify", action="store_true", help="count only, no writes")
    args = ap.parse_args()

    if args.verify:
        sys.exit(verify())

    ensure_tables()
    print(f"[tables] {TERM_TABLE} ready (IF NOT EXISTS)", flush=True)
    phase_terms(args.term_batch)
    sys.exit(verify())


if __name__ == "__main__":
    main()
