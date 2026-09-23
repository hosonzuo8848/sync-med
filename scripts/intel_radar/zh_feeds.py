#!/usr/bin/env python3
"""Eagle-eye Chinese feeds via RSSHub + shared keyword gate (execution order #17/#18, 2026-09-23).

Config: scripts/intel_radar/zh_feeds.json (gate keywords, RSSHub routes, HN sources the Worker gates).
The same gate block is fetched at run time by the CF Worker intel-radar for its HN sources,
so there is ONE keyword list; edit the JSON, not code.

What one run does:
  1. RSSHub routes (enabled only; --probe = every candidate on every base in RSSHUB_BASES)
     -> JSON Feed -> keyword gate -> rows shaped exactly like the Worker's intel_items rows
     -> dedup against D1 (read-only SELECT on text_hash).
  2. HN measure: fetch the HN sources the Worker gates, report before/after gate.
  3. --replay: run the gate over existing intel_items rows of those HN sources (read-only).
  Writes to D1 ONLY when WRITE=1 (repo variable ZH_FEEDS_WRITE, CTO decision). Default is dry-run.

Who sees it when it breaks: step summary + exit 1 when fewer than half the enabled routes return
data -> red run -> workflow_sentry.py opens an Issue.
"""
import os, sys, re, json, hashlib, datetime, html, urllib.request, urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
CFG = json.load(open(os.path.join(HERE, "zh_feeds.json"), encoding="utf-8"))
UA = "SueAI-intel-radar/1.0 (+https://www.gufangai.com; read-only feeds)"


# ---- gate (mirror of gatePass() in workers/intel-radar/src/index.js; keep semantics identical) ----
def _kw_re(kw):
    kw = kw.lower()
    if not kw.isascii():
        return re.compile(re.escape(kw))
    tail = "" if kw.endswith("*") else "(?![a-z0-9])"
    return re.compile("(?<![a-z0-9])" + re.escape(kw.rstrip("*")) + tail)


INCLUDE = [(topic, _kw_re(k)) for topic, kws in CFG["gate"]["include"].items() for k in kws]
EXCLUDE = [_kw_re(k) for k in CFG["gate"]["exclude"]]


def gate(text):
    """Return the first matching topic, or None if the item is dropped."""
    t = (text or "").lower()
    if any(r.search(t) for r in EXCLUDE):
        return None
    for topic, r in INCLUDE:
        if r.search(t):
            return topic
    return None


# ---- fetch / parse ----
def get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def plain(s, n):
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:n]


def rsshub_items(base, route, limit=50):
    sep = "&" if "?" in route else "?"
    j = json.loads(get(base.rstrip("/") + route + sep + "format=json&limit=%d" % limit))
    out = []
    for x in j.get("items") or []:
        desc = plain(x.get("content_html") or x.get("summary") or x.get("content_text"), 300)
        title = plain(x.get("title"), 200) or desc[:80]
        url = x.get("url") or x.get("id") or ""
        if title and url.startswith("http"):
            out.append({"title": title, "url": url, "desc": desc, "published_at": x.get("date_published")})
    return out


def hn_items(url):
    j = json.loads(get(url))
    return [{"title": h.get("title") or "", "desc": (h.get("story_text") or "")[:300]} for h in j.get("hits") or []]


# ---- rows (same columns/ids as the Worker's INSERT so dedup works across writers) ----
TODAY = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
COLS = ("intel_id,sector,title,summary,url,source,published_at,text_hash,relevance_score,importance,"
        "action_flag,related_module,evidence_level,evidence_source,impact,captured_at")


def row(it, src):
    h = hashlib.sha256(it["url"].encode("utf-8")).hexdigest()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    # relevance 0.5 / to_score: the Worker's rescore loop AI-scores to_score rows with leftover budget.
    return [("ii_%s_%s" % (TODAY, h[:8])), src.get("sector", "T"), it["title"], it["desc"][:200], it["url"],
            src["name"], it["published_at"], h, 0.5, "normal", "to_score", None,
            src.get("ev", "S3"), src["name"], "medium", now]


def existing_hashes(hashes):
    from _ai import d1
    seen = set()
    for i in range(0, len(hashes), 90):
        b = hashes[i:i + 90]
        rs = d1("SELECT text_hash FROM intel_items WHERE text_hash IN (%s)" % ",".join("?" * len(b)), b)
        seen.update(r["text_hash"] for r in rs)
    return seen


