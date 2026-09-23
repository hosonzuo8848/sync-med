#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Herb-name normalization, S1 + S2 pilot (platform CTO blueprint 2026-09-23, kb_herbs 372 -> 10k).

S1  Exactly two D1 reads per fresh export (one GROUP BY over sue_formulas_v2_herbs, one SELECT of
    the kb_herbs anchors). No paging, no loops, no retries (2026-09-08: batch reads saturated prod D1).
    The export is kept in the actions cache; when it is present D1 is not touched at all.
    Rule pass (pure string): NFKC + t2s, strip dose/unit words, strip processing prefix/suffix.
    Anchor-guarded: every intermediate form is checked against the anchors and the least-stripped
    hit wins, so an anchor like "baihe"/"sangjisheng" is never mangled by a unit/suffix strip.
S2  Top-N names by frequency that the rules could NOT map to an anchor, batches of `--batch`,
    judged independently by two different vendors through the internal free-pool gateway with
    no_fallback (a failed lane must never silently become the other vendor).
    A/B disagree -> Jev noul on each candidate; Jev conf < 0.8 or no Jev -> needs_review.
Resume: each model answer and each Jev answer is appended to jsonl as soon as it arrives;
    a rerun (cache restored) skips everything already judged.
Public repo: stdout carries counts only. Names and judgments live in the artifact, never in logs.
No D1 writes of any kind.
"""
import argparse, json, os, random, re, sys, threading, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ai import d1, ask, parse_json_array, jev, JEV_STATS  # noqa: E402  shared base, no copies

try:
    from zhconv import convert as _zh
except ImportError:
    _zh = None

SQL_HERBS = ("SELECT COALESCE(label_s,label) AS name, COUNT(*) AS n, "
             "json_group_array(DISTINCT label) AS variants "
             "FROM sue_formulas_v2_herbs WHERE COALESCE(label_s,label) IS NOT NULL "
             "GROUP BY 1 ORDER BY n DESC")
SQL_ANCHORS = "SELECT name FROM kb_herbs"
JEV_REVIEW = 0.8


def t2s(s):
    s = unicodedata.normalize("NFKC", s or "").strip()
    return _zh(s, "zh-cn") if _zh else s


# ---------------- rules ----------------
NUMS = "0-9.\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u534a\u4e24\u5eff\u5345\u3007\u96f6"   # no financial numerals / wan: shang-lu-gen is not a dose
UNITS = "\u94b1\u4e24\u5206\u65a4\u514b\u5398\u6beb\u94e2\u5b57\u5319\u64ae\u63e1\u628a\u679a\u4e2a\u7247\u7c92\u6761\u53ea\u5177\u5934\u6839\u5347\u5408\u6597\u76cf\u676f\u7897\u5bf8\u5c3a\u4e38g"
DOSE_RE = re.compile("\u5404?[%s]+[%s](?:[%s]+[%s]?)*" % (NUMS, UNITS, NUMS, UNITS))
DOSE_TAIL = ["\u5404\u7b49\u5206", "\u7b49\u5206", "\u5404\u534a", "\u5c11\u8bb8", "\u5c11\u91cf", "\u9002\u91cf", "\u82e5\u5e72", "\u4e0d\u62d8\u591a\u5c11", "\u4e0d\u62d8", "\u968f\u5b9c", "\u5404"]
PROC_SUFFIX = sorted([
    "\u53bb\u76ae\u5c16", "\u53bb\u76ae\u8110", "\u53bb\u82a6\u5934", "\u53bb\u7fc5\u8db3", "\u53bb\u5934\u8db3", "\u53bb\u7c97\u76ae", "\u53bb\u5fc3", "\u53bb\u76ae", "\u53bb\u8282", "\u53bb\u82a6", "\u53bb\u6bdb",
    "\u53bb\u6838", "\u53bb\u5b50", "\u53bb\u987b", "\u53bb\u6cb9", "\u53bb\u767d", "\u53bb\u74e4", "\u53bb\u571f", "\u53bb\u82d7", "\u70e7\u5b58\u6027", "\u7092\u9ec4", "\u7092\u9ed1", "\u7092\u7126",
    "\u7092\u70ad", "\u5fae\u7092", "\u9152\u6d17", "\u9152\u6d78", "\u9152\u7092", "\u9152\u84b8", "\u918b\u7092", "\u918b\u7099", "\u918b\u716e", "\u76d0\u6c34\u7092", "\u76d0\u7092", "\u59dc\u6c41\u7092",
    "\u59dc\u7092", "\u871c\u7099", "\u571f\u7092", "\u9eb8\u7092", "\u4e3a\u672b", "\u7814\u672b", "\u53e6\u7814", "\u7ec6\u7814", "\u751f\u7528", "\u540e\u4e0b", "\u5148\u714e", "\u5305\u714e",
    "\u70ca\u5316", "\u51b2\u670d", "\u7099", "\u7092", "\u7119", "\u7145", "\u7168", "\u7814", "\u9509", "\u6d17", "\u6d78", "\u84b8", "\u6363", "\u71ac", "\u5207", "\u788e",
    "\u672b", "\u70ad", "\u751f", "\u5236", "\u70ae"], key=len, reverse=True)
PROC_PREFIX = sorted([
    "\u9152\u7092", "\u9152\u5236", "\u9152\u6d17", "\u9152\u84b8", "\u918b\u5236", "\u918b\u7092", "\u918b\u7099", "\u871c\u7099", "\u76d0\u7092", "\u76d0\u5236", "\u59dc\u5236", "\u59dc\u6c41\u7092",
    "\u9eb8\u7092", "\u571f\u7092", "\u7099", "\u7092", "\u9152", "\u918b", "\u871c", "\u76d0", "\u59dc", "\u751f", "\u719f", "\u7145", "\u7119", "\u5236", "\u7168", "\u84b8",
    "\u7126", "\u70ae", "\u51c0", "\u9c9c"], key=len, reverse=True)
BRACKET_RE = re.compile("[(\\[\u3010\u3014]([^)\\]\u3011\u3015]*)[)\\]\u3011\u3015]")
PUNCT_RE = re.compile("[\\s,.;:\u3001\uff0c\u3002\uff1b\uff1a\u00b7]+")


def dose_cut(s):
    """Longest dose suffix that leaves a non-empty head ("baihe erliang" -> "baihe", not "").
    Whole string is a dose -> "". No dose -> None.
    ponytail: a non-anchor drug name that itself reads as number+unit (e.g. a bare non-anchor baihe)
    is cut; anchors are protected because rule() checks the unstripped form first."""
    for i in range(1, len(s)):
        if DOSE_RE.fullmatch(s, i):
            return s[:i]
    return "" if DOSE_RE.fullmatch(s) else None


def rule(name, anchors):
    """-> dict(kind=exact|rule|none|empty, anchor, clean, proc). anchors: set of t2s'd names."""
    s0 = t2s(name)
    proc = []

    def _b(m):
        proc.append(m.group(1))
        return ""
    s = PUNCT_RE.sub("", BRACKET_RE.sub(_b, s0))
    cands = [s0, s]
    changed = True
    while s and changed:
        changed = False
        c = dose_cut(s)
        if c is not None:
            s = c
            cands.append(s)
            changed = True
            continue
        for w in DOSE_TAIL:
            if s.endswith(w):
                s = s[:-len(w)]
                cands.append(s)
                changed = True
                break
        if changed:
            continue
        for w in PROC_SUFFIX:
            if s.endswith(w) and len(s) > len(w):
                s = s[:-len(w)]
                proc.append(w)
                cands.append(s)
                changed = True
                break
        if changed:
            continue
        for w in PROC_PREFIX:
            if s.startswith(w) and len(s) > len(w):
                s = s[len(w):]
                proc.append(w)
                cands.append(s)
                changed = True
                break
    hit = next((c for c in cands if c and c in anchors), None)
    if hit is not None:
        kind = "exact" if hit == s0 else "rule"
    else:
        kind = "empty" if not s else "none"
    return {"kind": kind, "anchor": hit, "clean": s, "proc": [p for p in proc if p]}


