#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""thumb-coverage: the sentinel that answers "are ALL visible books' covers in the R2 hot layer yet?"
(2026-09-11 01:3x, founder: verify the whole 46K, not 10 pages; and let a job do the checking, not the CTO's context).

Method: draw a random sample per population from D1 (books with / without a 123 thumbnail, visible, overseas +
overseas_guji), head_object each thumbs/{book_id}.webp in R2 (zero LIST), compute coverage, and post the numbers
to the step summary and to ONE living Issue (title fixed, body replaced each run). Who sees it when it breaks:
the Issue is red below TARGET and green at/above it; fleet-watch does not need to know about it.
"""
import io, json, os, sys, time, urllib.request, random
import boto3
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'content_factory'))
from _ai import d1   # single D1 transport (guard_single_source: no hardcoded endpoint copies)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ACC = os.environ["CF_ACCOUNT_ID"]; DB = os.environ["D1_DATABASE_ID"]; TOK = os.environ["D1_API_TOKEN"]
GH = os.environ.get("GH_TOKEN"); REPO = os.environ.get("GITHUB_REPOSITORY", "")
BUCKET = os.environ.get("R2_BUCKET") or "guyaofang-assets"
N = int(os.environ.get("SAMPLE_N") or "200"); TARGET = float(os.environ.get("TARGET_PCT") or "90")
s3 = boto3.client("s3", endpoint_url=os.environ["S_EP"], aws_access_key_id=os.environ["S_AK"],
                  aws_secret_access_key=os.environ["S_SK"], region_name="auto")
TITLE = "\U0001f5bc\ufe0f \u5c01\u9762\u70ed\u5c42\u8986\u76d6\u7387\u54e8\u5175"   # cover hot-layer coverage sentinel


def r2_has(bid):
    try: s3.head_object(Bucket=BUCKET, Key="thumbs/%s.webp" % bid); return True
    except Exception: return False

def gh(method, path, body=None):
    req = urllib.request.Request("https://api.github.com" + path, data=json.dumps(body).encode() if body else None,
                                 method=method, headers={"Authorization": "Bearer " + GH, "Accept": "application/vnd.github+json",
                                                         "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read() or b"null")

def main():
    pops = [("hasthumb", "thumb_done_at IS NOT NULL"), ("nothumb", "thumb_done_at IS NULL")]
    out, tot_n, tot_hit, weighted = [], 0, 0, 0.0
    for key, cond in pops:
        cnt = d1("SELECT COUNT(*) n FROM books_assets_v2 WHERE frontend_visible=1 AND %s AND collection IN ('overseas','overseas_guji')" % cond)[0]["n"]
        rows = d1("SELECT book_id FROM books_assets_v2 WHERE frontend_visible=1 AND %s AND collection IN ('overseas','overseas_guji') ORDER BY RANDOM() LIMIT %d" % (cond, N))
        ids = [r["book_id"] for r in rows]; hit = sum(r2_has(b) for b in ids)
        pct = 100.0 * hit / max(1, len(ids))
        out.append((key, cnt, len(ids), hit, pct)); tot_n += len(ids); tot_hit += hit; weighted += pct * cnt
        print("%s population=%d sample=%d in_r2=%d pct=%.1f" % (key, cnt, len(ids), hit, pct), flush=True)
    total_pop = sum(o[1] for o in out); est = weighted / max(1, total_pop)
    ok = est >= TARGET
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    # Chinese only as escapes (public repo rule: no plain CJK in source)
    h = ["\u65f6\u95f4", "\u4eba\u7fa4", "\u53ef\u89c1\u4e66\u6570", "\u62bd\u6837", "\u5728 R2 \u70ed\u5c42", "\u8986\u76d6\u7387"]
    lines = ["%s %s | %s: **%.1f%%**(\u76ee\u6807 \u2265 %.0f%%)" % ("\u2705" if ok else "\U0001f534", stamp,
             "\u5168\u7ad9\u52a0\u6743\u8986\u76d6\u7387", est, TARGET), "",
             "| %s | %s | %s | %s | %s |" % (h[1], h[2], h[3], h[4], h[5]), "|---|---|---|---|---|"]
    for key, cnt, n, hit, pct in out:
        name = "\u6709 123 \u7f29\u7565\u56fe" if key == "hasthumb" else "\u65e0 123 \u7f29\u7565\u56fe"
        lines.append("| %s | %d | %d | %d | %.1f%% |" % (name, cnt, n, hit, pct))
    lines += ["", "\u65b9\u6cd5: D1 \u968f\u673a\u62bd\u6837 + R2 head_object(\u96f6 LIST); \u4ea7\u7ebf gen-thumbs.yml(nothumb) / gen-thumbs-fast.yml(hasthumb)."]
    body = "\n".join(lines)
    open(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"), "a", encoding="utf-8").write(body + "\n")
    if GH and REPO:
        found = [i for i in gh("GET", "/repos/%s/issues?state=open&per_page=50" % REPO) if i.get("title") == TITLE]
        if found: gh("PATCH", "/repos/%s/issues/%d" % (REPO, found[0]["number"]), {"body": body})
        else: gh("POST", "/repos/%s/issues" % REPO, {"title": TITLE, "body": body})
    print("estimated site-wide coverage %.1f%% target %.0f%% -> %s" % (est, TARGET, "OK" if ok else "BELOW"))

if __name__ == "__main__":
    main()