def main():
    args = set(sys.argv[1:])
    probe, replay = "--probe" in args, "--replay" in args
    write = os.environ.get("WRITE") == "1" and not probe
    bases = [b for b in os.environ.get("RSSHUB_BASES", "http://localhost:1200").split(",") if b]
    report = {"mode": "probe" if probe else ("write" if write else "dry-run"), "rsshub": [], "hn": [], "replay": []}
    S = ["## eagle-eye zh feeds (%s)" % report["mode"], ""]

    # 1. RSSHub
    S += ["| base | source | route | fetched | gate pass | top topic | note |", "|---|---|---|---|---|---|---|"]
    rows, ok_routes, tried = {}, 0, 0
    for src in CFG["rsshub"]:
        if not (probe or src.get("enabled")):
            continue
        for base in (bases if probe else bases[:1]):
            tried += 1
            rec = {"base": base, "name": src["name"], "route": src["route"], "fetched": 0, "passed": 0, "note": ""}
            try:
                items = rsshub_items(base, src["route"])
            except Exception as e:
                items, rec["note"] = [], ("%s" % e)[:90]
            rec["fetched"] = len(items)
            topics = {}
            for it in items:
                tp = gate(it["title"] + " " + it["desc"]) if src.get("gate", True) else "ungated"
                if not tp:
                    continue
                rec["passed"] += 1
                topics[tp] = topics.get(tp, 0) + 1
                if base == bases[0]:
                    rows.setdefault(it["url"], row(it, src))
            if items:
                ok_routes += 1
            rec["topics"] = topics
            rec["sample_pass"] = [i["title"] for i in items if gate(i["title"] + " " + i["desc"])][:3]
            rec["sample_drop"] = [i["title"] for i in items if not gate(i["title"] + " " + i["desc"])][:3]
            report["rsshub"].append(rec)
            top = max(topics, key=topics.get) if topics else "-"
            S.append("| %s | %s | `%s` | %d | %d | %s | %s |" % (base.split("//")[-1], src["name"], src["route"],
                     rec["fetched"], rec["passed"], top, rec["note"].replace("|", "/")))

    # dedup + (optional) write
    new = list(rows.values())
    if new and os.environ.get("D1_API_TOKEN"):
        seen = existing_hashes([r[7] for r in new])
        new = [r for r in new if r[7] not in seen]
    report["would_insert" if not write else "inserted"] = len(new)
    report["sample_ids"] = [(r[0], r[5], r[2][:60]) for r in new[:10]]
    if write and new:
        from _ai import d1
        for r in new:
            d1("INSERT OR IGNORE INTO intel_items (%s) VALUES (%s)" % (COLS, ",".join("?" * 16)), r)
    S += ["", "**after gate, unique urls: %d; not yet in D1 -> %s: %d**" % (
        len(rows), "inserted" if write else "would insert (dry-run)", len(new)), ""]
    for i, s, t in report["sample_ids"]:
        S.append("- `%s` %s | %s" % (i, s, t))

    # 2. HN live measure
    S += ["", "| HN source (Worker-gated) | fetched | gate pass |", "|---|---|---|"]
    for src in CFG["hn_gated"]:
        try:
            its = hn_items(src["url"])
        except Exception as e:
            its = []; S.append("| %s | error %s | - |" % (src["name"], str(e)[:60]))
            continue
        p = [i for i in its if gate(i["title"] + " " + i["desc"])]
        report["hn"].append({"name": src["name"], "fetched": len(its), "passed": len(p),
                             "sample_drop": [i["title"] for i in its if i not in p][:8],
                             "sample_pass": [i["title"] for i in p][:5]})
        S.append("| %s | %d | %d |" % (src["name"], len(its), len(p)))

    # 3. replay gate over historical rows (read-only)
    if replay and os.environ.get("D1_API_TOKEN"):
        from _ai import d1
        names = [s["name"] for s in CFG["hn_gated"]]
        S += ["", "| replay on intel_items | rows | gate pass | pass high | rows high |", "|---|---|---|---|---|"]
        for n in names:
            # title only: stored summary is often the AI's Chinese reason text, not what the Worker gate sees
            rs = d1("SELECT title, importance FROM intel_items WHERE source = ?", [n])
            p = [r for r in rs if gate(r["title"] or "")]
            hi = sum(1 for r in rs if r["importance"] in ("high", "critical"))
            phi = sum(1 for r in p if r["importance"] in ("high", "critical"))
            report["replay"].append({"name": n, "rows": len(rs), "passed": len(p), "rows_high": hi, "pass_high": phi,
                                     "sample_drop": [r["title"] for r in rs if r not in p][:15],
                                     "sample_drop_high": [r["title"] for r in rs if r not in p
                                                          and r["importance"] in ("high", "critical")][:40]})
            S.append("| %s | %d | %d | %d | %d |" % (n, len(rs), len(p), phi, hi))

    out = os.path.join(HERE, "reports")
    os.makedirs(out, exist_ok=True)
    json.dump(report, open(os.path.join(out, "zh_feeds_report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    text = "\n".join(S)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(text + "\n")
    if not probe and tried and ok_routes * 2 < tried:
        print("::error::only %d/%d enabled RSSHub routes returned data" % (ok_routes, tried))
        sys.exit(1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert gate("Show HN: a RAG pipeline over PDFs") == "rag_retrieval"
        assert gate("Drag and drop storage garage") is None          # the HN\u00b7RAG noise class
        assert gate("Fine-tuning Qwen for OCR") == "ai_model"
        assert gate("Bitcoin LLM trading bot") is None               # exclude wins
        assert gate("Human task board for my agents") == "ai_model"  # 40 of 48 dropped high HN rows were agent posts
        assert gate("\u4e2d\u533b\u53e4\u7c4d\u6570\u5b57\u5316") == "tcm_digital"
        assert gate("\u67d0\u516c\u53f8\u53d1\u5e03\u65b0\u6b3e\u624b\u673a") is None
        print("selftest ok")
    else:
        main()