def selftest():
    A = {"\u7518\u8349", "\u4eba\u53c2", "\u767e\u5408", "\u6851\u5bc4\u751f", "\u5730\u9ec4", "\u719f\u5730\u9ec4", "\u534a\u590f", "\u4e09\u4e03", "\u5e72\u59dc", "\u751f\u59dc", "\u5927\u67a3"}
    cases = [("\u7518\u8349", "exact", "\u7518\u8349"), ("\u7099\u7518\u8349", "rule", "\u7518\u8349"), ("\u7518\u8349\u4e8c\u4e24", "rule", "\u7518\u8349"),
             ("\u4eba\u53c2\u4e00\u94b1\u534a", "rule", "\u4eba\u53c2"), ("\u767e\u5408", "exact", "\u767e\u5408"), ("\u6851\u5bc4\u751f", "exact", "\u6851\u5bc4\u751f"),
             ("\u719f\u5730\u9ec4", "exact", "\u719f\u5730\u9ec4"), ("\u751f\u5730\u9ec4", "rule", "\u5730\u9ec4"), ("\u534a\u590f(\u6c64\u6d17)", "rule", "\u534a\u590f"),
             ("\u4e09\u4e03", "exact", "\u4e09\u4e03"), ("\u5927\u67a3\u5341\u4e8c\u679a", "rule", "\u5927\u67a3"), ("\u59dc\u534a\u590f", "rule", "\u534a\u590f"),
             ("\u5e72\u59dc", "exact", "\u5e72\u59dc"), ("\u4e09\u94b1", "empty", None), ("\u5404\u7b49\u5206", "empty", None),
             ("\u5f53\u5f52\u5404", "none", None), ("\u4eba\u53c2\u53bb\u82a6\u5404\u4e09\u94b1", "rule", "\u4eba\u53c2"), ("\u767e\u5408\u4e8c\u4e24", "rule", "\u767e\u5408"),
             ("\u7518\u8349\u4e09\u94b1\u4e94\u5206", "rule", "\u7518\u8349"), ("\u5404\u4e09\u94b1", "empty", None)]
    for name, kind, anchor in cases:
        r = rule(name, A)
        assert (r["kind"], r["anchor"]) == (kind, anchor), (name.encode("unicode_escape"), r["kind"])
    assert rule("\u751f\u5730\u9ec4", A)["proc"] == ["\u751f"]
    assert rule("\u5546\u9646\u6839", A)["clean"] == "\u5546\u9646\u6839"
    assert rule("\u5f53\u5f52\u5404", A)["clean"] == "\u5f53\u5f52"
    a = {"is_herb": True, "canonical": "\u7518\u8349", "processing": ""}
    assert agree(a, dict(a, processing="\u7099")) and not agree(a, dict(a, canonical="\u9ec4\u82aa"))
    assert agree({"is_herb": False, "canonical": "x"}, {"is_herb": False, "canonical": ""})
    got = norm_items([{"name": "\u7518\u8349", "is_herb": "true", "canonical": "\u7518\u8349"}, {"name": "zz"}], ["\u7518\u8349", "\u9ec4\u82aa"])
    assert got["\u7518\u8349"]["is_herb"] is True and got["\u9ec4\u82aa"]["canonical"] == ""
    g = lambda c, nm: apply_gates({"is_herb": True, "canonical": c, "processing": ""}, nm, nm)
    assert g("", "\u9ec4\u8721")["canonical"] == "\u9ec4\u8721" and g("", "\u9ec4\u8721")["gate_fill"]
    assert g("\u5ddd\u8d1d\u6bcd", "\u8d1d\u6bcd")["canonical"] == "\u8d1d\u6bcd" and g("\u5ddd\u8d1d\u6bcd", "\u8d1d\u6bcd")["gate_generic"] == "\u5ddd\u8d1d\u6bcd"
    assert g("\u5ddd\u4e4c", "\u4e4c\u5934")["canonical"] == "\u4e4c\u5934" and g("\u5ddd\u4e4c", "\u4e4c\u5934")["gate_generic"]
    assert g("\u8089\u6842", "\u6842")["canonical"] == "\u6842"
    assert g("\u767d\u828d", "\u828d\u836f")["canonical"] == "\u767d\u828d" and not g("\u767d\u828d", "\u828d\u836f")["gate_generic"]
    assert g("\u9ea6\u51ac", "\u9ea6\u95e8\u51ac")["canonical"] == "\u9ea6\u51ac" and not g("\u9ea6\u51ac", "\u9ea6\u95e8\u51ac")["gate_generic"]
    assert apply_gates({"is_herb": False, "canonical": "", "processing": ""}, "\u4e09\u94b1", "")["canonical"] == ""
    print("selftest ok: %d rule cases + gates" % len(cases))


