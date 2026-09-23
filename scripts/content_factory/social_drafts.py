#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Social drafts (S1): Toutiao / Zhihu article drafts from public gufangai.com formula pages.

GENERATE ONLY. Nothing here logs in anywhere or publishes anything; drafts go to --out
(uploaded as a workflow artifact) for the founder to review.

Source: the public cross-book formula API (/api/formulas/detail) behind /fangji/<name>.
  Zero R2, zero D1 writes. The same formula name appears in many classical books with
  different compositions; that side-by-side comparison is the article angle.
Model: internal free-pool gateway only, pinned to one supplier with fallback OFF. Run 1 showed
  supplier=zhipu + fallback lands on agnes (rate-limited, not to be used). Run 2: zhipu_free47
  (shared with herb-norm S3) was already in gateway cooldown -> 503 tried=[]; do not default to it.
  Default zhipu (glm-4-flash); rules are written in Chinese and the body is sectioned with
  per-section lengths because glm-4-flash ignored an English total-length target.
Compliance gate, fail closed:
  1. regex: no dose numerals, no efficacy-promise words (cheap, deterministic)
  2. Jev (founder-approved): diagnosis / prescribing / dosage / efficacy claim /
     literature-as-subject. Jev unavailable -> draft rejected, never passed unchecked.
Rejected drafts are not written; only their reason and scores go into manifest.json.

Public repo: keep this file ASCII-only (non-ASCII text lives in \\u escapes).
"""
import argparse, json, os, random, re, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ai import ask, jev, JEV_STATS, UA  # noqa: E402  shared base, no copies

SITE = "https://www.gufangai.com"

# numeral + dose unit. Output gate leaves out ambiguous units (fen / ge / pian / he: "shifen",
# "baihe" the herb, "san ge banben" would be false positives); Jev catches the rest.
_NUM = "[0-9\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u534a\u4e24\u5169]"
DOSE_GATE = re.compile(_NUM + r"+\s*(?:\u4e24|\u5169|\u94b1|\u9322|\u514b|\u65a4|\u5347|\u679a|\u94e2|\u9296|\u5315|\u6beb\u5347|\u6beb\u514b|g\b|mg\b|ml\b)", re.I)
# material cleaning may be broader: classical quotes use fen as a dose unit ("ge san fen")
DOSE_STRIP = re.compile(_NUM + r"+\s*(?:\u4e24|\u5169|\u94b1|\u9322|\u514b|\u65a4|\u5347|\u679a|\u94e2|\u9296|\u682a|\u5315|\u5206|\u6beb\u5347|\u6beb\u514b)")
PROMISE_GATE = re.compile(r"\u6839\u6cbb|\u5305\u6cbb|\u836f\u5230\u75c5\u9664|\u85e5\u5230\u75c5\u9664|\u6cbb\u6108\u7387|\u6709\u6548\u7387|\u7956\u4f20\u79d8\u65b9|\u795e\u65b9")

FOOTER = ("\n\n---\n\u672c\u6587\u4f9d\u636e\u53e4\u7c4d\u6587\u732e\u6574\u7406\uff0c\u4ec5\u4f9b\u6587\u732e\u7814\u7a76\u4e0e\u4f20\u7edf\u6587\u5316\u4ea4\u6d41\u53c2\u8003\uff0c"
          "\u4e0d\u6784\u6210\u8bca\u65ad\u3001\u5904\u65b9\u6216\u7528\u836f\u5efa\u8bae\uff1b\u5177\u4f53\u8bca\u6cbb\u8bf7\u4ee5\u6267\u4e1a\u533b\u5e08\u9762\u8bca\u4e3a\u51c6\u3002\n"
          "\u300c{name}\u300d\u5728 {books} \u90e8\u53e4\u7c4d\u4e2d\u7684 {total} \u6761\u539f\u6587\u8bb0\u8f7d\u5e76\u6392\u5bf9\u7167\uff1a{url}\n")

SYSTEM = """\u4f60\u662f\u300c\u53e4\u65b9AI\u661f\u56fe\u300d\uff08gufangai.com\uff09\u7684\u53e4\u7c4d\u6587\u732e\u79d1\u666e\u5199\u624b\u3002\u672c\u5e73\u53f0\u662f\u4e2d\u533b\u53e4\u7c4d\u6587\u732e\u7814\u7a76\u53c2\u8003\u5e73\u53f0\uff0c\u4e0d\u662f\u8bca\u7597\u5e73\u53f0\uff0c\u4e0d\u63d0\u4f9b\u4efb\u4f55\u533b\u7597\u5efa\u8bae\u3002

