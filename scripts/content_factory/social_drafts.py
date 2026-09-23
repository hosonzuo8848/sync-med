#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Social drafts (S1): Toutiao / Zhihu article drafts from public gufangai.com formula pages.

GENERATE ONLY. Nothing here logs in anywhere or publishes anything; drafts go to --out
(uploaded as a workflow artifact) for the founder to review.

Source: the public cross-book formula API (/api/formulas/detail) behind /fangji/<name>.
  Zero R2, zero D1 writes. The same formula name appears in many classical books with
  different compositions; that side-by-side comparison is the article angle.
Model: internal free-pool gateway only, pinned to one supplier with fallback OFF. Run 1 showed
  supplier=zhipu + fallback lands on agnes (rate-limited, not to be used) and glm-4-flash writes
  too short; zhipu_free47 = glm-4.7-flash, Zhipu official free text model, thinking off.
  It is shared with herb-norm S3 (paced 1 worker / 30 s gap): keep this job sequential + gapped.
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

SYSTEM = """You write Simplified-Chinese popular-reading articles for "Gufang AI Starmap" (gufangai.com),
a research-reference platform for classical Chinese medical LITERATURE. It is not a clinic and
gives no medical advice.

HARD RULES - any violation and the article is discarded:
1. Literature is the subject of every factual statement: write "the book X records ...",
   "according to X ...". Never state in your own voice that a formula treats or cures anything.
2. No diagnosis: never tell readers what condition they have or how to identify their own illness.
3. No prescribing: never suggest the reader take, try, buy or combine any formula or herb;
   no preparation or usage instructions (decocting, timing, frequency).
4. No doses: no amounts, weights or counts of any drug (no liang / qian / grams / pieces),
   not even inside quotations.
5. No efficacy promises: no "cures", "guaranteed", "effective rate", "miracle", "secret recipe",
   no patient stories or testimonials.
6. Use only facts present in the MATERIAL. Do not invent books, quotes, authors or dynasties.
   Put book titles in Chinese title marks. If unsure, leave it out.
7. Title: attractive but honest. No exaggeration, no clickbait words (shocking, divine formula,
   secret recipe, must-read), no efficacy claim in the title.
8. Plain text in Simplified Chinese. You may use a few short subheadings starting with "## ".
   No links, no hashtags, no emojis - the page link is appended automatically.

The angle: the same formula name appears in many classical books with different ingredient
lists. Our platform lays every version side by side instead of synthesizing one "standard
formula". Help readers see how the texts differ and why comparing editions matters when
studying classical literature. End by inviting readers to compare the original texts themselves."""

PLATFORM = {
    "toutiao": ("Platform: Toutiao news feed. Output format: line 1 = the title only (at most 30 "
                "Chinese characters). Then one blank line, then the body. Body length: 900-1300 Chinese "
                "characters (hard limits 800-1500). Accessible storytelling for general readers interested "
                "in traditional culture; short paragraphs.", (800, 1500), 30),
    "zhihu": ("Platform: Zhihu. Write an answer to a natural Zhihu-style question. Output format: "
              "line 1 = the question only (at most 40 Chinese characters). Then one blank line, then the "
              "answer. Answer length: 800-1600 Chinese characters. Calm, knowledgeable tone; the first "
              "sentence answers directly; back each point with a named source from the MATERIAL; close "
              "with one short paragraph on the limits of what these texts can tell us.", (700, 1800), 45),
}

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
SUPPLIER = os.environ.get("SOCIAL_SUPPLIER", "zhipu_free47")
CALL_GAP_S = 15


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
            "note": "Dose amounts were removed from quotes on purpose; do not add any."}


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
    user = spec + "\n\nMATERIAL (JSON):\n" + json.dumps(material(nm, d), ensure_ascii=False, indent=1)
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
        feedback = ("\n\nYOUR PREVIOUS DRAFT WAS REJECTED FOR: " + "; ".join(why)
                    + ". Write a new draft that fixes this and still follows every hard rule.")
    return rec


def selftest():
    assert DOSE_GATE.search("\u6842\u679d\u4e09\u4e24") and DOSE_GATE.search("\u6bcf\u6b2110g") and DOSE_GATE.search("\u4e00\u94b1\u5315")
    assert not DOSE_GATE.search("\u4e24\u672c\u4e66\u8bb0\u8f7d\u4e0d\u540c") and not DOSE_GATE.search("\u767e\u5408") and not DOSE_GATE.search("\u5341\u5206\u91cd\u8981")
    assert not DOSE_GATE.search("\u4e09\u4e2a\u7248\u672c") and DOSE_STRIP.sub("", "\u832f\u82d3\u5404\u4e09\u5206\u732a\u82d3\u5341\u516b\u682a") == "\u832f\u82d3\u5404\u732a\u82d3"
    assert PROMISE_GATE.search("\u6b64\u65b9\u53ef\u6839\u6cbb") and not PROMISE_GATE.search("\u300a\u5343\u91d1\u65b9\u300b\u8bb0\u8f7d")
    assert split_title("# \u6807\u9898\uff1a\u4e94\u82d3\u6563\n\n\u6b63\u6587")[0] == "\u4e94\u82d3\u6563"
    assert cheap_gate("t", "x" * 900, (800, 1500)) == [] and cheap_gate("t", "x" * 10, (800, 1500))
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