# ---------------- S1 ----------------
def s1_export(out):
    fe, fa = os.path.join(out, "export.jsonl"), os.path.join(out, "anchors.json")
    if os.path.exists(fe) and os.path.exists(fa):
        rows = [json.loads(l) for l in open(fe, encoding="utf-8")]
        anchors = json.load(open(fa, encoding="utf-8"))
        print("[S1] export from cache: names=%d anchors=%d (D1 not queried)" % (len(rows), len(anchors)), flush=True)
        return rows, anchors, {"from_cache": True}
    t = time.time()
    raw = d1(SQL_HERBS)                       # query 1 of 2, no retry by design
    t1 = time.time() - t
    t = time.time()
    anchors = sorted({r["name"] for r in d1(SQL_ANCHORS) if r.get("name")})   # query 2 of 2
    t2 = time.time() - t
    rows = []
    for r in raw:
        try:
            v = json.loads(r.get("variants") or "[]")
        except ValueError:
            v = []
        rows.append({"name": r["name"], "n": int(r["n"]), "variants": [x for x in v if x]})
    with open(fe + ".tmp", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(fe + ".tmp", fe)
    json.dump(anchors, open(fa, "w", encoding="utf-8"), ensure_ascii=False)
    meta = {"from_cache": False, "q1_sec": round(t1, 2), "q2_sec": round(t2, 2),
            "names": len(rows), "occurrences": sum(r["n"] for r in rows), "anchors": len(anchors)}
    print("[S1] D1 2 queries: names=%d occ=%d (%.1fs) anchors=%d (%.1fs)"
          % (len(rows), meta["occurrences"], t1, len(anchors), t2), flush=True)
    return rows, anchors, meta


def s1_rules(out, rows, anchors):
    amap = {t2s(a): a for a in anchors}
    aset = set(amap)
    res, cnt, occ = [], {}, {}
    for r in rows:
        x = rule(r["name"], aset)
        x["anchor"] = amap.get(x["anchor"]) if x["anchor"] else None
        res.append(dict(name=r["name"], n=r["n"], **x))
        cnt[x["kind"]] = cnt.get(x["kind"], 0) + 1
        occ[x["kind"]] = occ.get(x["kind"], 0) + r["n"]
    with open(os.path.join(out, "rules.jsonl"), "w", encoding="utf-8") as f:
        for x in res:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    tot_n, tot_o = len(res), sum(r["n"] for r in res)
    anchors_hit = len({x["anchor"] for x in res if x["anchor"]})
    summ = {"names": tot_n, "occurrences": tot_o, "anchors": len(anchors), "anchors_hit": anchors_hit,
            "by_kind_names": cnt, "by_kind_occurrences": occ,
            "rule_to_anchor_names": cnt.get("exact", 0) + cnt.get("rule", 0),
            "rule_to_anchor_occ_pct": round(100.0 * (occ.get("exact", 0) + occ.get("rule", 0)) / max(1, tot_o), 2),
            "distinct_clean_unresolved": len({x["clean"] for x in res if x["kind"] == "none"})}
    json.dump(summ, open(os.path.join(out, "rules_summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("[S1] rules: " + json.dumps({k: v for k, v in summ.items()}), flush=True)
    return res, summ


# ---------------- S2 ----------------
SYS = (
    "You normalize raw ingredient strings cut out of classical Chinese medicine formula texts. "
    "They may carry dose, unit or processing words, or may be OCR noise.\n"
    "DEFINITION of is_herb (scope of the Chinese Pharmacopoeia): is_herb is true for ANY ingredient that enters "
    "the formula as medicine: plant drugs, animal drugs, mineral drugs, and adjuvants / guiding ingredients "
    "(juices, honey, wax, wine, vinegar, urine and the like). is_herb is false ONLY for doses, units, "
    "preparation-method descriptions, administration instructions, pure numbers or item labels, and unreadable noise.\n"
    "Examples (string -> is_herb): \u8708\u86a3 -> true (animal drug); \u6731\u7802 -> true (mineral drug); "
    "\u77f3\u818f -> true (mineral drug); \u8702\u871c -> true (adjuvant); \u9152 -> true (adjuvant); \u7ae5\u4fbf -> true (adjuvant); "
    "\u4e09\u94b1 -> false (dose); \u5404\u7b49\u5206 -> false (dose); \u4e3a\u672b -> false (preparation method); "
    "\u6c34\u714e\u670d -> false (administration).\n"
    "For EACH input string return one object:\n"
    '  "name": the input string copied exactly;\n'
    '  "is_herb": true / false by the definition above;\n'
    '  "canonical": the standard materia-medica name in simplified Chinese with dose and processing removed; '
    "use the exact ANCHOR spelling when it is the same drug; NEVER empty when is_herb is true "
    "(if unsure, repeat the input without dose/processing words); empty string when is_herb is false;\n"
    '  "processing": processing words such as \u7099, \u7092, \u9152\u5236, \u53bb\u5fc3; empty string if none;\n'
    '  "note": one short reason in Chinese, at most 20 characters.\n'
    "Never merge different drugs: \u767d\u672f vs \u82cd\u672f, \u5ddd\u8d1d\u6bcd vs \u6d59\u8d1d\u6bcd, \u8d64\u828d vs \u767d\u828d are different drugs. "
    "A classical generic name that can mean several species or products (e.g. \u8d1d\u6bcd, \u4e4c\u5934) must keep that "
    "generic name as canonical; never narrow it to one specific variety or origin.\n"
    'Output JSON only, exactly: {"items": [ one object per input, same order ]}\n'
    "ANCHOR names: ")

# herb_aliases as of guyaofang-web migrations 055 + 056 (14 rows, same count the audit measured in D1).
# Round 2 must not query D1, so this is a read-only snapshot; the source of truth stays the controlled
# vocabulary file those migrations were generated from.
HERB_ALIASES = {"\u51b0\u7247": "\u51b0\u7247", "\u9f99\u8111": "\u51b0\u7247", "\u767d\u828d": "\u767d\u828d", "\u828d\u836f": "\u767d\u828d", "\u6de1\u8c46\u8c49": "\u6de1\u8c46\u8c49",
                "\u9999\u8c49": "\u6de1\u8c46\u8c49", "\u9648\u76ae": "\u9648\u76ae", "\u6a58\u76ae": "\u9648\u76ae", "\u5927\u8c46\u9ec4\u5377": "\u5927\u8c46\u9ec4\u5377", "\u8c46\u9ec4\u5377": "\u5927\u8c46\u9ec4\u5377",
                "\u5c71\u836f": "\u5c71\u836f", "\u85af\u84e3": "\u5c71\u836f", "\u719f\u5730\u9ec4": "\u719f\u5730\u9ec4", "\u719f\u5730": "\u719f\u5730\u9ec4"}
# generic names the CTO listed explicitly (2026-09-23); everything else is caught by the structural rule
GENERIC = {"\u8d1d\u6bcd", "\u4e4c\u5934", "\u9644\u5b50"}


def apply_gates(j, name, clean):
    """Round-2 rule gates on ONE model judgment (mutates and returns it).
    gate 1: herb with empty canonical -> rule-cleaned name.
    gate 2: never narrow a name. A canonical that is a proper superstring of the cleaned name, or any remap
            of a CTO-listed generic name, falls back to the cleaned name and is flagged for review --
            unless herb_aliases already holds exactly that mapping.
    ponytail: gate 2 is string-structural, so abbreviation expansions also land in review; a curated
    generic-name list from an authoritative dictionary replaces it when one exists."""
    base = clean or t2s(name)
    j["gate_fill"], j["gate_generic"] = False, ""
    if not j["is_herb"]:
        return j
    if not j["canonical"]:
        j["canonical"], j["gate_fill"] = base, True
        return j
    c, b = canon(j["canonical"]), canon(base)
    if c == b or HERB_ALIASES.get(b) == c or HERB_ALIASES.get(canon(name)) == c:
        return j
    if b in GENERIC or (b and b in c and len(c) > len(b)):
        j["gate_generic"], j["canonical"] = j["canonical"], base
    return j


def _bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "y", "\u662f")


def norm_items(items, batch):
    """Map model items back to batch names (exact name first, then position when lengths match)."""
    items = [x for x in items if isinstance(x, dict)]
    by = {}
    for x in items:
        nm = str(x.get("name", "")).strip()
        if nm in batch and nm not in by:
            by[nm] = x
    if len(items) == len(batch):
        for i, nm in enumerate(batch):
            by.setdefault(nm, items[i])
    out = {}
    for nm, x in by.items():
        ih = _bool(x.get("is_herb"))
        out[nm] = {"is_herb": ih, "canonical": str(x.get("canonical") or "").strip() if ih else "",
                   "processing": str(x.get("processing") or "").strip(), "note": str(x.get("note") or "").strip()[:60]}
    return out


def canon(s):
    return PUNCT_RE.sub("", t2s(s or ""))


def agree(a, b):
    if a["is_herb"] != b["is_herb"]:
        return False
    return (not a["is_herb"]) or canon(a["canonical"]) == canon(b["canonical"])


def _load(path):
    if not os.path.exists(path):
        return []
    out = []
    for l in open(path, encoding="utf-8"):
        try:
            out.append(json.loads(l))
        except ValueError:
            pass          # half-written last line from a cancelled run
    return out


def s2_pilot(out, rules, anchors, n, bsz, sup, max_tokens=6000, probe=(), workers=2, gap=0.0, fail_pause=0.0):
    t_start = time.time()
    pick = [r for r in rules if r["kind"] not in ("exact", "rule")][:n]
    freq = {r["name"]: r["n"] for r in pick}
    clean = {r["name"]: r["clean"] for r in pick}
    fj, fjev, ffail = (os.path.join(out, x) for x in ("judgments.jsonl", "jev.jsonl", "failures.jsonl"))
    lock = threading.Lock()
    done = {(x["who"], x["name"]): x for x in _load(fj)}
    stats = {w: {"supplier": sup[w], "calls": 0, "failed_calls": 0, "asked": 0, "returned": 0,
                 "models": {}, "sec": 0.0} for w in ("A", "B")}
    print("[S2] pick=%d resume: A=%d B=%d already judged"
          % (len(pick), sum(1 for k in done if k[0] == "A"), sum(1 for k in done if k[0] == "B")), flush=True)
    sys_msg = SYS + "\u3001".join(anchors)

    def call(supplier, batch):
        t = time.time()
        err, got, model, txt = "", {}, "", ""
        try:
            txt, model = ask(sys_msg, "Inputs (JSON array):\n" + json.dumps(batch, ensure_ascii=False),
                             timeout=150, max_tokens=max_tokens, supplier=supplier, source="herb_norm",
                             json_mode=True, temperature=0, no_fallback=True, gw_timeout_ms=90000)
            got = {nm: apply_gates(j, nm, clean.get(nm, ""))
                   for nm, j in norm_items(parse_json_array(txt, quiet=True), batch).items()}
            if not got:
                err = "parse: 0 items"
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
        return got, model, err, txt, time.time() - t

    def sink(who, batch, tag, got, model, err, txt, dt):
        with lock:
            s = stats[who]
            s["calls"] += 1
            s["asked"] += len(batch)
            s["returned"] += len(got)
            s["sec"] += dt
            if err:
                s["failed_calls"] += 1
                with open(ffail, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"who": who, "supplier": sup[who], "tag": tag, "n": len(batch), "err": err,
                                        "raw": (txt or "")[:800]}, ensure_ascii=False) + "\n")
            if model:
                s["models"][model] = s["models"].get(model, 0) + 1
            with open(fj, "a", encoding="utf-8") as f:
                for nm, j in got.items():
                    rec = dict(who=who, name=nm, supplier=sup[who], model=model, **j)
                    done[(who, nm)] = rec
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("[S2] %s %s asked=%d got=%d %.1fs%s" % (who, tag, len(batch), len(got), dt,
                                                    (" ERR " + err[:90].encode("ascii", "replace").decode()) if err else ""),
              flush=True)

    # client-side pacing (2026-09-23 round 2: agnes answered a single probe call, then 2 concurrent calls were
    # refused at once and the gateway breaker kept refusing -- suspected vendor rate limit). One vendor's
    # calls start >= gap seconds apart, a failure pauses that worker, 3 consecutive failures stop the vendor
    # for this run (no hammering a breaker; the rerun resumes).
    last, fails = {"A": 0.0, "B": 0.0}, {"A": 0, "B": 0}

    def work(who, batch, tag):
        if fails[who] >= 3:
            print("[S2] %s %s skipped: vendor stopped after 3 consecutive failures" % (who, tag), flush=True)
            return
        with lock:
            wait = last[who] + gap - time.time()
            last[who] = max(time.time(), last[who] + gap)
        if wait > 0:
            time.sleep(wait)
        res = call(sup[who], batch)
        sink(who, batch, tag, *res)
        with lock:
            fails[who] = fails[who] + 1 if res[2] else 0
        if res[2] and fail_pause:
            time.sleep(fail_pause)

    # ---- judge A chosen by measurement: same probe batch to B and to every candidate ----
    fprobe = os.path.join(out, "probe.json")
    if sup["A"] == "auto":
        if os.path.exists(fprobe):
            sup["A"] = json.load(open(fprobe, encoding="utf-8"))["chosen"]
        else:
            pnames = [r["name"] for r in pick[:bsz]]
            todo = [x for x in pnames if ("B", x) not in done]
            if todo:
                work("B", todo, "probe")
            res = []
            for cand in [c for c in probe if c and c != sup["B"]]:
                got, model, err, txt, dt = call(cand, pnames)
                agr = sum(1 for x, j in got.items() if ("B", x) in done and agree(j, done[("B", x)]))
                res.append({"supplier": cand, "model": model, "asked": len(pnames), "returned": len(got),
                            "agree_with_B": agr, "sec": round(dt, 1), "err": err[:200],
                            "gate_fill": sum(1 for j in got.values() if j["gate_fill"]),
                            "gate_generic": sum(1 for j in got.values() if j["gate_generic"]),
                            "_got": got, "_raw": (txt or "")[:400]})
                print("[probe] %s returned=%d/%d agree_with_B=%d %.1fs%s" % (
                    cand, len(got), len(pnames), agr, dt,
                    (" ERR " + err[:90].encode("ascii", "replace").decode()) if err else ""), flush=True)
            ok = [x for x in res if x["returned"] >= 0.8 * len(pnames)]
            if not ok:
                json.dump({"chosen": None, "candidates": [{k: v for k, v in x.items() if k != "_got"} for x in res]},
                          open(fprobe + ".failed", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                sys.exit("no judge-A candidate returned >= 80% of the probe batch")
            best = max(ok, key=lambda x: (x["agree_with_B"], -x["sec"]))
            sup["A"] = best["supplier"]
            stats["A"]["supplier"] = sup["A"]
            sink("A", pnames, "probe", best["_got"], best["model"], best["err"], "", best["sec"])   # no wasted call
            json.dump({"chosen": sup["A"], "rule": "returned>=80% then max agree_with_B then fastest",
                       "probe_names": len(pnames), "candidates": [{k: v for k, v in x.items() if k != "_got"} for x in res]},
                      open(fprobe, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        stats["A"]["supplier"] = sup["A"]
        print("[probe] judge A = %s" % sup["A"], flush=True)

    for p in (1, 2):   # pass 2 = one retry for names a model dropped or failed on
        per = {}
        for who in ("A", "B"):
            todo = [r["name"] for r in pick if (who, r["name"]) not in done]
            size = bsz if p == 1 else max(10, bsz // 2)
            per[who] = [(who, todo[i:i + size], "p%d-b%d" % (p, i // size + 1)) for i in range(0, len(todo), size)]
        # interleave A/B so each vendor sees ~2 concurrent calls
        jobs = [j for pair in zip_longest(per["A"], per["B"]) for j in pair if j]
        if not jobs:
            break
        print("[S2] pass %d: %d calls" % (p, len(jobs)), flush=True)
        # 2 concurrent calls per vendor: zhipu also heads the production gateway chain
        with ThreadPoolExecutor(workers * len({j[0] for j in jobs})) as ex:
            list(ex.map(lambda j: work(*j), jobs))
    t_models = time.time() - t_start

    # ---- Jev on disagreements ----
    jdone = {x["name"]: x for x in _load(fjev) if x.get("p_a") is not None}   # failed Jev calls get retried
    need = [r["name"] for r in pick if ("A", r["name"]) in done and ("B", r["name"]) in done
            and not agree(done[("A", r["name"])], done[("B", r["name"])]) and r["name"] not in jdone]

    def desc(x):
        return "is_herb=%s; canonical=%s; processing=%s" % (
            "true" if x["is_herb"] else "false", x["canonical"] or "-", x["processing"] or "-")

    def judge(nm):
        a, b = done[("A", nm)], done[("B", nm)]
        state = ("Raw ingredient string cut out of classical Chinese medicine formula texts: %s "
                 "(appears %d times). Task: decide whether it names a medicinal substance and, if it does, "
                 "its standard materia-medica name in simplified Chinese with dose and processing words removed. "
                 "Two independent judgments disagree.\nJudgment A: %s\nJudgment B: %s") % (nm, freq[nm], desc(a), desc(b))
        ans = jev(state, {"a": {"type": "noul", "instructions": "Judgment A is correct: " + desc(a)},
                          "b": {"type": "noul", "instructions": "Judgment B is correct: " + desc(b)}}, timeout=40)
        pa = pb = None
        if ans:
            try:
                pa = float((ans.get("a") or {}).get("noul"))
                pb = float((ans.get("b") or {}).get("noul"))
            except (TypeError, ValueError):
                pa = pb = None
        rec = {"name": nm, "p_a": pa, "p_b": pb}
        with lock:
            jdone[nm] = rec
            with open(fjev, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    t = time.time()
    if need:
        print("[S2] jev: %d disagreements to arbitrate" % len(need), flush=True)
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(judge, need))
    t_jev = time.time() - t

    # ---- assemble ----
    recs = []
    for r in pick:
        nm = r["name"]
        a, b = done.get(("A", nm)), done.get(("B", nm))
        rec = {"name": nm, "n": r["n"], "rule_kind": r["kind"], "rule_clean": r["clean"], "A": a, "B": b,
               "jev": None, "status": "", "final": None, "needs_review": False,
               "gate_generic": bool((a and a.get("gate_generic")) or (b and b.get("gate_generic")))}
        if not (a and b):
            rec["status"] = "incomplete"
            rec["needs_review"] = True
        elif agree(a, b):
            rec["status"] = "agree"
            rec["final"] = {"is_herb": a["is_herb"], "canonical": a["canonical"],
                            "processing": a["processing"] or b["processing"], "source": "A=B"}
        else:
            rec["status"] = "jev"
            j = jdone.get(nm) or {}
            pa, pb = j.get("p_a"), j.get("p_b")
            if pa is None or pb is None:
                rec["jev"] = {"p_a": pa, "p_b": pb, "pick": None, "conf": None}
                rec["needs_review"] = True
            else:
                w = "A" if pa >= pb else "B"
                x = a if w == "A" else b
                conf = max(pa, pb)
                rec["jev"] = {"p_a": round(pa, 3), "p_b": round(pb, 3), "pick": w, "conf": round(conf, 3)}
                rec["final"] = {"is_herb": x["is_herb"], "canonical": x["canonical"], "processing": x["processing"],
                                "source": "jev:" + w}
                rec["needs_review"] = conf < JEV_REVIEW
        if rec["gate_generic"]:
            rec["needs_review"] = True        # CTO gate 2: a narrowed generic name always goes to a human
        recs.append(rec)
    with open(os.path.join(out, "pilot.jsonl"), "w", encoding="utf-8") as f:
        for x in recs:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")

    aset = {t2s(a) for a in anchors}
    st = {}
    for x in recs:
        st[x["status"]] = st.get(x["status"], 0) + 1
    both = st.get("agree", 0) + st.get("jev", 0)
    confs = [x["jev"]["conf"] for x in recs if x["jev"] and x["jev"]["conf"] is not None]
    bins = {"<0.5": 0, "0.5-0.8": 0, "0.8-0.95": 0, ">=0.95": 0}
    for c in confs:
        bins["<0.5" if c < 0.5 else "0.5-0.8" if c < 0.8 else "0.8-0.95" if c < 0.95 else ">=0.95"] += 1
    for w in ("A", "B"):
        s = stats[w]
        s["sec"] = round(s["sec"], 1)
        s["failed_call_rate_pct"] = round(100.0 * s["failed_calls"] / max(1, s["calls"]), 1)
        s["final_missing"] = sum(1 for r in pick if (w, r["name"]) not in done)
    summ = {
        "picked": len(pick), "status": st,
        "agree_rate_pct": round(100.0 * st.get("agree", 0) / max(1, both), 1),
        "final_is_herb_false": sum(1 for x in recs if x["final"] and x["final"]["is_herb"] is False),
        "a_says_not_herb": sum(1 for x in recs if x["A"] and not x["A"]["is_herb"]),
        "b_says_not_herb": sum(1 for x in recs if x["B"] and not x["B"]["is_herb"]),
        "jev_arbitrated": st.get("jev", 0), "jev_with_conf": len(confs), "jev_conf_bins": bins,
        "jev_pick": {w: sum(1 for x in recs if x["jev"] and x["jev"]["pick"] == w) for w in ("A", "B")},
        "jev_api": dict(JEV_STATS), "needs_review": sum(1 for x in recs if x["needs_review"]),
        "canonical_in_anchor": sum(1 for x in recs if x["final"] and x["final"]["is_herb"]
                                   and canon(x["final"]["canonical"]) in aset),
        "models": stats, "sec_models": round(t_models, 1), "sec_jev": round(t_jev, 1),
        "gate_fill": {w: sum(1 for x in recs if x[w] and x[w].get("gate_fill")) for w in ("A", "B")},
        "gate_generic": {w: sum(1 for x in recs if x[w] and x[w].get("gate_generic")) for w in ("A", "B")},
        "gate_generic_names": sum(1 for x in recs if x["gate_generic"]),
        "needs_review_why": {
            "incomplete": sum(1 for x in recs if x["status"] == "incomplete"),
            "jev_low_or_missing": sum(1 for x in recs if x["status"] == "jev" and (
                not x["jev"] or x["jev"]["conf"] is None or x["jev"]["conf"] < JEV_REVIEW)),
            "generic_gate_only": sum(1 for x in recs if x["gate_generic"] and not (x["status"] == "jev" and (
                not x["jev"] or x["jev"]["conf"] is None or x["jev"]["conf"] < JEV_REVIEW)))},
        "judge_A": sup["A"], "judge_B": sup["B"],
    }
    json.dump(summ, open(os.path.join(out, "pilot_summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    write_sample(out, recs)
    print("[S2] " + json.dumps({k: v for k, v in summ.items() if k != "models"}), flush=True)
    for w in ("A", "B"):
        print("[S2] %s %s" % (w, json.dumps(stats[w])), flush=True)
    return summ


def _cell(x):
    if not x:
        return "(\u65e0)"
    if not x["is_herb"]:
        return "\u975e\u836f"
    return x["canonical"] + ("\u3014%s\u3015" % x["processing"] if x.get("processing") else "")


def write_sample(out, recs):
    rng = random.Random(20260923)
    taken = set()

    def take(pool, k):
        pool = [x for x in pool if x["name"] not in taken]
        got = rng.sample(pool, min(k, len(pool)))
        taken.update(x["name"] for x in got)
        return sorted(got, key=lambda x: -x["n"])

    nonherb = take([x for x in recs if x["final"] and x["final"]["is_herb"] is False], 10)
    ag = sorted([x for x in recs if x["status"] == "agree"], key=lambda x: -x["n"])
    k = len(ag) // 3
    agree_s = take(ag[:k], 9) + take(ag[k:2 * k], 8) + take(ag[2 * k:], 8)
    jev_s = take([x for x in recs if x["status"] == "jev"], 15)
    rows = [("agree", x) for x in agree_s] + [("jev", x) for x in jev_s] + [("\u975e\u836f", x) for x in nonherb]
    with open(os.path.join(out, "sample50.jsonl"), "w", encoding="utf-8") as f:
        for layer, x in rows:
            f.write(json.dumps(dict(layer=layer, **x), ensure_ascii=False) + "\n")
    lines = ["| # | \u5c42 | name | \u9891\u6b21 | A | B | Jev | \u6700\u7ec8 |", "|---|---|---|---|---|---|---|---|"]
    for i, (layer, x) in enumerate(rows, 1):
        j = x["jev"]
        jc = "-" if not j else ("\u9009%s %.2f/%.2f" % (j["pick"], j["p_a"], j["p_b"]) if j["pick"] else "\u65e0Jev\u7ed3\u679c")
        fin = _cell(x["final"]) if x["final"] else "(\u5f85\u5ba1)"
        if x["needs_review"]:
            fin += " \u26a0\u5f85\u5ba1"
        lines.append("| %d | %s | %s | %d | %s | %s | %s | %s |" % (
            i, layer, x["name"].replace("|", "/"), x["n"], _cell(x["A"]), _cell(x["B"]), jc, fin))
    open(os.path.join(out, "sample50.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/herb_norm")
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--a", default="auto")    # auto = measured pick among --probe; never sensenova (quota)
    ap.add_argument("--probe", default="nv_gemma,agnes")
    ap.add_argument("--tag", default="")      # round subdir; export/rules stay shared at --out
    ap.add_argument("--workers", type=int, default=2)        # concurrent calls per vendor
    ap.add_argument("--gap", type=float, default=0.0)        # min seconds between one vendor's call starts
    ap.add_argument("--fail-pause", type=float, default=0.0)  # seconds a worker waits after a failed call
    ap.add_argument("--b", default="modelscope")
    ap.add_argument("--max-tokens", type=int, default=6000)   # glm-4-flash caps output near 4k
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if _zh is None or t2s("\u7576\u6b78") != "\u5f53\u5f52":
        sys.exit("zhconv missing or not converting: pip install zhconv")
    if a.a == a.b:
        sys.exit("A and B must be different suppliers")
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    rows, anchors, meta = s1_export(a.out)
    rules, rsum = s1_rules(a.out, rows, anchors)
    out_r = os.path.join(a.out, a.tag) if a.tag else a.out
    os.makedirs(out_r, exist_ok=True)
    psum = s2_pilot(out_r, rules, anchors, a.n, a.batch, {"A": a.a, "B": a.b}, a.max_tokens,
                    [c.strip() for c in a.probe.split(",")], a.workers, a.gap, a.fail_pause)
    total = round(time.time() - t0, 1)
    json.dump({"export": meta, "rules": rsum, "pilot": psum, "sec_total": total},
              open(os.path.join(out_r, "run_summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    sm = os.environ.get("GITHUB_STEP_SUMMARY")
    if sm:
        with open(sm, "a", encoding="utf-8") as f:
            f.write("## herb-norm pilot (counts only; names are in the artifact)\n\n```\n")
            f.write(json.dumps({"export": meta, "rules_by_kind": rsum["by_kind_names"],
                                "rule_to_anchor_names": rsum["rule_to_anchor_names"],
                                "status": psum["status"], "agree_rate_pct": psum["agree_rate_pct"],
                                "final_is_herb_false": psum["final_is_herb_false"],
                                "jev_conf_bins": psum["jev_conf_bins"], "needs_review": psum["needs_review"],
                                "A_failed_calls": psum["models"]["A"]["failed_calls"],
                                "B_failed_calls": psum["models"]["B"]["failed_calls"],
                                "sec_total": total}, indent=1))
            f.write("\n```\n")
    print("[done] %.1fs" % total, flush=True)


if __name__ == "__main__":
    main()