\u3010\u786c\u6027\u7ea2\u7ebf\uff0c\u8fdd\u53cd\u4efb\u4f55\u4e00\u6761\u6574\u7bc7\u4f5c\u5e9f\u3011
1. \u6587\u732e\u662f\u4e00\u5207\u9648\u8ff0\u7684\u4e3b\u8bed\uff1a\u5199\u300c\u300a\u67d0\u4e66\u300b\u8bb0\u8f7d\u2026\u2026\u300d\u300c\u636e\u300a\u67d0\u4e66\u300b\u2026\u2026\u300d\u3002\u4e0d\u5f97\u7528\u81ea\u5df1\u7684\u53e3\u543b\u65ad\u8a00\u67d0\u65b9\u80fd\u6cbb\u4ec0\u4e48\u75c5\u3001\u6709\u4ec0\u4e48\u529f\u6548\u3002
2. \u4e0d\u8bca\u65ad\uff1a\u4e0d\u544a\u8bc9\u8bfb\u8005\u5f97\u4e86\u4ec0\u4e48\u75c5\uff0c\u4e0d\u6559\u8bfb\u8005\u81ea\u6211\u5224\u65ad\u75c5\u60c5\u3002
3. \u4e0d\u5f00\u65b9\uff1a\u4e0d\u5efa\u8bae\u8bfb\u8005\u670d\u7528\u3001\u8bd5\u7528\u3001\u8d2d\u4e70\u3001\u642d\u914d\u4efb\u4f55\u65b9\u5242\u6216\u836f\u6750\uff1b\u4e0d\u5199\u714e\u6cd5\u3001\u670d\u6cd5\u3001\u670d\u7528\u65f6\u95f4\u4e0e\u6b21\u6570\u3002
4. \u4e0d\u5199\u5242\u91cf\uff1a\u4efb\u4f55\u836f\u7269\u7684\u6570\u91cf\u3001\u91cd\u91cf\u3001\u679a\u6570\u4e00\u5f8b\u4e0d\u5199\uff08\u4e0d\u51fa\u73b0\u4e24\u3001\u94b1\u3001\u514b\u3001\u679a\u3001\u5206\u7b49\u5242\u91cf\uff09\uff0c\u5f15\u7528\u539f\u6587\u65f6\u4e5f\u7565\u53bb\u5242\u91cf\u3002
5. \u4e0d\u627f\u8bfa\u7597\u6548\uff1a\u4e0d\u5199\u300c\u6cbb\u6108\u300d\u300c\u6839\u6cbb\u300d\u300c\u6709\u6548\u7387\u300d\u300c\u795e\u6548\u300d\u300c\u79d8\u65b9\u300d\uff0c\u4e0d\u5199\u75c5\u4f8b\u6545\u4e8b\u548c\u60a3\u8005\u89c1\u8bc1\u3002
6. \u53ea\u7528\u3010\u7d20\u6750\u3011\u91cc\u7684\u4e8b\u5b9e\uff1a\u4e0d\u5f97\u7f16\u9020\u4e66\u540d\u3001\u5f15\u6587\u3001\u4f5c\u8005\u3001\u671d\u4ee3\uff1b\u7d20\u6750\u91cc\u6ca1\u6709\u7684\u4e00\u5f8b\u4e0d\u5199\uff0c\u4e5f\u4e0d\u8981\u51ed\u8bb0\u5fc6\u8865\u5145\u7ec4\u6210\u6216\u5242\u91cf\u3002
7. \u6807\u9898\u5438\u5f15\u4eba\u4f46\u4e0d\u5938\u5927\uff1a\u4e0d\u7528\u300c\u9707\u60ca\u300d\u300c\u795e\u65b9\u300d\u300c\u79d8\u65b9\u300d\u300c\u5fc5\u770b\u300d\u7b49\u8bcd\uff0c\u6807\u9898\u4e0d\u51fa\u73b0\u7597\u6548\u3002
8. \u5168\u6587\u7528\u7b80\u4f53\u4e2d\u6587\uff08\u76f4\u63a5\u5f15\u7528\u539f\u6587\u65f6\u53ef\u4fdd\u7559\u539f\u5b57\uff09\uff1b\u7eaf\u6587\u672c\uff0c\u53ef\u7528\u300c## \u300d\u5c0f\u6807\u9898\uff1b\u4e0d\u5199\u94fe\u63a5\u3001\u8bdd\u9898\u6807\u7b7e\u3001\u8868\u60c5\u7b26\u53f7\u2014\u2014\u9875\u9762\u94fe\u63a5\u4f1a\u81ea\u52a8\u9644\u5728\u6587\u672b\u3002

