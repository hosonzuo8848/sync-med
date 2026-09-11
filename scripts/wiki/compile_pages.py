#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wiki compile: one page per entity (kind + name_s), facts assembled from D1 tables with zero AI, optional grounded
summary via the internal free-pool gateway. Blueprint: docs/new/ blueprint for the compiled-wiki layer (2026-09-11).

Modes (env MODE):
  facts   assemble facts_json / body_md / refs for every candidate name whose source_hash changed (or all with FORCE=1);
          candidates = DISTINCT name_s of live rows in sue_formulas_pub (KIND=formula), optionally BOOK-filtered.
  llm     for pages in status='facts' (oldest first, LIMIT pages), ask the gateway for a summary; three gates:
          every herb / book mentioned must exist in facts; every sentence tagged 1/2/3; tag-1 sentences cite [v:id].
Env: CF_ACCOUNT_ID D1_DATABASE_ID D1_API_TOKEN GW_KEY  MODE=facts|llm KIND=formula BOOK=  LIMIT=400 THREADS=3 FORCE=0
Runs from GitHub Actions (sync-med) or locally; all Chinese text is escaped in this source (public repo rule).
"""
import os, sys, io, json, time, hashlib, re, urllib.request, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'content_factory'))
from _ai import d1, GATEWAY   # single D1 transport + gateway URL (guard_single_source)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
MODE = os.environ.get("MODE") or "facts"; KIND = os.environ.get("KIND") or "formula"
BOOK = os.environ.get("BOOK") or ""; LIMIT = int(os.environ.get("LIMIT") or "400")
FORCE = os.environ.get("FORCE") == "1"; KEY = os.environ.get("GW_KEY", "")
PROMPT_VER = "wiki_summary_v2"; MAX_VERSIONS = 40; SUMMARY_VERSIONS = 12
qs = lambda v: "'" + str(v).replace("\x00", "").replace("'", "''") + "'"
now = lambda: int(time.time())

# ---------- rendering strings (escaped) ----------
H_VERSIONS = "\u5404\u4e66\u7248\u672c"                 #
H_COMP     = "\u7ec4\u6210"                             #
H_IND      = "\u4e3b\u6cbb"                             #
H_QUOTE    = "\u539f\u6587\u6458\u53e5"                 #
H_GRAPH    = "\u56fe\u8c31\u5173\u7cfb"                 #
H_SUM      = "\u7efc\u8ff0"                             #
H_NOTE     = "\u8457\u5f55"                             #
H_MORE     = "\u53e6\u6709 %d \u7248\u672a\u5217\uff0c\u89c1\u4e66\u76ee\uff1a"   #
L_TAG      = "\u8ba4\u77e5\u6863\uff1a\u2460\u6709\u51fa\u5904 \u2461\u6cd5\u5ea6\u63a8\u6f14 \u2462\u5b58\u7591"  #
TAG1, TAG2, TAG3 = "\u2460", "\u2461", "\u2462"

def sha1(s): return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]
def herbs_of(comp):
    if not comp: return []
    try:
        if comp.startswith("["): return [str(x) for x in json.loads(comp) if x]
    except Exception: pass
    return [x for x in re.split(r"[\u3001\uff0c,\uff1b;\s]+", comp) if x]
def herb_name(h): return re.sub(r"[\d\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e24\u9322\u5206\u5347\u5408\u65a4\u679a\u4e2a\u4e2a\u5404\u534a]+$", "", h).strip() or h

# ---------- facts ----------
def candidates():
    where = "COALESCE(is_formula,1)=1 AND dup_of IS NULL AND name_s IS NOT NULL AND name_s<>''"
    if BOOK: where += " AND book_s=%s" % qs(BOOK)
    rows = d1("SELECT DISTINCT name_s FROM sue_formulas_pub WHERE %s" % where)
    return [r["name_s"] for r in rows]

def fetch_versions(names):
    out = {}
    for i in range(0, len(names), 120):
        chunk = names[i:i + 120]
        rows = d1("SELECT formula_id, name, name_s, book, book_s, text_id, vol, composition, indication, substr(quote,1,220) AS q, src_note, src "
                  "FROM sue_formulas_pub WHERE name_s IN (%s) AND COALESCE(is_formula,1)=1 AND dup_of IS NULL ORDER BY book_s, formula_id" % ",".join(qs(n) for n in chunk))
        for r in rows: out.setdefault(r["name_s"], []).append(r)
    return out

def fetch_graph(names):
    out = {}
    for i in range(0, len(names), 150):
        chunk = names[i:i + 150]
        rows = d1("SELECT a.label AS head, b.label AS tail, b.node_kind AS kind, e.edge_kind AS rel, e.provenance_tier AS tier, b.id AS nid "
                  "FROM sue_graph_nodes a JOIN sue_graph_edges e ON e.src_node_id=a.id JOIN sue_graph_nodes b ON b.id=e.dst_node_id "
                  "WHERE a.node_kind='formula' AND a.label IN (%s) ORDER BY (e.provenance_tier='\u539f\u6587\u76f4\u8bc1') DESC, e.weight DESC" % ",".join(qs(n) for n in chunk))
        for r in rows:
            lst = out.setdefault(r["head"], [])
            if len(lst) < 30: lst.append(r)
    return out

def fetch_display(names):
    out = {}
    for i in range(0, len(names), 200):
        chunk = names[i:i + 200]
        for r in d1("SELECT name_s, display_name, book_count FROM kb_formulas WHERE name_s IN (%s)" % ",".join(qs(n) for n in chunk)):
            out[r["name_s"]] = r
    return out

def build_facts(name_s, versions, graph, disp):
    vs = versions[:MAX_VERSIONS]
    facts = {
        "kind": KIND, "name_s": name_s,
        "title": (disp or {}).get("display_name") or (versions[0]["name"] if versions else name_s),
        "versions_total": len(versions), "books": sorted({v["book_s"] or v["book"] for v in versions if (v["book_s"] or v["book"])}),
        "versions": [{"id": v["formula_id"], "name": v["name"], "book": v["book"], "book_s": v["book_s"], "text_id": v["text_id"],
                      "herbs": herbs_of(v["composition"])[:40], "indication": (v["indication"] or "")[:200], "quote": (v["q"] or ""),
                      "note": v["src_note"] or "", "src": v["src"]} for v in vs],
        "graph": [{"tail": g["tail"], "kind": g["kind"], "rel": g["rel"], "tier": g["tier"], "nid": g["nid"]} for g in graph],
    }
    return facts

def render_body(f):
    L = ["# " + f["title"], "", L_TAG, ""]
    L.append("## " + H_VERSIONS + " (%d)" % f["versions_total"])
    L.append("")
    L.append("| # | \u4e66 | \u672c\u4e66\u4f5c | " + H_COMP + " | " + H_IND + " |")
    L.append("|---|---|---|---|---|")
    for i, v in enumerate(f["versions"], 1):
        herbs = "\u3001".join(v["herbs"][:18]) + ("\u2026" if len(v["herbs"]) > 18 else "")
        L.append("| %d | %s | %s | %s | %s |" % (i, v["book"], v["name"], herbs.replace("|", "/"), (v["indication"] or "").replace("|", "/")[:60]))
    if f["versions_total"] > len(f["versions"]):
        L.append(""); L.append(H_MORE % (f["versions_total"] - len(f["versions"])) + "\u3001".join(f["books"]))
    L.append(""); L.append("## " + H_QUOTE)
    for v in f["versions"][:6]:
        if v["quote"]:
            L.append("- \u300a%s\u300b[v:%s] %s%s" % (v["book"], v["id"], v["quote"].replace("\n", " "), ("\uff08" + H_NOTE + "\uff1a" + v["note"] + "\uff09") if v["note"] else ""))
    if f["graph"]:
        L.append(""); L.append("## " + H_GRAPH)
        for g in f["graph"][:24]:
            L.append("- %s \u2192 %s (%s, %s)" % (f["title"], g["tail"], g["rel"], g["tier"] or "-"))
    return "\n".join(L) + "\n"

def refs_of(f):
    refs = [{"t": "formula", "id": v["id"]} for v in f["versions"]]
    refs += [{"t": "text", "id": v["text_id"]} for v in f["versions"] if v.get("text_id")]
    refs += [{"t": "node", "id": g["nid"]} for g in f["graph"] if g.get("nid")]
    seen, out = set(), []
    for r in refs:
        k = (r["t"], r["id"])
        if k in seen: continue
        seen.add(k); out.append(r)
    return out[:200]

_BUF = []
def flush_buf():
    if not _BUF: return
    d1(chr(59).join(_BUF)); _BUF.clear()

def upsert_page(page_id, f, body, refs, h):
    """buffered: ~12 pages per D1 call (3 statements each), so a 25K-page run is ~6K calls instead of 75K."""
    sql = ("INSERT INTO wiki_pages (page_id,kind,name_s,title,status,facts_json,body_md,summary_md,refs_json,source_hash,versions_n,books_n,prompt_ver,model,compiled_at) "
           "VALUES (%s,%s,%s,%s,'facts',%s,%s,NULL,%s,%s,%d,%d,NULL,NULL,%d) "
           "ON CONFLICT(page_id) DO UPDATE SET title=excluded.title, status='facts', facts_json=excluded.facts_json, body_md=excluded.body_md, summary_md=NULL, "
           "refs_json=excluded.refs_json, source_hash=excluded.source_hash, versions_n=excluded.versions_n, books_n=excluded.books_n, compiled_at=excluded.compiled_at"
           % (qs(page_id), qs(KIND), qs(f["name_s"]), qs(f["title"]), qs(json.dumps(f, ensure_ascii=False)[:48000]), qs(body[:60000]),
              qs(json.dumps(refs, ensure_ascii=False)), qs(h), f["versions_total"], len(f["books"]), now()))
    _BUF.append(sql)
    _BUF.append("DELETE FROM wiki_refs WHERE page_id=%s" % qs(page_id))
    vals = ",".join("(%s,%s,%s)" % (qs(page_id), qs(r["t"]), qs(r["id"])) for r in refs[:120])
    if vals: _BUF.append("INSERT OR IGNORE INTO wiki_refs (page_id,ref_type,ref_id) VALUES " + vals)
    if len(_BUF) >= 36 or sum(len(x) for x in _BUF) > 600_000: flush_buf()

def mode_facts():
    t0 = time.time(); names = candidates()
    print("candidates", len(names), "book", BOOK or "(all)", flush=True)
    existing = {}
    for i in range(0, len(names), 300):
        chunk = names[i:i + 300]
        for r in d1("SELECT name_s, source_hash FROM wiki_pages WHERE kind=%s AND name_s IN (%s)" % (qs(KIND), ",".join(qs(n) for n in chunk))):
            existing[r["name_s"]] = r["source_hash"]
    made = skipped = failed = 0
    for i in range(0, len(names), 120):
        chunk = names[i:i + 120]
        versions = fetch_versions(chunk); graph = fetch_graph(chunk); disp = fetch_display(chunk)
        for n in chunk:
            vs = versions.get(n) or []
            if not vs: continue
            f = build_facts(n, vs, graph.get(n) or [], disp.get(n))
            fj = json.dumps(f, ensure_ascii=False, sort_keys=True); h = sha1(fj)
            if not FORCE and existing.get(n) == h: skipped += 1; continue
            try: upsert_page("%s:%s" % (KIND, n), f, render_body(f), refs_of(f), h); made += 1
            except Exception as e:                                   # noqa: BLE001
                failed += 1; print("  fail", n, str(e)[:120], flush=True)
        if (i // 120) % 10 == 0: print("  %d/%d made=%d skip=%d fail=%d %ds" % (min(i + 120, len(names)), len(names), made, skipped, failed, time.time() - t0), flush=True)
        time.sleep(0.3)
    flush_buf()
    line = "wiki-compile facts kind=%s book=%s candidates=%d made=%d skip=%d fail=%d elapsed=%ds" % (KIND, BOOK or "-", len(names), made, skipped, failed, time.time() - t0)
    print(line); open(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"), "a", encoding="utf-8").write("```\n" + line + "\n```\n")

# ---------- llm summary ----------
SYS = ("\u4f60\u662f\u53e4\u7c4d\u65b9\u5242\u7f16\u7e82\u5458\u3002\u53ea\u51c6\u6839\u636e\u7ed9\u5b9a\u4e8b\u5b9e\u5199\u4e00\u6bb5 120-220 \u5b57\u7684\u7efc\u8ff0\uff1a"
       "\u5404\u4e66\u7248\u672c\u5f02\u540c\u3001\u7ec4\u6210\u5171\u6027\u4e0e\u5dee\u5f02\u3001\u4e3b\u6cbb\u8303\u56f4\u3002"
       "\u683c\u5f0f\u786c\u89c4\u5b9a\uff1a\u6bcf\u4e00\u53e5\u7684\u53e5\u53f7\u524d\u5fc5\u987b\u5199\u6863\u4f4d\u7b26\u53f7\uff1a\u2460 = \u6709\u51fa\u5904\uff08\u540e\u9762\u7d27\u8ddf [v:\u65b9\u53f7]\uff0c\u65b9\u53f7\u53ea\u80fd\u662f\u4e8b\u5b9e\u91cc\u7684 id\uff09\uff1b\u2461 = \u6cd5\u5ea6\u63a8\u6f14\uff1b\u2462 = \u5b58\u7591\u3002"
       "\u793a\u4f8b\uff1a\u300a\u5723\u6d4e\u603b\u5f55\u300b\u672c\u65b9\u7531\u8305\u82d3\u3001\u828d\u836f\u3001\u767d\u672f\u3001\u751f\u59dc\u3001\u9644\u5b50\u7ec4\u6210\u2460[v:SJ4_00123]\u3002\u5404\u4e66\u5242\u91cf\u4e92\u6709\u51fa\u5165\u2461\u3002\u662f\u5426\u540c\u6e90\u5c1a\u5f85\u8003\u8bc1\u2462\u3002"
       "\u4e0d\u5199\u8bca\u7597\u5efa\u8bae\uff0c\u4e0d\u5f15\u7528\u4e8b\u5b9e\u4e4b\u5916\u7684\u836f\u540d\u4e0e\u4e66\u540d\u3002\u53ea\u8f93\u51fa JSON: {\"summary\": \"...\"}")

def call_gateway(user):
    body = {"messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user}],
            "json": True, "max_tokens": 700, "temperature": 0, "source": "wiki_compile", "timeout_ms": 90000}
    req = urllib.request.Request(GATEWAY, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "X-Gateway-Key": KEY, "User-Agent": "wiki-compile/1"})
    j = json.loads(urllib.request.urlopen(req, timeout=120).read())
    if not j.get("ok"): raise RuntimeError("gateway ok:false " + str(j.get("error"))[:80])
    txt = (j.get("text") or "").strip(); model = str(j.get("provider") or j.get("model") or "")
    m = re.search(r"\{.*\}", txt, re.S)
    return (json.loads(m.group(0)).get("summary") if m else ""), model

TAG_RE = re.compile(r"(?:[\u2460\u2461\u2462]\s*(?:\[v:[^\]]+\]\s*)*|(?:\[v:[^\]]+\]\s*)+[\u2460\u2461\u2462])\s*$")
def normalize_tags(s):
    """move a tag written after the terminal punctuation back in front of it: X. (1)[v:id] -> X(1)[v:id]."""
    pat = "([" + chr(0x3002) + chr(0xff01) + chr(0xff1f) + "])" + chr(92) + "s*([" + chr(0x2460) + chr(0x2461) + chr(0x2462) + "](?:" + chr(92) + "s*" + chr(92) + "[v:[^" + chr(92) + "]]+" + chr(92) + "])*)"
    return re.sub(pat, lambda m: m.group(2) + m.group(1), s or "")

def gate_summary(s, f):
    s = normalize_tags(s)
    if not s or len(s) < 40: return "empty"
    books = set(f["books"]); ids = {v["id"] for v in f["versions"]}
    sents = [x.strip() for x in re.split(r"(?<=[\u3002\uff01\uff1f])", s) if x.strip()]
    for x in sents:
        core = re.sub(r"[\u3002\uff01\uff1f\s]+$", "", x)
        if not TAG_RE.search(core): return "untagged:" + x[:24]
        if TAG1 in core:
            m = re.findall(r"\[v:([^\]]+)\]", core)
            if not m or any(i not in ids for i in m): return "tag1-no-ref:" + x[:24]
    for b in re.findall(r"\u300a([^\u300b]{2,12})\u300b", s):
        if b not in books and not any(b in bb or bb in b for bb in books): return "book-not-in-facts:" + b
    return ""

def mode_llm():
    t0 = time.time()
    rows = d1("SELECT page_id, facts_json FROM wiki_pages WHERE kind=%s AND status='facts' ORDER BY versions_n DESC, compiled_at ASC LIMIT %d" % (qs(KIND), LIMIT))
    print("llm candidates", len(rows), flush=True)
    ok = gated = failed = 0
    for r in rows:
        f = json.loads(r["facts_json"])
        brief = {"title": f["title"], "books": f["books"], "versions": [{"id": v["id"], "book": v["book"], "herbs": v["herbs"][:20], "indication": v["indication"][:120]} for v in f["versions"][:SUMMARY_VERSIONS]]}
        try:
            s, model = call_gateway(json.dumps(brief, ensure_ascii=False)); s = normalize_tags(s)
        except Exception as e:                                       # noqa: BLE001
            failed += 1; print("  gw fail", r["page_id"], str(e)[:100], flush=True); time.sleep(3); continue
        why = gate_summary(s, f)
        if why:
            gated += 1; print("  gated", r["page_id"], why, flush=True)
            if gated <= 3: print("    raw:", s[:300].replace(chr(10), " "), flush=True)
            continue
        body = d1("SELECT body_md FROM wiki_pages WHERE page_id=%s" % qs(r["page_id"]))[0]["body_md"]
        body2 = body.rstrip("\n") + "\n\n## " + H_SUM + "\n\n" + s + "\n"
        d1("UPDATE wiki_pages SET summary_md=%s, body_md=%s, status='published', prompt_ver=%s, model=%s, summarized_at=%d WHERE page_id=%s"
           % (qs(s), qs(body2[:60000]), qs(PROMPT_VER), qs(model[:80]), now(), qs(r["page_id"])))
        ok += 1
        if ok % 25 == 0: print("  ok=%d gated=%d fail=%d %ds" % (ok, gated, failed, time.time() - t0), flush=True)
    line = "wiki-compile llm kind=%s tried=%d published=%d gated=%d fail=%d elapsed=%ds" % (KIND, len(rows), ok, gated, failed, time.time() - t0)
    print(line); open(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"), "a", encoding="utf-8").write("```\n" + line + "\n```\n")

if __name__ == "__main__":
    {"facts": mode_facts, "llm": mode_llm}[MODE]()
