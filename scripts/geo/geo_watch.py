#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""geo-watch: weekly "cited by AI" report (Dan Koe's GEO step 4, made measurable with what we log).
Reads page_view_log (bot_name / ref_class added 2026-09-11; no raw UA or referer is stored) for the last 7 days:
  * AI fetches: chatgpt-user / oai-searchbot / claude-searchbot / claude-user / perplexitybot -- these fire when an AI
    answer reads or cites a page (gptbot / claudebot / google-extended are training crawls, listed separately);
  * AI referrals: ref_class in chatgpt / perplexity / claude / gemini / copilot / doubao / kimi / deepseek / yuanbao;
  * top cited pages (paths with the most AI fetches), week-over-week deltas.
Posts ONE living Issue (fixed title, body replaced) + step summary. Google Search Console "AI Overviews" needs OAuth
and is not wired; it stays a manual weekly check noted in the Issue.
"""
import io, json, os, sys, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'content_factory'))
from _ai import d1

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
GH = os.environ.get("GH_TOKEN"); REPO = os.environ.get("GITHUB_REPOSITORY", "")
TITLE = "\U0001f9ed GEO \u88ab\u5f15\u5468\u62a5\uff08AI \u641c\u7d22\u62bd\u53d6\u4e0e\u6765\u8def\uff09"
AI_FETCH = ("chatgpt-user", "oai-searchbot", "claude-searchbot", "claude-user", "perplexitybot")
AI_TRAIN = ("gptbot", "claudebot", "anthropic-ai", "google-extended", "ccbot", "bytespider")
AI_REF = ("chatgpt", "perplexity", "claude", "gemini", "copilot", "doubao", "kimi", "deepseek", "yuanbao")
qs = lambda v: "'" + str(v).replace("'", "''") + "'"
inl = lambda xs: ",".join(qs(x) for x in xs)

def gh(method, path, body=None):
    req = urllib.request.Request("https://api.github.com" + path, data=json.dumps(body).encode() if body else None, method=method,
                                 headers={"Authorization": "Bearer " + GH, "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read() or b"null")

def counts(days_from, days_to):
    w = "created_at >= strftime('%%s','now') - %d*86400 AND created_at < strftime('%%s','now') - %d*86400" % (days_from, days_to)
    fetch = d1("SELECT bot_name, COUNT(*) n FROM page_view_log WHERE %s AND bot_name IN (%s) GROUP BY 1 ORDER BY n DESC" % (w, inl(AI_FETCH)))
    train = d1("SELECT bot_name, COUNT(*) n FROM page_view_log WHERE %s AND bot_name IN (%s) GROUP BY 1 ORDER BY n DESC" % (w, inl(AI_TRAIN)))
    refs = d1("SELECT ref_class, COUNT(*) n FROM page_view_log WHERE %s AND ua_class <> 'bot' AND ref_class IN (%s) GROUP BY 1 ORDER BY n DESC" % (w, inl(AI_REF)))
    top = d1("SELECT path, COUNT(*) n FROM page_view_log WHERE %s AND bot_name IN (%s) GROUP BY 1 ORDER BY n DESC LIMIT 20" % (w, inl(AI_FETCH)))
    humans = d1("SELECT COUNT(*) n FROM page_view_log WHERE %s AND ua_class <> 'bot'" % w)[0]["n"]
    return {"fetch": fetch, "train": train, "refs": refs, "top": top, "humans": humans}

def total(rows): return sum(r["n"] for r in rows)

def main():
    cur, prev = counts(7, 0), counts(14, 7)
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    def delta(a, b): return "%+d" % (a - b)
    L = ["# %s %s" % (TITLE, stamp), "",
         "| \u6307\u6807 | \u672c\u5468 | \u4e0a\u5468 | \u53d8\u5316 |", "|---|---|---|---|",
         "| AI \u56de\u7b54\u65f6\u62bd\u53d6\u672c\u7ad9\u9875\u6570\uff08ChatGPT-User / OAI-SearchBot / Claude-SearchBot / Perplexity\uff09 | %d | %d | %s |" % (total(cur["fetch"]), total(prev["fetch"]), delta(total(cur["fetch"]), total(prev["fetch"]))),
         "| AI \u8bad\u7ec3\u722c\u866b\u6293\u53d6\uff08GPTBot / ClaudeBot / Google-Extended \u7b49\uff09 | %d | %d | %s |" % (total(cur["train"]), total(prev["train"]), delta(total(cur["train"]), total(prev["train"]))),
         "| AI \u9001\u6765\u7684\u771f\u4eba\u8bbf\u95ee\uff08\u6765\u8def = ChatGPT / Perplexity / Claude / Gemini / \u8c46\u5305 / Kimi \u7b49\uff09 | %d | %d | %s |" % (total(cur["refs"]), total(prev["refs"]), delta(total(cur["refs"]), total(prev["refs"]))),
         "| \u771f\u4eba\u603b\u8bbf\u95ee\uff08\u975e bot\uff09 | %d | %d | %s |" % (cur["humans"], prev["humans"], delta(cur["humans"], prev["humans"])),
         "", "## \u6309\u6765\u6e90", ""]
    for k, lab in (("fetch", "AI \u62bd\u53d6"), ("train", "AI \u8bad\u7ec3\u722c\u866b"), ("refs", "AI \u6765\u8def")):
        L.append("- **%s**: " % lab + (", ".join("%s %d" % (r.get("bot_name") or r.get("ref_class"), r["n"]) for r in cur[k]) or "0"))
    L += ["", "## \u88ab AI \u62bd\u53d6\u6700\u591a\u7684\u9875\uff08\u672c\u5468 Top 20\uff09", ""]
    L += ["- `%s` \xd7 %d" % (r["path"], r["n"]) for r in cur["top"]] or ["- \uff08\u672c\u5468\u96f6\u6b21\uff09"]
    L += ["", "## \u8bfb\u6cd5", "",
          "- \u6570\u636e\u6765\u81ea\u672c\u7ad9\u8bbf\u95ee\u65e5\u5fd7\u7684\u7c97\u5206\u7c7b\uff08\u4e0d\u5b58\u539f\u59cb UA / \u6765\u8def\uff09\uff1b\u201cAI \u62bd\u53d6\u201d\u662f AI \u56de\u7b54\u91cc\u53d6\u7528\u672c\u7ad9\u7684\u76f4\u63a5\u8bc1\u636e\uff0c\u201cAI \u6765\u8def\u201d\u662f AI \u628a\u4eba\u9001\u8fdb\u6765\u3002",
          "- Google Search Console \u7684 AI Overviews \u5c55\u793a\u6b21\u6570\u9700\u4eba\u5de5\u767b\u5f55\u67e5\u770b\uff08\u672a\u63a5 API\uff09\uff1b\u624b\u5de5\u5728 ChatGPT / Perplexity \u641c\u201c\u4e94\u82d3\u6563 \u7ec4\u6210 \u53e4\u7c4d\u201d\u7b49\u6838\u5fc3\u8bcd\u770b\u662f\u5426\u5f15\u7528\u672c\u7ad9\u3002",
          "- \u4e0b\u4e00\u6b65\u89c4\u5219\uff1a\u88ab\u62bd\u53d6\u9875\u7684\u5171\u6027\uff08\u9996\u6bb5\u7b54\u6848\u5757\u3001\u95ee\u9898\u5f0f\u6807\u9898\u3001FAQ \u7ed3\u6784\uff09\u590d\u5236\u5230\u5176\u4ed6\u9875\uff1b\u88ab\u6324\u6389\u5219\u56de\u5230\u7b54\u6848\u5757\u4e0e\u6807\u9898\u4f18\u5316\u3002"]
    body = "\n".join(L)
    open(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"), "a", encoding="utf-8").write(body + "\n")
    if GH and REPO:
        found = [i for i in gh("GET", "/repos/%s/issues?state=open&per_page=50" % REPO) if i.get("title") == TITLE]
        if found: gh("PATCH", "/repos/%s/issues/%d" % (REPO, found[0]["number"]), {"body": body})
        else: gh("POST", "/repos/%s/issues" % REPO, {"title": TITLE, "body": body})
    print("geo-watch fetch=%d train=%d refs=%d humans=%d" % (total(cur["fetch"]), total(cur["train"]), total(cur["refs"]), cur["humans"]))

if __name__ == "__main__":
    main()