\u3010\u5199\u4f5c\u89d2\u5ea6\u3011\u540c\u4e00\u4e2a\u65b9\u540d\u5728\u591a\u90e8\u53e4\u7c4d\u91cc\u7ec4\u6210\u5404\u4e0d\u76f8\u540c\u3002\u672c\u5e73\u53f0\u628a\u5404\u7248\u672c\u539f\u6587\u5e76\u6392\u9648\u5217\uff0c\u800c\u4e0d\u66ff\u8bfb\u8005\u5408\u6210\u4e00\u4e2a\u300c\u6807\u51c6\u65b9\u300d\u3002\u5e2e\u8bfb\u8005\u770b\u5230\u6587\u732e\u4e4b\u95f4\u7684\u5dee\u5f02\uff0c\u7406\u89e3\u6bd4\u8f83\u7248\u672c\u5bf9\u7814\u8bfb\u53e4\u7c4d\u7684\u610f\u4e49\uff1b\u7ed3\u5c3e\u9080\u8bf7\u8bfb\u8005\u53bb\u67e5\u770b\u539f\u6587\u5bf9\u7167\u3002"""

PLATFORM = {
    "toutiao": ("\u5e73\u53f0\uff1a\u4eca\u65e5\u5934\u6761\u3002\u8f93\u51fa\u683c\u5f0f\uff1a\u7b2c\u4e00\u884c\u53ea\u5199\u6807\u9898\uff08\u4e0d\u8d85\u8fc730\u5b57\uff09\uff0c\u7a7a\u4e00\u884c\u540e\u5199\u6b63\u6587\u3002"
                "\u6b63\u6587\u5fc5\u987b\u52065\u4e2a\u90e8\u5206\uff0c\u6bcf\u90e8\u5206\u4ee5\u300c## \u300d\u5c0f\u6807\u9898\u5f00\u5934\uff0c\u6bcf\u90e8\u5206\u5199200\u5230280\u5b57\uff1a"
                "\u2460\u5f15\u5b50\uff1a\u8fd9\u4e2a\u65b9\u540d\u51fa\u73b0\u5728\u591a\u5c11\u90e8\u53e4\u7c4d\u3001\u591a\u5c11\u6761\u8bb0\u8f7d\u91cc\uff1b\u2461\u7b2c\u4e00\u4e2a\u7248\u672c\uff1a\u51fa\u81ea\u54ea\u672c\u4e66\u3001\u7531\u54ea\u4e9b\u836f\u7ec4\u6210\uff1b"
                "\u2462\u5176\u4ed6\u7248\u672c\u5bf9\u6bd4\uff1a\u53e6\u5916\u4e24\u4e09\u672c\u4e66\u7684\u7ec4\u6210\u6709\u4f55\u4e0d\u540c\uff1b\u2463\u4e3a\u4ec0\u4e48\u4f1a\u540c\u540d\u5f02\u65b9\uff08\u53ea\u4ece\u6587\u732e\u6d41\u4f20\u3001\u4f20\u6284\u3001\u5404\u4e66\u4f53\u4f8b\u7684\u89d2\u5ea6\u8bb2\uff09\uff1b"
                "\u2464\u600e\u6837\u67e5\u770b\u539f\u6587\u5bf9\u7167\u3002\u6b63\u6587\u5408\u8ba1900\u52301300\u5b57\uff08\u786c\u6027\u8303\u56f4800\u52301500\u5b57\uff09\u3002\u8bed\u8a00\u901a\u4fd7\uff0c\u6bb5\u843d\u77ed\u3002", (800, 1500), 30),
    "zhihu": ("\u5e73\u53f0\uff1a\u77e5\u4e4e\u3002\u7528\u56de\u7b54\u4e00\u4e2a\u77e5\u4e4e\u5f0f\u95ee\u9898\u7684\u53e3\u543b\u5199\u3002\u8f93\u51fa\u683c\u5f0f\uff1a\u7b2c\u4e00\u884c\u53ea\u5199\u95ee\u9898\uff08\u4e0d\u8d85\u8fc740\u5b57\uff09\uff0c\u7a7a\u4e00\u884c\u540e\u5199\u56de\u7b54\u3002"
              "\u56de\u7b54\u52064\u52305\u4e2a\u90e8\u5206\uff0c\u6bcf\u90e8\u5206\u4ee5\u300c## \u300d\u5c0f\u6807\u9898\u5f00\u5934\uff0c\u6bcf\u90e8\u5206\u5199200\u5230300\u5b57\uff0c\u5408\u8ba1900\u52301400\u5b57\uff08\u786c\u6027\u8303\u56f4700\u52301800\u5b57\uff09\u3002"
              "\u8bed\u6c14\u51b7\u9759\u3001\u6709\u89c1\u8bc6\uff1b\u7b2c\u4e00\u53e5\u76f4\u63a5\u56de\u7b54\uff1b\u6bcf\u4e2a\u89c2\u70b9\u90fd\u7528\u3010\u7d20\u6750\u3011\u4e2d\u7684\u5177\u4f53\u4e66\u540d\u652f\u6491\uff1b"
              "\u6700\u540e\u7528\u4e00\u5c0f\u6bb5\u8bf4\u660e\u8fd9\u4e9b\u6587\u732e\u80fd\u8bf4\u660e\u4ec0\u4e48\u3001\u4e0d\u80fd\u8bf4\u660e\u4ec0\u4e48\u3002", (700, 1800), 45),
}

HINT = {
    "diagnosis": "\u51fa\u73b0\u4e86\u8bca\u65ad\u6216\u6559\u8bfb\u8005\u5224\u65ad\u75c5\u60c5\u7684\u5185\u5bb9",
    "prescribing": "\u51fa\u73b0\u4e86\u5efa\u8bae\u670d\u7528\u3001\u642d\u914d\u6216\u670d\u6cd5\u714e\u6cd5",
    "dosage": "\u51fa\u73b0\u4e86\u5242\u91cf\u6216\u6570\u91cf",
    "efficacy_claim": "\u7528\u81ea\u5df1\u7684\u53e3\u543b\u65ad\u8a00\u4e86\u7597\u6548\u6216\u4e3b\u6cbb\uff08\u5fc5\u987b\u6539\u6210\u300c\u300a\u67d0\u4e66\u300b\u8bb0\u8f7d\u2026\u2026\u300d\u7684\u6587\u732e\u8f6c\u8ff0\uff09",
    "literature_subject too low": "\u9648\u8ff0\u6ca1\u6709\u843d\u5230\u5177\u4f53\u4e66\u540d\u4e0a",
}
FEEDBACK = "\n\n\u3010\u4e0a\u4e00\u7a3f\u672a\u901a\u8fc7\u3011{why}\u3002\u8bf7\u91cd\u5199\u4e00\u7bc7\uff0c\u9010\u6761\u4fee\u6b63\uff0c\u540c\u65f6\u9075\u5b88\u5168\u90e8\u7ea2\u7ebf\u3002"
LEN_HINT = "\u6b63\u6587{n}\u5b57\uff0c\u4e0d\u5728{lo}\u5230{hi}\u5b57\u8303\u56f4\u5185\uff08\u504f\u77ed\u5c31\u628a\u6bcf\u4e2a\u90e8\u5206\u90fd\u5199\u8db3250\u5b57\u4ee5\u4e0a\uff0c\u504f\u957f\u5c31\u538b\u7f29\uff09"
MAT_NOTE = "\u5f15\u6587\u4e2d\u7684\u5242\u91cf\u5df2\u523b\u610f\u5220\u53bb\uff0c\u4e0d\u8981\u8865\u5199\u3002"
MAT_HEAD = "\n\n\u3010\u7d20\u6750\u3011\uff08JSON\uff09\uff1a\n"

JEV_Q = {
    "diagnosis": "The article diagnoses the reader, or tells readers how to work out which illness they themselves have.",
    "prescribing": "The article suggests the reader take, use, buy or try a formula or herb, or gives preparation or usage instructions for the reader to follow.",
    "dosage": "The article states specific quantities or doses of a medicinal substance.",
    "efficacy_claim": "In its own voice (not attributed to a named classical text), the article asserts that a formula or herb treats, cures or is effective for a condition, or promises results.",
    "literature_subject": "Factual statements about formulas and herbs in the article are attributed to named classical texts.",
}
# ponytail: thresholds are an initial guess with no measured sample yet; raw Jev probabilities
# are kept in manifest.json so they can be recalibrated once reviewed drafts accumulate.
JEV_MAX_VIOLATION = 0.3
JEV_MIN_LITERATURE = 0.5
SUPPLIER = os.environ.get("SOCIAL_SUPPLIER") or "zhipu"
CALL_GAP_S = 5


def get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def pick_formulas(n, seed, min_books=3, max_probe=150):
    xml = get(SITE + "/sitemap-1.xml", 60)
    names = [urllib.parse.unquote(x) for x in re.findall(r"<loc>" + re.escape(SITE) + r"/fangji/([^<]+)</loc>", xml)]
    print("[pick] sitemap formula pages: %d" % len(names), flush=True)
    random.Random(seed).shuffle(names)
    good, i = [], -1
    for i, nm in enumerate(names[:max_probe]):
        try:
            d = json.loads(get(SITE + "/api/formulas/detail?name=" + urllib.parse.quote(nm)))
        except Exception as e:  # noqa: BLE001
            print("[pick] %s fetch error %s" % (nm, str(e)[:80]), flush=True)
            continue
        if d.get("found") and not d.get("fallback") and (d.get("books") or 0) >= min_books and (d.get("with_herbs") or 0) >= 3:
            good.append((nm, d))
            if len(good) >= 2 * n:
                break
        time.sleep(0.3)  # public prod API: stay polite
    print("[pick] probed %d, usable %d" % (i + 1, len(good)), flush=True)
    good.sort(key=lambda x: -(x[1].get("books") or 0))
    return good[:n]


def material(nm, d, max_versions=6):
    seen, vers = set(), []
    for v in d.get("versions") or []:
        b = v.get("book_s") or v.get("book") or ""
        if b in seen or not v.get("herbs"):
            continue
        seen.add(b)
        vers.append({"book": v.get("book") or b,
                     "herbs": [h.get("t") for h in v["herbs"] if h.get("t")],
                     "quote": DOSE_STRIP.sub("", (v.get("quote") or ""))[:220]})
        if len(vers) >= max_versions:
            break
    return {"formula": d.get("display_name") or nm, "books_total": d.get("books"),
            "records_total": d.get("total"),
            "herbs_shared_by_most_versions": d.get("core_keys") or [],
            "most_frequent_herbs": [h.get("t") for h in (d.get("herb_freq") or [])[:12]],
            "sample_versions": vers,
            "note": MAT_NOTE}


def split_title(txt):
    lines = [x.strip() for x in (txt or "").strip().split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    if not lines:
        return "", ""
    title = re.sub(r"^(?:#+\s*|\u6807\u9898[:\uff1a]\s*|\u95ee\u9898[:\uff1a]\s*)+", "", lines[0]).strip().strip("*")
    return title, "\n".join(lines[1:]).strip()


def chars(s):
    return len(re.sub(r"[\s#*>\-]", "", s or ""))


def cheap_gate(title, body, lim, tmax=45):
    why = []
    n = chars(body)
    if not title or len(title) > tmax:
        why.append("title missing or too long")
    if not lim[0] <= n <= lim[1]:
        why.append("body length %d outside %d-%d Chinese characters" % (n, lim[0], lim[1]))
    for rx, tag in ((DOSE_GATE, "dose amount"), (PROMISE_GATE, "efficacy-promise word")):
        m = rx.search(title + "\n" + body)
        if m:
            why.append("%s found: %s" % (tag, m.group(0)))
    if "http" in body:
        why.append("contains a link")
    return why


def hint(w, lim):
    if w.startswith("body length"):
        return LEN_HINT.format(n=w.split()[2], lo=lim[0], hi=lim[1])
    return HINT.get(w, w)


def jev_gate(title, body):
    state = ("Draft article for a Chinese social-media platform, written by a classical-literature research "
             "site that is not a clinic and must not give medical advice. Title and text follow.\n\n"
             + title + "\n\n" + body)
    ans = jev(state, {k: {"type": "noul", "instructions": v} for k, v in JEV_Q.items()}, timeout=60)
    if not ans:
        return None, ["jev unavailable (fail closed)"]
    p = {}
    for k in JEV_Q:
        try:
            p[k] = round(float((ans.get(k) or {}).get("noul")), 3)
        except (TypeError, ValueError):
            return p, ["jev answer missing for " + k]
    why = [k for k in JEV_Q if k != "literature_subject" and p[k] >= JEV_MAX_VIOLATION]
    if p["literature_subject"] < JEV_MIN_LITERATURE:
        why.append("literature_subject too low")
    return p, why


def draft(nm, d, platform, attempts=3):
    spec, lim, tmax = PLATFORM[platform]
    user = spec + MAT_HEAD + json.dumps(material(nm, d), ensure_ascii=False, indent=1)
    rec = {"platform": platform, "attempts": 0, "models": [], "passed": False}
    feedback = ""
    for a in range(attempts):
        rec["attempts"] = a + 1
        time.sleep(CALL_GAP_S)  # shared free key: stay sequential and gapped
        try:
            txt, model = ask(SYSTEM, user + feedback, timeout=180, max_tokens=3200, supplier=SUPPLIER,
                             source="social_drafts", json_mode=False, no_fallback=True, gw_timeout_ms=90000)
        except Exception as e:  # noqa: BLE001
            rec["models"].append("error: " + str(e)[:160])
            continue
        rec["models"].append(model)
        title, body = split_title(txt)
        why = cheap_gate(title, body, lim, tmax)
        p = None
        if not why:
            p, why = jev_gate(title, body)
        rec.update({"reject_reason": why, "jev": p, "chars": chars(body)})
        if not why:
            rec.update({"passed": True, "title": title, "body": body, "model": model})
            return rec
        print("  [%s] attempt %d rejected: %s" % (platform, a + 1, "; ".join(why)[:200]), flush=True)
        feedback = FEEDBACK.format(why="; ".join(hint(w, lim) for w in why))
    return rec


def selftest():
    assert DOSE_GATE.search("\u6842\u679d\u4e09\u4e24") and DOSE_GATE.search("\u6bcf\u6b2110g") and DOSE_GATE.search("\u4e00\u94b1\u5315")
    assert not DOSE_GATE.search("\u4e24\u672c\u4e66\u8bb0\u8f7d\u4e0d\u540c") and not DOSE_GATE.search("\u767e\u5408") and not DOSE_GATE.search("\u5341\u5206\u91cd\u8981")
    assert not DOSE_GATE.search("\u4e09\u4e2a\u7248\u672c") and DOSE_STRIP.sub("", "\u832f\u82d3\u5404\u4e09\u5206\u732a\u82d3\u5341\u516b\u682a") == "\u832f\u82d3\u5404\u732a\u82d3"
    assert PROMISE_GATE.search("\u6b64\u65b9\u53ef\u6839\u6cbb") and not PROMISE_GATE.search("\u300a\u5343\u91d1\u65b9\u300b\u8bb0\u8f7d")
    assert split_title("# \u6807\u9898\uff1a\u4e94\u82d3\u6563\n\n\u6b63\u6587")[0] == "\u4e94\u82d3\u6563"
    assert cheap_gate("t", "x" * 900, (800, 1500)) == [] and cheap_gate("t", "x" * 10, (800, 1500))
    assert "639" in hint("body length 639 outside 800-1500 Chinese characters", (800, 1500))
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--seed", default="")
    ap.add_argument("--out", default="out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not os.environ.get("JEV_API_KEY"):
        sys.exit("JEV_API_KEY missing: compliance gate cannot run, refusing to generate")
    os.makedirs(a.out, exist_ok=True)
    seed = a.seed or str(int(time.time()))
    picks = pick_formulas(a.count, seed)
    manifest = {"seed": seed, "requested": a.count, "picked": len(picks), "drafts": []}
    for i, (nm, d) in enumerate(picks, 1):
        url = SITE + "/fangji/" + urllib.parse.quote(nm)
        print("[%d/%d] %s books=%s total=%s" % (i, len(picks), nm, d.get("books"), d.get("total")), flush=True)
        for platform in PLATFORM:
            r = draft(nm, d, platform)
            r.update({"n": i, "formula": nm, "url": url, "books": d.get("books"), "records": d.get("total")})
            if r["passed"]:
                fn = "%02d_%s.md" % (i, platform)
                footer = FOOTER.format(name=nm, books=d.get("books"), total=d.get("total"), url=url)
                with open(os.path.join(a.out, fn), "w", encoding="utf-8") as f:
                    f.write("# " + r["title"] + "\n\n" + r.pop("body") + footer)
                r["file"] = fn
                assert url in open(os.path.join(a.out, fn), encoding="utf-8").read()
            print("  [%s] passed=%s chars=%s model=%s" % (platform, r["passed"], r.get("chars"), r.get("model") or r["models"]), flush=True)
            manifest["drafts"].append(r)
    ok = sum(1 for r in manifest["drafts"] if r["passed"])
    manifest.update({"passed": ok, "rejected": len(manifest["drafts"]) - ok, "jev_api": dict(JEV_STATS)})
    with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:  # counts only: run pages of a public repo are public, article text stays in the artifact
        with open(summ, "a", encoding="utf-8") as f:
            f.write("social drafts: picked %d, drafts %d, passed %d, rejected %d, jev calls %d ok %d\n"
                    % (len(picks), len(manifest["drafts"]), ok, manifest["rejected"], JEV_STATS["calls"], JEV_STATS["ok"]))
    print("[done] passed %d / %d drafts, jev %s" % (ok, len(manifest["drafts"]), JEV_STATS), flush=True)
    if ok == 0:
        sys.exit("zero drafts passed the compliance gate")


if __name__ == "__main__":
    main()
