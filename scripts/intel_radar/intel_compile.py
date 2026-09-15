#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""intel compile: turn eagle-eye intel_items into compiled knowledge pages (wiki_pages kind='intel'), the way
jxxy.net/ai compiles every source into one fixed-template page. Blueprint: docs/new/ (intel knowledge base, 2026-09-16).

MODE=facts   candidates = intel_items (importance='high' OR relevance_score>=0.7) AND source<>'bioRxiv' within DAYS,
             fetch full text (r.jina.ai, <=12KB), GitHub metadata/README for repos; write facts_json/body_md/refs,
             status='facts'; skip when source_hash unchanged.
MODE=llm     for status='facts' pages of kind intel (newest first, LIMIT): ask the gateway for six fixed sections as
             structured JSON; tier-1 sentences must carry a verbatim quote found in the fetched text, else downgrade;
             missing section / unknown names / too short -> gated (page stays facts-only).
MODE=digest  top 3 published pages of the last 24h -> one living Issue per day (reader template), links to pages.
Env: CF_ACCOUNT_ID D1_DATABASE_ID D1_API_TOKEN GW_KEY GH_TOKEN GITHUB_REPOSITORY  DAYS=1 LIMIT=40 FORCE=0
Public repo: all Chinese in this file is escaped.
"""
import os, sys, io, json, time, hashlib, re, urllib.request, urllib.error, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'content_factory'))
from _ai import d1, GATEWAY

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
MODE = os.environ.get("MODE") or "facts"; DAYS = int(os.environ.get("DAYS") or "1"); LIMIT = int(os.environ.get("LIMIT") or "40")
FORCE = os.environ.get("FORCE") == "1"; KEY = os.environ.get("GW_KEY", ""); GH = os.environ.get("GH_TOKEN", ""); REPO = os.environ.get("GITHUB_REPOSITORY", "")
PROMPT_VER = "intel_v1"; KIND = "intel"; TEXT_MAX = 12000
qs = lambda v: "'" + str(v).replace("\x00", "").replace("'", "''") + "'"
now = lambda: int(time.time())
UA = "Mozilla/5.0 (compatible; gufang-intel/1; +https://www.gufangai.com)"

# taxonomy: our product lines (escaped)
LINES = {
    "rag":   "\u68c0\u7d22\u4e0eRAG", "graph": "\u77e5\u8bc6\u56fe\u8c31", "pool": "\u514d\u8d39\u6a21\u578b\u6c60\u4e0e\u7f51\u5173",
    "ocr":   "OCR\u4e0e\u53e4\u7c4d\u6570\u5b57\u5316", "geo": "\u524d\u7aef\u4e0eGEO", "tcm": "\u4e2d\u533bAI\u7ade\u54c1",
    "agent": "Agent\u4e0e\u5de5\u4f5c\u6d41", "infra": "\u6570\u636e\u4e0e\u57fa\u5efa", "other": "\u5176\u4ed6",
}
LINE_RULES = [
    ("rag", r"\brag\b|retriev|embedding|vector|rerank|hybrid search|bm25|semantic search|chunk"),
    ("graph", r"knowledge graph|graphrag|hyperedge|ontology|triple|\bkg\b|\u56fe\u8c31"),
    ("pool", r"free tier|free api|openrouter|gateway|rate limit|token price|\u514d\u8d39|\u7f51\u5173|\u964d\u4ef7|pricing"),
    ("ocr", r"\bocr\b|layout|scan|handwrit|\u53e4\u7c4d|\u6587\u732e|digitiz|paddleocr|mineru"),
    ("geo", r"\bseo\b|\bgeo\b|crawler|gptbot|sitemap|structured data|schema\.org|frontend|react|\u524d\u7aef"),
    ("tcm", r"\btcm\b|acupunct|herbal|\u4e2d\u533b|\u9488\u7078|\u65b9\u5242|\u672c\u8349|\u4f24\u5bd2"),
    ("agent", r"\bagent|workflow|tool use|mcp\b|multi-agent|orchestrat|\u5de5\u4f5c\u6d41|\u667a\u80fd\u4f53"),
    ("infra", r"cloudflare|workers|d1\b|sqlite|postgres|github actions|cron|pipeline|\u57fa\u5efa"),
]
SEC = [("conclusion", "\u4e00\u53e5\u8bdd\u7ed3\u8bba"), ("what", "\u5b83\u505a\u4e86\u4ec0\u4e48"), ("meaning", "\u5bf9\u53e4\u65b9AI\u661f\u56fe\u7684\u610f\u4e49"),
       ("copy", "\u53ef\u6284\u7684\u505a\u6cd5"), ("risk", "\u98ce\u9669\u4e0e\u4e0d\u9002\u7528"), ("numbers", "\u6570\u5b57\u8d26")]
TAG1, TAG2, TAG3 = "\u2460", "\u2461", "\u2462"
L_TAG = "\u8ba4\u77e5\u6863\uff1a\u2460\u6709\u51fa\u5904(\u9644\u9010\u5b57\u5f15\u6587) \u2461\u63a8\u6f14 \u2462\u5b58\u7591"
H_SRC = "\u539f\u6587\u4fe1\u606f"; H_META = "\u4e8b\u5b9e\u5c42"; H_EXCERPT = "\u5168\u6587\u6458\u5f55"; H_SUM = "\u7efc\u8ff0"

def sha1(s): return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]
def norm(s): return re.sub(r"\s+", "", s or "")

def http_get(url, timeout=60, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r: return r.read()

def fetch_fulltext(url):
    """r.jina.ai reader (free). returns (text, ok)"""
    try:
        b = http_get("https://r.jina.ai/" + url, timeout=75, headers={"Accept": "text/plain"})
        t = b.decode("utf-8", "replace")
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t[:TEXT_MAX], len(t) > 300
    except Exception as e:                                       # noqa: BLE001
        return "", False

def github_meta(url):
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)", url or "")
    if not m: return None
    full = "%s/%s" % (m.group(1), m.group(2).removesuffix(".git"))
    h = {"Accept": "application/vnd.github+json"}
    if GH: h["Authorization"] = "Bearer " + GH
    try:
        j = json.loads(http_get("https://api.github.com/repos/" + full, timeout=30, headers=h))
        meta = {"repo": full, "stars": j.get("stargazers_count"), "forks": j.get("forks_count"), "license": (j.get("license") or {}).get("spdx_id"),
                "language": j.get("language"), "pushed_at": (j.get("pushed_at") or "")[:10], "created_at": (j.get("created_at") or "")[:10],
                "description": (j.get("description") or "")[:300], "archived": j.get("archived"), "topics": (j.get("topics") or [])[:10]}
        try:
            rd = json.loads(http_get("https://api.github.com/repos/%s/readme" % full, timeout=30, headers=h))
            import base64
            meta["readme"] = base64.b64decode(rd.get("content") or "").decode("utf-8", "replace")[:TEXT_MAX]
        except Exception: meta["readme"] = ""
        return meta
    except Exception as e:                                       # noqa: BLE001
        return {"repo": full, "error": str(e)[:100]}

def rule_tags(text):
    t = (text or "").lower(); out = []
    for k, rx in LINE_RULES:
        if re.search(rx, t, re.I): out.append(k)
    return out[:2] or ["other"]

def candidates():
    rows = d1("SELECT intel_id, title, summary, url, source, published_at, relevance_score, importance, related_module, sector "
              "FROM intel_items WHERE captured_at >= date('now', %s) AND source <> 'bioRxiv' AND url IS NOT NULL AND url <> '' "
              "AND (importance='high' OR relevance_score >= 0.7) ORDER BY relevance_score DESC, captured_at DESC LIMIT %d" % (qs("-%d days" % DAYS), LIMIT * 3))
    return rows

def arxiv_abstract(url):
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", url or "")
    if not m: return ""
    try:
        x = http_get("http://export.arxiv.org/api/query?id_list=" + m.group(1), timeout=30).decode("utf-8", "replace")
        t = re.search(r"<title>(.*?)</title>.*?<summary>(.*?)</summary>", x, re.S)
        return ("Title: %s

%s" % (t.group(1).strip(), re.sub(r"\s+", " ", t.group(2)).strip())) if t else ""
    except Exception: return ""

def build_facts(it):
    text, ok = fetch_fulltext(it["url"])
    if not ok:
        ab = arxiv_abstract(it["url"])
        if ab: text, ok = ab, True
    gh = github_meta(it["url"])
    if gh and gh.get("readme") and len(text) < 600: text = gh["readme"]; ok = True
    f = {"kind": KIND, "intel_id": it["intel_id"], "title": it["title"], "url": it["url"], "source": it["source"], "published_at": it["published_at"],
         "relevance": it["relevance_score"], "importance": it["importance"], "radar_module": it["related_module"], "radar_summary": it["summary"],
         "fulltext_ok": bool(ok), "text": text if ok else "", "github": {k: v for k, v in (gh or {}).items() if k != "readme"} if gh else None,
         "tags": rule_tags((it["title"] or "") + " " + (it["summary"] or "") + " " + (text[:3000] if ok else ""))}
    return f

def render_body(f):
    tags = " / ".join(LINES.get(t, t) for t in f["tags"])
    L = ["# [%s] %s" % (tags, f["title"]), "", L_TAG, "", "## " + H_SRC, "- URL: %s" % f["url"], "- %s: %s" % ("\u6765\u6e90", f["source"]),
         "- %s: %s" % ("\u53d1\u5e03", f.get("published_at") or "-"), "- %s: %s / %s" % ("\u96f7\u8fbe\u8bc4\u5206", f.get("relevance"), f.get("importance"))]
    if f.get("github"):
        g = f["github"]; L += ["", "## " + H_META, "- GitHub: %s  stars %s  forks %s  license %s  lang %s  pushed %s  created %s" % (
            g.get("repo"), g.get("stars"), g.get("forks"), g.get("license"), g.get("language"), g.get("pushed_at"), g.get("created_at"))]
        if g.get("description"): L.append("- " + g["description"])
    L += ["", "## " + H_EXCERPT + (" (nofulltext)" if not f["fulltext_ok"] else "")]
    if f["fulltext_ok"]: L.append(f["text"][:2500].replace("\n", " ") + ("\u2026" if len(f["text"]) > 2500 else ""))
    else: L.append("(%s)" % (f.get("radar_summary") or "-"))
    return "\n".join(L) + "\n"

_BUF = []
def flush_buf():
    if not _BUF: return
    d1(chr(59).join(_BUF)); _BUF.clear()

def upsert(f, body, h):
    pid = "intel:" + f["intel_id"]
    refs = [{"t": "url", "id": f["url"]}] + ([{"t": "repo", "id": f["github"]["repo"]}] if f.get("github") and f["github"].get("repo") else [])
    title = "[%s] %s" % (" / ".join(LINES.get(t, t) for t in f["tags"]), (f["title"] or "")[:160])
    _BUF.append("INSERT INTO wiki_pages (page_id,kind,name_s,title,status,facts_json,body_md,summary_md,refs_json,source_hash,versions_n,books_n,prompt_ver,model,compiled_at) "
                "VALUES (%s,'intel',%s,%s,'facts',%s,%s,NULL,%s,%s,%d,%d,NULL,NULL,%d) "
                "ON CONFLICT(page_id) DO UPDATE SET title=excluded.title, status='facts', facts_json=excluded.facts_json, body_md=excluded.body_md, summary_md=NULL, "
                "refs_json=excluded.refs_json, source_hash=excluded.source_hash, versions_n=excluded.versions_n, compiled_at=excluded.compiled_at"
                % (qs(pid), qs(f["intel_id"]), qs(title), qs(json.dumps(f, ensure_ascii=False)[:48000]), qs(body[:60000]), qs(json.dumps(refs, ensure_ascii=False)), qs(h),
                   int(round(float(f.get("relevance") or 0) * 100)), 1 if f["fulltext_ok"] else 0, now()))
    _BUF.append("DELETE FROM wiki_refs WHERE page_id=%s" % qs(pid))
    _BUF.append("INSERT OR IGNORE INTO wiki_refs (page_id,ref_type,ref_id) VALUES " + ",".join("(%s,%s,%s)" % (qs(pid), qs(r["t"]), qs(r["id"])) for r in refs))
    if len(_BUF) >= 24: flush_buf()

def mode_facts():
    t0 = time.time(); items = candidates(); print("candidates", len(items), "days", DAYS, flush=True)
    ids = [i["intel_id"] for i in items]; existing = {}
    for i in range(0, len(ids), 200):
        for r in d1("SELECT name_s, source_hash FROM wiki_pages WHERE kind='intel' AND name_s IN (%s)" % ",".join(qs(x) for x in ids[i:i + 200])): existing[r["name_s"]] = r["source_hash"]
    made = skipped = nofull = 0
    for it in items:
        if made >= LIMIT: break
        if not FORCE and it["intel_id"] in existing: skipped += 1; continue
        f = build_facts(it)
        h = sha1(json.dumps({k: f[k] for k in ("title", "url", "text", "github")}, ensure_ascii=False, sort_keys=True))
        if not FORCE and existing.get(it["intel_id"]) == h: skipped += 1; continue
        if not f["fulltext_ok"]: nofull += 1
        upsert(f, render_body(f), h); made += 1
        print("  page", it["intel_id"], "tags", f["tags"], "fulltext", f["fulltext_ok"], "text", len(f["text"]), flush=True); time.sleep(0.5)
    flush_buf()
    line = "intel-compile facts days=%d candidates=%d made=%d skip=%d nofulltext=%d elapsed=%ds" % (DAYS, len(items), made, skipped, nofull, time.time() - t0)
    print(line); open(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"), "a", encoding="utf-8").write("```\n" + line + "\n```\n")

# ---------- llm ----------
SYS = ("\u4f60\u662f\u60c5\u62a5\u5206\u6790\u5458\uff0c\u670d\u52a1\u4e00\u4e2a\u4e2d\u533b\u53e4\u7c4d\u6587\u732e\u5e73\u53f0\uff08\u68c0\u7d22/RAG\u3001\u77e5\u8bc6\u56fe\u8c31\u3001\u514d\u8d39\u6a21\u578b\u6c60\u7f51\u5173\u3001OCR\u53e4\u7c4d\u6570\u5b57\u5316\u3001\u524d\u7aefGEO\u3001Agent\u5de5\u4f5c\u6d41\uff09\u3002"
       "\u53ea\u51c6\u6839\u636e\u7ed9\u5b9a\u539f\u6587\u5199\u516d\u6bb5\uff0c\u6bcf\u6bb5 1-3 \u53e5\uff1aconclusion\uff08\u4e00\u53e5\u8bdd\u7ed3\u8bba\uff09what\uff08\u5b83\u505a\u4e86\u4ec0\u4e48\uff09meaning\uff08\u5bf9\u672c\u5e73\u53f0\u7684\u610f\u4e49\uff09copy\uff08\u53ef\u6284\u7684\u505a\u6cd5\uff09risk\uff08\u98ce\u9669\u4e0e\u4e0d\u9002\u7528\uff09numbers\uff08\u6570\u5b57\u8d26\uff0c\u6ca1\u6709\u6570\u5b57\u5c31\u5199\u201c\u539f\u6587\u65e0\u6570\u5b57\u201d\uff09\u3002"
       "\u6bcf\u53e5\u4e00\u6761\uff1atext\uff1btier 1/2/3\uff081=\u76f4\u63a5\u51fa\u81ea\u539f\u6587\uff0c\u5fc5\u987b\u7ed9 quote = \u539f\u6587\u4e2d\u9010\u5b57\u62f7\u8d1d\u7684\u4e00\u5c0f\u6bb5\uff08\u2264 60 \u5b57\uff09\uff1b2=\u63a8\u6f14\uff1b3=\u5b58\u7591\uff09\u3002"
       "\u4e0d\u5199\u539f\u6587\u4e4b\u5916\u7684\u9879\u76ee\u540d\u3001\u6570\u5b57\u548c\u516c\u53f8\u540d\u3002\u53ea\u8f93\u51fa JSON: {\"sections\": [{\"key\": \"conclusion\", \"sentences\": [{\"text\": \"...\", \"tier\": 1, \"quote\": \"...\"}]}, ...]}")

def call_gateway(user):
    body = {"messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user}], "json": True, "max_tokens": 1400, "temperature": 0, "source": "intel_compile", "timeout_ms": 90000}
    req = urllib.request.Request(GATEWAY, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "X-Gateway-Key": KEY, "User-Agent": "intel-compile/1"})
    j = json.loads(urllib.request.urlopen(req, timeout=120).read())
    if not j.get("ok"): raise RuntimeError("gateway ok:false " + str(j.get("error"))[:80])
    txt = (j.get("text") or "").strip(); m = re.search(r"\{.*\}", txt, re.S)
    return (json.loads(m.group(0)).get("sections") if m else None), str(j.get("provider") or j.get("model") or "")

def gate(sections, f):
    if not isinstance(sections, list): return "shape"
    keys = {s.get("key") for s in sections if isinstance(s, dict)}
    if not all(k for k, _ in SEC if k in keys) or len(keys & {k for k, _ in SEC}) < 6: return "missing-section:" + ",".join(sorted({k for k, _ in SEC} - keys))
    body = norm(f.get("text") or "") + norm(f.get("radar_summary") or "") + norm(f.get("title") or "")
    total = 0; downgraded = 0
    for s in sections:
        for x in s.get("sentences") or []:
            if not isinstance(x, dict) or not str(x.get("text", "")).strip(): return "empty-sentence"
            try: tier = int(x.get("tier"))
            except Exception: return "bad-tier"
            if tier not in (1, 2, 3): return "bad-tier"
            total += len(str(x.get("text")))
            if tier == 1:
                q = norm(str(x.get("quote") or ""))
                if len(q) < 4 or q not in body: x["tier"] = 2; x["quote"] = ""; x["downgraded"] = 1; downgraded += 1
            if re.search(r"[\u2460\u2461\u2462]", str(x.get("text"))): return "glyph-in-text"
    if total < 150: return "too-short"
    f["_downgraded"] = downgraded
    return ""

def render_summary(sections):
    out = []; by = {s.get("key"): s for s in sections if isinstance(s, dict)}
    for k, h in SEC:
        s = by.get(k)
        if not s: continue
        parts = []
        for x in s.get("sentences") or []:
            t = re.sub(r"[\u3002\uff01\uff1f\s]+$", "", str(x.get("text", "")).strip()); tier = int(x.get("tier"))
            tag = {1: TAG1, 2: TAG2, 3: TAG3}[tier]
            parts.append(t + tag + ("\u3010%s\u3011" % x.get("quote") if tier == 1 and x.get("quote") else "") + "\u3002")
        out.append("### " + h + "\n" + "".join(parts))
    return "\n\n".join(out)

def mode_llm():
    t0 = time.time()
    rows = d1("SELECT page_id, facts_json, body_md FROM wiki_pages WHERE kind='intel' AND status='facts' AND books_n=1 ORDER BY compiled_at DESC LIMIT %d" % LIMIT)
    print("llm candidates", len(rows), flush=True); ok = gated = failed = 0; down = 0
    for r in rows:
        f = json.loads(r["facts_json"])
        user = json.dumps({"title": f["title"], "url": f["url"], "source": f["source"], "github": f.get("github"), "text": (f.get("text") or "")[:7000]}, ensure_ascii=False)
        try: sections, model = call_gateway(user)
        except Exception as e:                                   # noqa: BLE001
            failed += 1; print("  gw fail", r["page_id"], str(e)[:100], flush=True); time.sleep(3); continue
        why = gate(sections, f)
        if why: gated += 1; print("  gated", r["page_id"], why, flush=True); continue
        down += f.get("_downgraded", 0)
        s = render_summary(sections); body2 = r["body_md"].rstrip("\n") + "\n\n## " + H_SUM + "\n\n" + s + "\n"
        d1("UPDATE wiki_pages SET summary_md=%s, body_md=%s, status='published', prompt_ver=%s, model=%s, summarized_at=%d WHERE page_id=%s"
           % (qs(s), qs(body2[:60000]), qs(PROMPT_VER), qs(model[:80]), now(), qs(r["page_id"])))
        ok += 1; time.sleep(0.5)
    line = "intel-compile llm tried=%d published=%d gated=%d fail=%d downgraded_sentences=%d elapsed=%ds" % (len(rows), ok, gated, failed, down, time.time() - t0)
    print(line); open(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"), "a", encoding="utf-8").write("```\n" + line + "\n```\n")

# ---------- digest ----------
def gh_api(method, path, body=None):
    req = urllib.request.Request("https://api.github.com" + path, data=json.dumps(body).encode() if body else None, method=method,
                                 headers={"Authorization": "Bearer " + GH, "Accept": "application/vnd.github+json", "Content-Type": "application/json", "User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=60).read() or b"null")

def mode_digest():
    rows = d1("SELECT page_id, name_s, title, summary_md, facts_json FROM wiki_pages WHERE kind='intel' AND status='published' AND summarized_at >= %d ORDER BY versions_n DESC, summarized_at DESC LIMIT 3" % (now() - 36 * 3600))
    day = time.strftime("%Y-%m-%d", time.gmtime(now() + 8 * 3600))
    title = "\U0001f985\u9e70\u773c\u65e5\u62a5 %s" % day
    if not rows:
        print("no published intel pages in window"); return
    L = ["\u4eca\u5929 3 \u6761\uff08\u6bcf\u6761\u94fe\u5230\u7f16\u7e82\u9875\uff1b\u2460\u6709\u51fa\u5904 \u2461\u63a8\u6f14 \u2462\u5b58\u7591\uff09", ""]
    for i, r in enumerate(rows, 1):
        f = json.loads(r["facts_json"]); sm = r["summary_md"] or ""
        def sec(k):
            m = re.search(r"### %s\n(.*?)(?:\n\n|$)" % re.escape(dict(SEC)[k]), sm, re.S); return (m.group(1).strip() if m else "-")
        L += ["## %d. %s" % (i, r["title"]),
              "- **%s**\uff1a%s" % ("\u4eca\u5929\u53d1\u751f\u4e86\u4ec0\u4e48", sec("what")),
              "- **%s**\uff1a%s" % ("\u5bf9\u6211\u4eec\u610f\u5473\u7740\u4ec0\u4e48", sec("meaning")),
              "- **%s**\uff1a%s" % ("\u53ef\u4ee5\u8bd5\u4ec0\u4e48", sec("copy")),
              "- **%s**\uff1a%s" % ("\u6ce8\u610f\u4ec0\u4e48\u5751", sec("risk")),
              "- %s\uff1a%s \xb7 \u7f16\u7e82\u9875\uff1ahttps://www.gufangai.com/api/wiki/page?kind=intel&name=%s" % ("\u539f\u6587", f.get("url"), urllib.parse.quote(r["name_s"])), ""]
    body = "\n".join(L)
    open(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"), "a", encoding="utf-8").write(body + "\n")
    if GH and REPO:
        found = [i for i in gh_api("GET", "/repos/%s/issues?state=open&per_page=50" % REPO) if i.get("title") == title]
        if found: gh_api("PATCH", "/repos/%s/issues/%d" % (REPO, found[0]["number"]), {"body": body})
        else: gh_api("POST", "/repos/%s/issues" % REPO, {"title": title, "body": body, "labels": ["intel"]})
    print("digest posted:", title, "items", len(rows))

if __name__ == "__main__":
    {"facts": mode_facts, "llm": mode_llm, "digest": mode_digest}[MODE]()
