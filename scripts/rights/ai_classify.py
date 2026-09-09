#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rights-ai-classify: ask the internal free-pool gateway whether each `unverified` books_text title is a
pre-1912 classic or a modern work. Read-only: D1 is only read, nothing is written; the output is a JSON
artifact plus an Issue summary for the CTO to review before any status change. Chinese strings are
\\u-escaped on purpose (public repo policy)."""
import os, sys, json, time, io, datetime, urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
GW = "https://gufangai.com/api/gateway/chat"
ACC = os.environ["CF_ACCOUNT_ID"]; DB = os.environ["D1_DATABASE_ID"]; TOK = os.environ["D1_API_TOKEN"]
KEY = os.environ.get("GW_KEY", "")
LIMIT = int(os.environ.get("LIMIT") or "400")
SYS = "\u4f60\u662f\u4e2d\u533b\u53e4\u7c4d\u7248\u672c\u76ee\u5f55\u5b66\u4e13\u5bb6\u3002\u7ed9\u5b9a\u4e00\u90e8\u4e66\u7684\u4e66\u540d(\u53ef\u80fd\u5e26\u4f5c\u8005/\u671d\u4ee3\u7ebf\u7d22),\u5224\u65ad\u5b83\u662f\u5426\u4e3a 1912 \u5e74\u4ee5\u524d\u6210\u4e66\u7684\u53e4\u7c4d,\u4ee5\u53ca\u662f\u5426\u4e3a\u73b0\u4ee3\u4eba\u7f16\u5199/\u7f16\u8bd1/\u6574\u7406\u7684\u4f5c\u54c1(\u73b0\u4ee3\u6574\u7406\u672c\u3001\u6559\u6750\u3001\u79d1\u666e\u3001\u8bba\u6587\u96c6\u3001\u73b0\u4ee3\u533b\u5bb6\u533b\u6848\u5747\u7b97\u73b0\u4ee3)\u3002\u53ea\u8f93\u51fa JSON,\u4e0d\u8981\u89e3\u91ca:{\"dynasty\":\"\u671d\u4ee3\u6216 \u73b0\u4ee3\",\"author\":\"\u4f5c\u8005\u6216\u7a7a\",\"year_est\":\u6210\u4e66\u5e74\u4efd\u6574\u6570\u6216null,\"is_pre_1912\":true/false,\"is_modern_work\":true/false,\"confidence\":0\u52301,\"reason\":\"30\u5b57\u5185\u4f9d\u636e\"}"
USER = "\u4e66\u540d:{name}\n\u539f\u59cb\u6587\u4ef6\u540d:{orig}\n\u5df2\u77e5\u4f5c\u8005:{author}\n\u5df2\u77e5\u671d\u4ee3:{dynasty}\n\u5df2\u77e5\u5e74\u4ee3:{year}\n\u5206\u7c7b:{cat}"

def d1(sql):
    url = "https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query" % (ACC, DB)
    req = urllib.request.Request(url, data=json.dumps({"sql": sql}).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
    j = json.loads(urllib.request.urlopen(req, timeout=60).read())
    if not j.get("success"):
        raise RuntimeError(str(j.get("errors"))[:200])
    return j["result"][0]["results"]

def ask(row):
    user = USER.format(name=row["clean_name"] or "", orig=row["original_name"] or "", author=row["author"] or "",
                       dynasty=row["dynasty"] or "", year=row["year_text"] or "", cat=row["category"] or "")
    body = {"messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user}],
            "json": True, "max_tokens": 300, "temperature": 0, "source": "rights_ai"}
    last = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(GW, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json; charset=utf-8", "X-Gateway-Key": KEY,
                                                  "User-Agent": "sync-med-rights-ai/1.0 (GitHub Actions; +https://github.com/hosonzuo8848/sync-med)"})
            j = json.loads(urllib.request.urlopen(req, timeout=120).read())
            if not j.get("ok"):
                last = "gateway ok:false " + str(j.get("error"))[:80]; time.sleep(2); continue
            t = (j.get("text") or "").strip()
            a = t.find("{"); b = t.rfind("}")
            out = json.loads(t[a:b + 1])
            out["provider"] = j.get("provider")
            return out, ""
        except Exception as e:                                   # noqa: BLE001
            last = type(e).__name__ + ": " + str(e)[:80]; time.sleep(2)
    return None, last

def main():
    rows = d1("SELECT text_id, clean_name, original_name, author, dynasty, year_text, category FROM books_text "
              "WHERE rights_status='unverified' ORDER BY text_id LIMIT %d" % LIMIT)
    print("unverified rows:", len(rows), flush=True)
    res = []; failed = 0
    for i, r in enumerate(rows, 1):
        out, err = ask(r)
        rec = dict(r); rec["ai"] = out; rec["err"] = err
        if out is None: failed += 1
        res.append(rec)
        if i % 25 == 0: print("  %d/%d failed=%d" % (i, len(rows), failed), flush=True)
        time.sleep(0.4)
    def sug(rec):
        a = rec["ai"] or {}
        c = float(a.get("confidence") or 0)
        if a.get("is_pre_1912") is True and not a.get("is_modern_work") and c >= 0.8: return "public_domain"
        if a.get("is_modern_work") is True and c >= 0.8: return "internal_reference"
        return "review"
    for rec in res: rec["suggest"] = sug(rec)
    old = sum(1 for x in res if x["suggest"] == "public_domain")
    modern = sum(1 for x in res if x["suggest"] == "internal_reference")
    low = sum(1 for x in res if x["suggest"] == "review")
    json.dump(res, open("rights_ai.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    day = datetime.date.today().isoformat()
    lines = ["\u53ea\u8bfb\u8bca\u65ad,\u672a\u5199 D1\u3002\u5224\u636e:is_pre_1912 \u4e14 confidence>=0.8 \u2192 \u5efa\u8bae public_domain;is_modern_work \u4e14 confidence>=0.8 \u2192 \u5efa\u8bae internal_reference;\u5176\u4f59\u4eba\u5ba1\u3002\u7ed3\u679c\u5168\u91cf\u5728 run artifact rights_ai.json\u3002CTO \u590d\u6838\u540e\u518d\u7528 classify-rights \u843d\u5e93\u3002", "", "| text_id | name | dynasty(ai) | author(ai) | year | pre1912 | modern | conf | suggest | reason |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for x in res:
        a = x["ai"] or {}
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            x["text_id"], (x["clean_name"] or "")[:20], a.get("dynasty", ""), (a.get("author") or "")[:12], a.get("year_est", ""),
            a.get("is_pre_1912", ""), a.get("is_modern_work", ""), a.get("confidence", ""), x["suggest"],
            (a.get("reason") or x["err"] or "")[:40].replace("|", "/")))
    body = "\n".join(lines)
    open("issue.md", "w", encoding="utf-8").write(body)
    title = "\u7248\u6743AI\u5206\u7c7b {day}: {n} \u672c unverified \u00b7 \u7591\u53e4\u7c4d {old} \u00b7 \u7591\u73b0\u4ee3 {modern} \u00b7 \u4f4e\u7f6e\u4fe1 {low}".format(day=day, n=len(res), old=old, modern=modern, low=low)
    print(title); print("failed:", failed)
    open(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"), "a", encoding="utf-8").write("```\n" + title + "\nfailed=%d\n```\n" % failed)
    if os.environ.get("GITHUB_REPOSITORY"):
        import subprocess
        subprocess.run(["gh", "issue", "create", "-R", os.environ["GITHUB_REPOSITORY"], "-t", title, "-F", "issue.md"], check=False)

if __name__ == "__main__":
    main()
