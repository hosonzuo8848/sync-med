#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hyper-s3: AI stage of hyperedge extraction (formula composition F / syndrome reasoning R) on a small random
batch of public-domain text chunks. READ-ONLY: D1 is only read (SELECT + pragma), nothing is written; every AI
call goes through the internal free-pool gateway (no provider/model pinned). Output = jsonl artifact + summary +
30-row judge sample + one Issue. Chinese strings are \\u-escaped on purpose (public repo policy; see _esc.py).

Env: CF_ACCOUNT_ID D1_DATABASE_ID D1_API_TOKEN GW_KEY  LIMIT=500 THREADS=3 SEED=42  TEXT_MAX=1500 OUT_DIR=.
Chunk source: books_fts_v2_src(rowid, chunk_id, part_no, text_id, vol_no, body_raw); parts of one chunk sit on
rowids are sparse (start near 2^48), so chunks are sampled by a seeded SQL ordering and parts fetched by chunk_id.
"""
import datetime
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'content_factory'))
from _ai import d1, GATEWAY   # single D1 transport (guard_single_source: no hardcoded endpoint copies)

import io
import json
import os
import random
import re
import statistics
import sys
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
GW = GATEWAY   # from _ai (single source of the gateway URL)
UA = "sync-med-hyper-s3/1.0 (GitHub Actions; +https://github.com/hosonzuo8848/sync-med)"
ACC = os.environ["CF_ACCOUNT_ID"]; DB = os.environ["D1_DATABASE_ID"]; TOK = os.environ["D1_API_TOKEN"]
KEY = os.environ.get("GW_KEY", "")
LIMIT = int(os.environ.get("LIMIT") or "500")
TIER_A_ONLY = os.environ.get("TIER_A_ONLY", "").strip() in ("1", "true", "yes")
SAMPLE_N = int(os.environ.get("SAMPLE_N") or "30")
THREADS = int(os.environ.get("THREADS") or "3")
SEED = int(os.environ.get("SEED") or "42")
TEXT_MAX = int(os.environ.get("TEXT_MAX") or "1500")   # chars sent to the model; longer chunks are cut + flagged
OUT_DIR = os.environ.get("OUT_DIR") or "."
MIN_CHARS = 80
MAX_EDGES = 12
PROMPT_VERSION = "he_s3_v0"
SLEEP_BETWEEN = 0.3
EXTRA_PARTS = 3          # parts beyond part_no 0 looked up at rowid+1..+3 (3 x 3000 chars; more is cut anyway)

SYS = (
    "\u4f60\u662f\u53e4\u7c4d\u65b9\u4e66\u7684\u7ed3\u6784\u5316\u6821\u5bf9\u5458,\u4e0d\u662f\u533b\u751f\u3002\u7ed9\u4f60\u4e00\u6bb5\u4e2d\u533b\u53e4\u7c4d\u539f\u6587,\u53ea\u505a\u4e00\u4ef6\u4e8b:\u628a\u539f\u6587\u91cc\u660e\u5199\u7684\u4e24\u7c7b\u7ed3\u6784\u6284\u6210 JSON \u8d85\u8fb9\u3002"
    "\u4e0d\u8981\u751f\u6210\u539f\u6587\u6ca1\u6709\u7684\u5185\u5bb9,\u4e0d\u8981\u6309\u65b9\u5242\u5b66\u77e5\u8bc6\u8865\u5168,\u4e0d\u8981\u6539\u5199\u539f\u6587\u3002\n"
    "F \u578b(\u65b9\u5242\u7ec4\u6210):\u539f\u6587\u660e\u5199\u300c\u67d0\u65b9:\u836fA \u5242\u91cf,\u836fB \u5242\u91cf\u2026\u2026\u300d\u8fd9\u79cd\u65b9\u5242\u53ca\u5176\u7ec4\u6210\u65f6,\u4e00\u6761 F \u8d85\u8fb9 = \u4e00\u9996\u65b9 + \u5b83\u7684\u5168\u90e8\u7ec4\u6210\u836f\u3002"
    "head=\u65b9\u540d(\u7167\u539f\u6587\u5199\u6cd5);members \u6bcf\u5473\u836f\u4e00\u9879:label=\u836f\u540d(\u7528\u7b80\u4f53\u901a\u884c\u6b63\u540d,\u5982\u300c\u4e7e\u8591\u300d\u5199\u300c\u5e72\u59dc\u300d\u3001\u300c\u7518\u8278\u300d\u5199\u300c\u7518\u8349\u300d),"
    "dose=\u5242\u91cf\u9010\u5b57\u6284\u539f\u6587(\u539f\u6587\u6ca1\u5199\u5c31 null),role \u53ea\u5728\u539f\u6587\u660e\u5199\u300c\u541b/\u81e3/\u4f50/\u4f7f/\u4e3a\u541b\u300d\u65f6\u586b \u541b \u81e3 \u4f50 \u4f7f \u4e4b\u4e00,\u5426\u5219 null\u3002"
    "\u714e\u670d\u6cd5\u91cc\u7684\u59dc\u3001\u67a3\u3001\u6c34\u3001\u9152,\u52a0\u51cf\u6cd5\u91cc\u7684\u836f,\u70ae\u5236\u6ce8,\u90fd\u4e0d\u662f\u7ec4\u6210,\u4e0d\u8981\u653e\u8fdb members\u3002\n"
    "R \u578b(\u8fa8\u8bc1\u63a8\u7406\u94fe):\u539f\u6587\u660e\u5199\u300c\u67d0\u8bc1\u6216\u67d0\u75c7\u72b6 \u2192 \u6cbb\u6cd5 \u2192 \u7528\u67d0\u65b9\u300d\u8fd9\u6837\u7684\u94fe\u6761\u65f6,\u4e00\u6761 R \u8d85\u8fb9 = head=\u8bc1\u5019\u540d(\u7167\u539f\u6587;"
    "\u6ca1\u6709\u8bc1\u5019\u53ea\u6709\u75c7\u72b6\u65f6 head \u7528\u4e3b\u75c7);members \u6bcf\u9879 role \u53ea\u80fd\u662f symptom(\u75c7\u72b6)\u3001syndrome(\u8bc1\u5019)\u3001principle(\u6cbb\u6cd5)\u3001formula(\u65b9\u540d)\u4e4b\u4e00,"
    "dose \u4e00\u5f8b null\u3002\u81f3\u5c11 1 \u4e2a formula \u6210\u5458\u548c 1 \u4e2a symptom \u6216 syndrome \u6210\u5458\u624d\u7b97\u4e00\u6761 R\u3002\n"
    "\u5171\u540c\u8981\u6c42:source_quote \u662f\u539f\u6587\u9010\u5b57\u5b50\u4e32(\u4e0d\u8d85\u8fc7 60 \u5b57),\u5fc5\u987b\u80fd\u5728\u539f\u6587\u91cc\u539f\u6837\u627e\u5230;confidence \u53d6 0 \u5230 1"
    "(\u539f\u6587\u7ed3\u6784\u6e05\u695a\u3001\u6210\u5458\u9f50\u5168\u7ed9 0.8 \u4ee5\u4e0a;\u6709\u542b\u7cca\u7ed9 0.5 \u4ee5\u4e0b)\u3002\u300c\u67d0\u6c64\u4e0b\u300d\u300c\u67d0\u4e38\u9001\u670d\u300d\u8fd9\u7c7b\u670d\u6cd5\u3001\u8bae\u8bba\u91cc\u987a\u5e26\u63d0\u5230\u7684\u65b9\u540d,\u4e0d\u7ec4\u8d85\u8fb9\u3002"
    "\u4e00\u6bb5\u6700\u591a 12 \u6761\u8d85\u8fb9;\u539f\u6587\u6ca1\u6709\u53ef\u6284\u7684\u7ed3\u6784\u5c31\u8f93\u51fa\u7a7a\u6570\u7ec4\u3002\n"
    "\u53ea\u8f93\u51fa\u4e00\u4e2a JSON \u5bf9\u8c61,\u4e0d\u8981\u89e3\u91ca,\u4e0d\u8981 Markdown \u56f4\u680f,\u683c\u5f0f\u5982\u4e0b:\n"
    '{"hyperedges":[{"type":"F","head":"\u56db\u9006\u6c64","members":[{"label":"\u7518\u8349","role":null,"dose":"\u4e8c\u4e24"},'
    '{"label":"\u5e72\u59dc","role":null,"dose":"\u4e00\u4e24\u534a"},{"label":"\u9644\u5b50","role":null,"dose":"\u4e00\u679a"}],'
    '"source_quote":"\u56db\u9006\u6c64\u65b9:\u7518\u8349\u4e8c\u4e24,\u7099,\u5e72\u59dc\u4e00\u4e24\u534a,\u9644\u5b50\u4e00\u679a,\u751f\u7528","confidence":0.9},'
    '{"type":"R","head":"\u5c11\u9634\u75c5","members":[{"label":"\u8109\u5fae\u7ec6","role":"symptom","dose":null},'
    '{"label":"\u4f46\u6b32\u5bd0","role":"symptom","dose":null},{"label":"\u56db\u9006\u6c64","role":"formula","dose":null}],'
    '"source_quote":"\u5c11\u9634\u75c5,\u8109\u5fae\u7ec6,\u4f46\u6b32\u5bd0,\u5b9c\u56db\u9006\u6c64","confidence":0.7}]}'
)
USER = "\u539f\u6587(chunk_id={cid}):\n{text}"
KIND_SYNDROME = "\u8bc1\u5019"
ROLES_F = {"\u541b", "\u81e3", "\u4f50", "\u4f7f"}
ROLES_R = {"symptom", "syndrome", "principle", "formula"}

ISSUE_HEAD = ("\u53ea\u8bfb\u5e72\u8dd1,\u672a\u5199 D1\u3002\u6bcf\u6bb5\u4e00\u6b21\u7f51\u5173\u8c03\u7528(json \u6a21\u5f0f,\u4e0d\u6307\u5b9a provider/model),\u6210\u5458\u53ea\u6821\u9a8c\u4e0d\u9020\u8282\u70b9:"
              "\u65b9\u540d\u5bf9 kb_formulas(name_s+display_name),\u836f\u540d\u5bf9 kb_herbs+herb_aliases+herb_safety_flags,\u8bc1\u5019\u5bf9 search_terms(kind=\u8bc1\u5019);"
              "\u4e0d\u5728\u8868\u91cc\u7684\u6210\u5458 unknown_node=true \u4fdd\u7559\u4f46\u4e0d\u8ba1\u547d\u4e2d\u3002\u5168\u91cf\u5728 run artifact hyper_s3.jsonl;\u88c1\u5224\u5728 sample30 \u8868\u300c\u5224\u300d\u5217\u586b C(\u5bf9)/P(\u90e8\u5206)/W(\u9519)\u3002")
ISSUE_TITLE = "\u8d85\u8fb9S3 {day}: {n} \u6bb5 \u00b7 {e} \u8d85\u8fb9 \u00b7 \u6210\u529f\u7387 {ok}% \u00b7 \u5df2\u77e5\u8282\u70b9\u547d\u4e2d {hit}%"
SAMPLE_HEAD = ("# S3 \u88c1\u5224\u6837\u672c 30 \u6761(seed={seed},\u4ece {total} \u6761\u8d85\u8fb9\u91cc\u62bd)\n\n"
               "\u5224\u6cd5:C=\u65b9/\u8bc1\u4e0e\u6210\u5458\u3001\u5242\u91cf\u90fd\u5bf9\u539f\u6587;P=\u65b9\u5bf9\u4f46\u6210\u5458\u591a\u4e86\u6216\u5c11\u4e86\u6216\u5242\u91cf\u9519;W=\u65b9\u540d\u4e0d\u662f\u539f\u6587\u660e\u5199\u7684\u65b9/\u6574\u6761\u662f\u5e7b\u89c9\u3002"
               "\u6210\u5458\u540e\u7f00 ? = \u4e0d\u5728\u5df2\u77e5\u8282\u70b9\u8868(unknown_node);quote \u540e\u7f00 ! = \u5f15\u6587\u4e0d\u662f\u539f\u6587\u5b50\u4e32\u3002\n\n"
               "| # | \u578b | chunk | head | members(label\u00b7role\u00b7dose) | source_quote | conf | \u5224 |\n|---|---|---|---|---|---|---|---|")
LOCK = threading.Lock()




def cols(table):
    return {r["name"] for r in d1("SELECT name FROM pragma_table_info('%s') LIMIT 100" % table)}


def need(table, want, required=True):
    have = cols(table)
    miss = [c for c in want if c not in have]
    print("schema %s: %s%s" % (table, sorted(have)[:12], (" MISSING " + str(miss)) if miss else ""), flush=True)
    if miss and required:
        raise RuntimeError("schema mismatch %s missing %s" % (table, miss))
    return not miss


def load_known():
    """Three name sets, all read from D1 after a pragma check; column names are verified, never guessed."""
    need("kb_formulas", ["name_s", "display_name"])
    need("kb_herbs", ["name"])
    need("herb_aliases", ["variant", "canonical"])
    formulas, herbs, syndromes = set(), set(), set()
    for r in d1("SELECT name_s, display_name FROM kb_formulas LIMIT 200000"):
        for v in (r.get("name_s"), r.get("display_name")):
            if v: formulas.add(v.strip())
    for r in d1("SELECT name FROM kb_herbs LIMIT 10000"):
        if r.get("name"): herbs.add(r["name"].strip())
    for r in d1("SELECT variant, canonical FROM herb_aliases LIMIT 10000"):
        for v in (r.get("variant"), r.get("canonical")):
            if v: herbs.add(v.strip())
    if need("herb_safety_flags", ["herb", "aliases"], required=False):
        for r in d1("SELECT herb, aliases FROM herb_safety_flags LIMIT 10000"):
            if r.get("herb"): herbs.add(r["herb"].strip())
            for v in re.split(r"[,\uff0c;\u3001]", r.get("aliases") or ""):
                if v.strip(): herbs.add(v.strip())
    if need("search_terms", ["term", "kind"], required=False):
        for r in d1("SELECT term FROM search_terms WHERE kind='%s' LIMIT 20000" % KIND_SYNDROME):
            if r.get("term"): syndromes.add(r["term"].strip())
    herb_heads = set(herbs)
    if need("search_terms", ["term", "kind"], required=False):
        for r in d1("SELECT term FROM search_terms WHERE kind='%s' LIMIT 60000" % "\u672c\u8349"):
            if r.get("term"): herb_heads.add(r["term"].strip())
    print("known: formulas=%d herbs=%d syndromes=%d herb_heads=%d" % (len(formulas), len(herbs), len(syndromes), len(herb_heads)), flush=True)
    return formulas, herbs, syndromes, herb_heads


def sample_chunks(rng):
    """Seeded, reproducible sample of LIMIT chunks straight from books_fts_v2_src.
    06:19 first cloud run: 240/240 random rowids fell into gaps (rowids start near 2^48 and are sparse), so
    random-rowid lookups return nothing. Now the ORDER BY is a deterministic pseudo-random key of (rowid, SEED)
    computed in SQL (same SEED + same table => same chunks), filtered to public-domain visible books and
    part_no 0; extra parts are fetched by chunk_id (UNIQUE(chunk_id, part_no) is indexed)."""
    need("books_fts_v2_src", ["chunk_id", "part_no", "text_id", "vol_no", "body_raw"])
    need("books_text", ["text_id", "rights_status", "frontend_visible"])
    stats = Counter()
    rows = d1("SELECT rowid, chunk_id, part_no, text_id, vol_no, body_raw FROM books_fts_v2_src "
              "WHERE part_no=0 AND length(body_raw) >= %d "
              "AND text_id IN (SELECT text_id FROM books_text WHERE rights_status='public_domain' AND frontend_visible=1) "
              "ORDER BY ((rowid %% 1000003) * 7919 + %d) %% 999983, rowid LIMIT %d"
              % (MIN_CHARS, int(SEED), LIMIT * 2))
    stats["candidates"] = len(rows)
    picked, seen_ids = [], set()
    for r in rows:
        if r["chunk_id"] in seen_ids: stats["dup"] += 1; continue
        seen_ids.add(r["chunk_id"]); picked.append(r)
        if len(picked) >= LIMIT: break
    print("sampled %d chunks after filters %s" % (len(picked), dict(stats)), flush=True)
    extra = {}
    ids = [r["chunk_id"] for r in picked]
    for i in range(0, len(ids), 100):
        q = ",".join("'" + x.replace("'", "''") + "'" for x in ids[i:i + 100])
        for r in d1("SELECT chunk_id, part_no, body_raw FROM books_fts_v2_src WHERE chunk_id IN (%s) AND part_no>0 "
                    "AND part_no<=%d LIMIT 400" % (q, EXTRA_PARTS)):
            extra.setdefault(r["chunk_id"], []).append((r["part_no"], r["body_raw"] or ""))
        time.sleep(0.5)
    out = []
    for r in picked:
        parts = [(0, r["body_raw"] or "")] + sorted(extra.get(r["chunk_id"], []))
        text = "".join(t for _, t in parts)
        out.append({"chunk_id": r["chunk_id"], "text_id": r["text_id"], "vol_no": r["vol_no"],
                    "n_parts": len(parts), "n_chars": len(text), "text": text})
    return out, dict(stats)


def gw_call(body):
    """One gateway POST. Returns the parsed gateway JSON; raises on transport error."""
    req = urllib.request.Request(GW, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "X-Gateway-Key": KEY, "User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def call_gateway(chunk):
    """Up to 3 attempts (2 retries) for transport / ok:false / non-JSON. Returns (obj|None, meta)."""
    text = chunk["text"]
    truncated = len(text) > TEXT_MAX
    body = {"messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": USER.format(cid=chunk["chunk_id"], text=text[:TEXT_MAX])}],
            "json": True, "max_tokens": 1500, "temperature": 0, "source": "hyper_s3", "timeout_ms": 90000}
    meta = {"provider": None, "latency_ms": None, "err": "", "attempts": 0, "truncated": truncated}
    for attempt in range(3):
        meta["attempts"] = attempt + 1
        t0 = time.time()
        try:
            j = gw_call(body)
            meta["latency_ms"] = int((time.time() - t0) * 1000)
            meta["provider"] = j.get("provider")
            if not j.get("ok"):
                meta["err"] = "gateway ok:false " + str(j.get("error"))[:80]; time.sleep(2); continue
            t = (j.get("text") or "").strip()
            a = t.find("{"); b = t.rfind("}")
            if a < 0 or b <= a:
                meta["err"] = "non_json: " + t[:60].replace("\n", " "); time.sleep(2); continue
            try:
                obj = json.loads(t[a:b + 1])
            except ValueError as e:
                meta["err"] = "non_json: " + str(e)[:60]; time.sleep(2); continue
            if not isinstance(obj, dict):
                meta["err"] = "non_json: not an object"; time.sleep(2); continue
            meta["err"] = ""
            return obj, meta
        except Exception as e:                                   # noqa: BLE001
            meta["latency_ms"] = int((time.time() - t0) * 1000)
            meta["err"] = type(e).__name__ + ": " + str(e)[:80]; time.sleep(2)
    return None, meta


DROPPED = Counter()          # post-filter drop reasons (summary.n_dropped)
_PUNCT = re.compile(r"[\s\u3000-\u303f\uff00-\uffef\u2000-\u206f!-/:-@\[-`{-~]+")

def _squash(t):
    """Whitespace + CJK/ASCII punctuation removed: quotes copied by the model often differ only there."""
    return _PUNCT.sub("", t or "")

def norm_edges(obj, chunk, known):
    """Validate the model object against the schema; never invents, only drops/flags. Returns (edges, overflow).
    Post-filters (2026-09-10 08:4x, from judging Issue #581 sample): an R edge needs a formula member; an F edge
    with <2 members and no verbatim quote is noise; quote match falls back to a punctuation-free comparison."""
    formulas, herbs, syndromes = known[0], known[1], known[2]
    herb_heads = known[3] if len(known) > 3 else herbs
    text_sq = _squash(chunk["text"])
    raw = obj.get("hyperedges")
    if not isinstance(raw, list):
        return [], False
    overflow = len(raw) > MAX_EDGES
    edges = []
    for e in raw[:MAX_EDGES]:
        if not isinstance(e, dict): continue
        typ = str(e.get("type") or "").strip().upper()[:1]
        head = str(e.get("head") or "").strip()
        if typ not in ("F", "R") or not head: continue
        members = []
        for m in e.get("members") or []:
            if not isinstance(m, dict): continue
            label = str(m.get("label") or "").strip()
            if not label: continue
            role = m.get("role"); role = str(role).strip() if role not in (None, "", "null") else None
            dose = m.get("dose"); dose = str(dose).strip() if dose not in (None, "", "null") else None
            if typ == "F":
                if role not in ROLES_F: role = None
                pool = herbs
            else:
                if role not in ROLES_R: role = None
                pool = formulas if role == "formula" else syndromes if role == "syndrome" else None
            known_flag = (label in pool) if pool is not None else None
            mm = {"label": label, "role": role, "dose": dose, "known": known_flag}
            if known_flag is False: mm["unknown_node"] = True
            members.append(mm)
        if not members: continue
        q = str(e.get("source_quote") or "").strip()[:60]
        in_text = bool(q) and (q in chunk["text"] or (len(_squash(q)) >= 6 and _squash(q) in text_sq))
        if typ == "R" and not any(m["role"] == "formula" for m in members):
            DROPPED["R_no_formula"] += 1; continue
        if typ == "F" and len(members) < 2 and not in_text:
            DROPPED["F_thin"] += 1; continue
        # second filter set (2026-09-10 09:5x, from judging Issue #582: all 9 W were machine-catchable)
        if typ == "R" and not any(m["role"] == "formula" and m["label"] in formulas for m in members):
            DROPPED["R_formula_unknown"] += 1; continue
        if typ == "F":
            addsub = sum(1 for m in members if any(ch in ((m.get("dose") or "") + m["label"][:1]) for ch in "\u52a0\u51cf"))
            if addsub * 2 >= len(members):
                DROPPED["F_addsub"] += 1; continue
            if head in herbs or head in herb_heads:
                DROPPED["F_head_is_herb"] += 1; continue
            # sixth filter set (Issue #589)
            if any(m["label"] and m["label"][-1] in "\u6c64\u6563\u4e38\u4e39\u818f\u996e\u714e\u6e6f\u98f2" and len(m["label"]) >= 3 for m in members):
                DROPPED["F_member_is_formula"] += 1; continue
            if len(head) > 20:
                DROPPED["F_head_too_long"] += 1; continue
            # third filter set (2026-09-10 12:3x, Issue #584 judging): a formula whose members are not written in the
            # chunk was filled in from model knowledge (name-only mentions) -- drop it.
            # fourth filter set (Issue #586): check members inside a +-160 char window around the head mention,
            # not the whole chunk -- a name-only mention of formula X passed because X's herbs occur in other
            # formulas of the same chunk (bu-zhong-yi-qi / ge-gen-qiang-huo cases).
            hp = chunk["text"].find(head)
            win = chunk["text"][max(0, hp - 160): hp + 160 + len(head)] if hp >= 0 else chunk["text"]
            win_sq = _squash(win)
            in_txt = sum(1 for m in members if m["label"] and (m["label"] in win or _squash(m["label"]) in win_sq))
            if in_txt < 2 and not (len(members) == 1 and members[0].get("dose")):
                DROPPED["F_members_not_in_text"] += 1; continue
            # seventh filter set (Issue #590): symptom lists typed as F members; members that are just pieces of the head
            if len(members) >= 2 and not any(m["label"] in herb_heads or m["label"] in herbs for m in members):
                DROPPED["F_no_known_herb"] += 1; continue
            if members and all(m["label"] and m["label"] in head for m in members):
                DROPPED["F_members_from_head"] += 1; continue
            if hp >= 0 and members:
                after = chunk["text"][hp + len(head): hp + len(head) + 120]
                first = members[0]["label"]
                if first and first not in after and _squash(first) not in _squash(after):
                    DROPPED["F_members_far"] += 1; continue
            if not (e.get("source_quote") or "").strip() and head not in formulas:
                DROPPED["F_no_quote_unknown_head"] += 1; continue
            # fifth filter set (Issue #587): "X jia Y, Z" (add Y and Z to formula X) written right after the head with
            # dose-less members is an addition to X, not X's composition (liu-wei-wan + mai-dong/wu-wei case).
            if hp >= 0:
                tail = chunk["text"][hp + len(head): hp + len(head) + 12]
                nodose = sum(1 for m in members if not (m.get("dose") or "").strip())
                if any(k in tail for k in ("\u52a0", "\u51cf")) and nodose * 2 >= len(members):
                    DROPPED["F_addsub_text"] += 1; continue
        try:
            conf = float(e.get("confidence")) if e.get("confidence") is not None else None
        except (TypeError, ValueError):
            conf = None
        edges.append({"type": typ, "head": head,
                      "head_known": (head in formulas) if typ == "F" else (head in syndromes),
                      "members": members, "source_quote": q, "quote_in_text": in_text,
                      "confidence": conf})
        hk = edges[-1]["head_known"]
        edges[-1]["tier"] = "A" if (in_text and hk) else "B" if in_text else "C"
        if typ == "F" and not hk and not head.endswith(tuple("\u6c64\u6563\u4e38\u4e39\u818f\u9152\u714e\u996e\u65b9\u5242\u997c\u952d\u9732\u7ca5\u6c41\u4e39\u818f\u6cb9\u6d74\u6d17\u5242\u6761\u7ebf\u6813")):
            edges[-1]["head_is_indication"] = True     # unnamed formula: head is the indication sentence (kept, flagged)
    return edges, overflow


def process(chunk, known):
    obj, meta = call_gateway(chunk)
    rec = {"chunk_id": chunk["chunk_id"], "text_id": chunk["text_id"], "vol_no": chunk["vol_no"],
           "n_chars": chunk["n_chars"], "n_parts": chunk["n_parts"], "hyperedges": [], "overflow": False}
    if obj is not None:
        rec["hyperedges"], rec["overflow"] = norm_edges(obj, chunk, known)
        if TIER_A_ONLY:                                          # S4 mode: staging takes tier A only
            rec["hyperedges"] = [e for e in rec["hyperedges"] if e.get("tier") == "A"]
    rec.update(meta)
    time.sleep(SLEEP_BETWEEN)
    return rec


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def summarize(recs, sample_stats, known):
    ok = [r for r in recs if not r["err"]]
    edges = [e for r in ok for e in r["hyperedges"]]
    f = [e for e in edges if e["type"] == "F"]; rr = [e for e in edges if e["type"] == "R"]
    checkable = [m for e in edges for m in e["members"] if m["known"] is not None]
    hit = sum(1 for m in checkable if m["known"])
    lat = sorted(r["latency_ms"] for r in ok if r["latency_ms"] is not None)
    prov = {}
    for r in recs:
        p = r["provider"] or "none"
        d = prov.setdefault(p, {"calls": 0, "ok": 0})
        d["calls"] += 1; d["ok"] += 0 if r["err"] else 1
    for d in prov.values(): d["ok_pct"] = pct(d["ok"], d["calls"])
    err_kinds = Counter((r["err"].split(":")[0] if r["err"] else "") for r in recs if r["err"])
    return {
        "prompt_version": PROMPT_VERSION, "seed": SEED, "threads": THREADS, "limit": LIMIT, "text_max": TEXT_MAX,
        "known_sizes": {"formulas": len(known[0]), "herbs": len(known[1]), "syndromes": len(known[2])},
        "sample_filters": sample_stats,
        "n_chunks": len(recs), "n_ok": len(ok), "n_fail": len(recs) - len(ok), "ok_pct": pct(len(ok), len(recs)),
        "err_kinds": dict(err_kinds), "n_truncated": sum(1 for r in recs if r.get("truncated")),
        "n_overflow": sum(1 for r in ok if r["overflow"]),
        "n_dropped": dict(DROPPED),
        "tiers": dict(Counter(e.get("tier") for e in edges)),
        "n_edges": len(edges), "n_F": len(f), "n_R": len(rr),
        "chunks_with_edges": sum(1 for r in ok if r["hyperedges"]),
        "edges_per_ok_chunk": round(len(edges) / len(ok), 2) if ok else 0,
        "members_checkable": len(checkable), "members_known": hit, "known_hit_pct": pct(hit, len(checkable)),
        "head_known_pct": pct(sum(1 for e in edges if e["head_known"]), len(edges)),
        "quote_in_text_pct": pct(sum(1 for e in edges if e["quote_in_text"]), len(edges)),
        "F_role_filled_pct": pct(sum(1 for e in f for m in e["members"] if m["role"]), sum(len(e["members"]) for e in f)),
        "providers": prov,
        "latency_ms": {"p50": (statistics.median(lat) if lat else None),
                       "p95": (lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None),
                       "max": (lat[-1] if lat else None)},
        "attempts_total": sum(r["attempts"] for r in recs),
    }


def sample_md(recs, n=SAMPLE_N):
    flat = [(r, e) for r in recs if not r["err"] for e in r["hyperedges"]]
    pick = random.Random(SEED).sample(flat, min(n, len(flat)))
    lines = [SAMPLE_HEAD.format(seed=SEED, total=len(flat))]
    for i, (r, e) in enumerate(pick, 1):
        mem = " / ".join("%s%s%s%s" % (m["label"], "?" if m["known"] is False else "",
                                        ("\u00b7" + m["role"]) if m["role"] else "",
                                        ("\u00b7" + m["dose"]) if m["dose"] else "") for m in e["members"])
        q = e["source_quote"].replace("|", "/").replace("\n", " ") + ("" if e["quote_in_text"] else " !")
        lines.append("| %d | %s | %s/v%s/%s | %s%s | %s | %s | %s |  |" % (
            i, e["type"], r["text_id"], r["vol_no"], r["chunk_id"].rsplit("_", 1)[-1], e["head"],
            "" if e["head_known"] else "?", mem.replace("|", "/"), q, e["confidence"] if e["confidence"] is not None else ""))
    return "\n".join(lines) + "\n"


def main():
    t_start = time.time()
    rng = random.Random(SEED)
    known = load_known()
    chunks, sample_stats = sample_chunks(rng)
    print("calling gateway: %d chunks x threads=%d" % (len(chunks), THREADS), flush=True)
    recs = [None] * len(chunks)
    done = [0]

    def run(i):
        try:
            recs[i] = process(chunks[i], known)
        except Exception as e:                                   # noqa: BLE001  one bad record must not kill the run
            c = chunks[i]
            recs[i] = {"chunk_id": c["chunk_id"], "text_id": c["text_id"], "vol_no": c["vol_no"], "n_chars": c["n_chars"],
                       "n_parts": c["n_parts"], "hyperedges": [], "overflow": False, "provider": None, "latency_ms": None,
                       "err": "internal: " + type(e).__name__ + ": " + str(e)[:80], "attempts": 0, "truncated": False}
        with LOCK:
            done[0] += 1
            # incremental flush: a job killed at the timeout cap still leaves a usable partial artifact
            try:
                os.makedirs(OUT_DIR, exist_ok=True)
                with open(os.path.join(OUT_DIR, "hyper_s3_partial.jsonl"), "a", encoding="utf-8") as pf:
                    pf.write(json.dumps(recs[i], ensure_ascii=False) + chr(10))
            except Exception:                                    # noqa: BLE001
                pass
            if done[0] % 25 == 0 or done[0] == len(chunks):
                nf = sum(1 for r in recs if r and r["err"])
                print("  %d/%d failed=%d elapsed=%ds" % (done[0], len(chunks), nf, time.time() - t_start), flush=True)
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        list(ex.map(run, range(len(chunks))))
    recs = [r for r in recs if r]
    summ = summarize(recs, sample_stats, known)
    summ["elapsed_s"] = int(time.time() - t_start)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "hyper_s3.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs: fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    json.dump(summ, open(os.path.join(OUT_DIR, "hyper_s3_summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    smd = sample_md(recs)
    open(os.path.join(OUT_DIR, "sample30.md"), "w", encoding="utf-8").write(smd)
    day = datetime.date.today().isoformat()
    title = ("[S4 tier-A staging] " if TIER_A_ONLY else "") + ISSUE_TITLE.format(day=day, n=summ["n_chunks"], e=summ["n_edges"], ok=summ["ok_pct"], hit=summ["known_hit_pct"])
    body = ISSUE_HEAD + "\n\n```json\n" + json.dumps(summ, ensure_ascii=False, indent=1) + "\n```\n\n" + smd
    open(os.path.join(OUT_DIR, "issue.md"), "w", encoding="utf-8").write(body)
    print(title); print(json.dumps(summ, ensure_ascii=False))
    open(os.environ.get("GITHUB_STEP_SUMMARY", os.path.join(OUT_DIR, "summary.md")), "a", encoding="utf-8").write(
        "```\n" + title + "\n" + json.dumps(summ, ensure_ascii=False, indent=1) + "\n```\n")
    if os.environ.get("GITHUB_REPOSITORY"):
        import subprocess
        subprocess.run(["gh", "issue", "create", "-R", os.environ["GITHUB_REPOSITORY"], "-t", title,
                        "-F", os.path.join(OUT_DIR, "issue.md")], check=False)


if __name__ == "__main__":
    main()
