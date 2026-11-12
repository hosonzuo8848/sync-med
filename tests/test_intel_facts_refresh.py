# -*- coding: utf-8 -*-
"""Offline refresh when an intel page already exists and its hash changed.

The first capture can store books_n=0 because full text was missing.
mode_llm only selects books_n=1. A later fulltext_ok fact whose
source_hash differs must rewrite that row, including books_n, or the
page never reaches judgment. An unchanged source_hash must still skip.

No D1, no gateway, no repo judge. The article title below contains
"retriev", which is a repo-prefilter signal; the fake query function
rejects any write aimed at the repo pool.
"""
import json
import os
import re
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts", "intel_radar"))

import intel_compile  # noqa: E402


ARTICLE = "Scanned page retrieval for a reading room"
MARKER = "FULLTEXT_MARKER_91ab"


def _source_hash(fact):
    # Same payload mode_facts hashes. A drift here makes the skip test rewrite.
    payload = {k: fact[k] for k in ("title", "url", "text", "github")}
    return intel_compile.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fact(intel_id, text):
    return {
        "kind": "intel",
        "intel_id": intel_id,
        "title": ARTICLE,
        "url": "https://example.com/papers/" + intel_id,
        "source": "example-journal",
        "published_at": "2026-11-01",
        "relevance": 0.91,
        "importance": "high",
        "radar_module": "rag",
        "radar_summary": "abstract only on first capture",
        "fulltext_ok": True,
        "text": text,
        "github": None,
        "tags": ["rag"],
    }


def _item(intel_id):
    return {
        "intel_id": intel_id,
        "title": ARTICLE,
        "summary": "abstract only",
        "url": "https://example.com/papers/" + intel_id,
        "source": "example-journal",
        "published_at": "2026-11-01",
        "relevance_score": 0.91,
        "importance": "high",
        "related_module": "rag",
        "sector": "rag",
    }


def _existing(intel_id, source_hash):
    return {
        "page_id": "intel:" + intel_id,
        "name_s": intel_id,
        "kind": "intel",
        "status": "facts",
        "books_n": 0,
        "source_hash": source_hash,
        "compiled_at": 1,
        "facts_json": "{}",
        "body_md": "OLD_BODY",
    }


def _apply_upsert(sql, store, fact):
    # Model the real INSERT ... ON CONFLICT column list. books_n moves only
    # when the update assigns excluded.books_n; a missing assignment is the
    # defect that leaves the first 0 in place after a rewrite.
    found = re.search(r"VALUES \('([^']+)','intel','([^']+)'", sql)
    if not found:
        raise AssertionError("wiki upsert missing page_id/name_s")
    page_id, name_s = found.group(1), found.group(2)
    tail = re.search(
        r"'([0-9a-f]{16})',(\d+),(\d+),NULL,NULL,(\d+)\)\s*ON CONFLICT",
        sql,
    )
    if not tail:
        raise AssertionError("wiki upsert missing source_hash/books_n tail")
    # VALUES tail is versions_n, books_n, NULL, NULL, compiled_at.
    source_hash = tail.group(1)
    books_n = int(tail.group(3))
    compiled_at = int(tail.group(4))
    row = store.get(name_s)
    if row is None:
        row = {
            "page_id": page_id,
            "name_s": name_s,
            "kind": "intel",
            "status": "facts",
            "books_n": books_n,
            "source_hash": source_hash,
            "compiled_at": compiled_at,
            "facts_json": "{}",
            "body_md": "",
        }
        store[name_s] = row
    else:
        row["page_id"] = page_id
        row["status"] = "facts"
        row["source_hash"] = source_hash
        row["compiled_at"] = compiled_at
        if "books_n=excluded.books_n" in sql:
            row["books_n"] = books_n
    if fact["text"] in sql:
        row["facts_json"] = json.dumps(fact, ensure_ascii=False)
        row["body_md"] = fact["text"]
    return row


class TestIntelFactsRefresh(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("GITHUB_STEP_SUMMARY")
        fd, self._summary = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        os.environ["GITHUB_STEP_SUMMARY"] = self._summary
        self._orig = {
            "d1": intel_compile.d1,
            "candidates": intel_compile.candidates,
            "build_facts": intel_compile.build_facts,
            "call_gateway": intel_compile.call_gateway,
            "jev": intel_compile.jev,
            "http_get": intel_compile.http_get,
            "fetch_fulltext": intel_compile.fetch_fulltext,
            "github_meta": intel_compile.github_meta,
            "arxiv_abstract": intel_compile.arxiv_abstract,
            "sleep": intel_compile.time.sleep,
            "FORCE": intel_compile.FORCE,
            "buf": list(intel_compile._BUF),
        }
        intel_compile._BUF.clear()
        intel_compile.FORCE = False
        intel_compile.time.sleep = lambda *_a, **_k: None

        def _offline(*_a, **_k):
            raise AssertionError("offline test tried a network call")

        intel_compile.http_get = _offline
        intel_compile.fetch_fulltext = _offline
        intel_compile.github_meta = _offline
        intel_compile.arxiv_abstract = _offline
        intel_compile.jev = _offline
        intel_compile.call_gateway = _offline
        self.queries = []
        self.gateway = []

    def tearDown(self):
        intel_compile.d1 = self._orig["d1"]
        intel_compile.candidates = self._orig["candidates"]
        intel_compile.build_facts = self._orig["build_facts"]
        intel_compile.call_gateway = self._orig["call_gateway"]
        intel_compile.jev = self._orig["jev"]
        intel_compile.http_get = self._orig["http_get"]
        intel_compile.fetch_fulltext = self._orig["fetch_fulltext"]
        intel_compile.github_meta = self._orig["github_meta"]
        intel_compile.arxiv_abstract = self._orig["arxiv_abstract"]
        intel_compile.time.sleep = self._orig["sleep"]
        intel_compile.FORCE = self._orig["FORCE"]
        intel_compile._BUF[:] = self._orig["buf"]
        if self._saved_env is None:
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
        else:
            os.environ["GITHUB_STEP_SUMMARY"] = self._saved_env
        try:
            os.remove(self._summary)
        except OSError:
            pass

    def _install(self, store, fact):
        intel_id = fact["intel_id"]

        def fake_d1(sql, params=None):
            self.queries.append(sql)
            if "evolve_candidates" in sql or "gh_repo_pool" in sql or "kind='repo'" in sql:
                raise AssertionError("intel title reached repo judgment: %s" % sql[:180])
            if "SELECT name_s, source_hash" in sql:
                return [
                    {"name_s": row["name_s"], "source_hash": row["source_hash"]}
                    for row in store.values()
                    if ("'" + row["name_s"] + "'") in sql
                ]
            if "INSERT INTO wiki_pages" in sql:
                _apply_upsert(sql, store, fact)
                return []
            if "kind='intel' AND status='facts' AND books_n=1" in sql:
                rows = [
                    row for row in store.values()
                    if row.get("kind") == "intel" and row.get("status") == "facts" and row.get("books_n") == 1
                ]
                rows.sort(key=lambda r: r.get("compiled_at") or 0, reverse=True)
                return [
                    {"page_id": r["page_id"], "facts_json": r["facts_json"], "body_md": r["body_md"]}
                    for r in rows[:intel_compile.LIMIT]
                ]
            return []

        def fake_gateway(user):
            self.gateway.append(user)
            return None, "offline-fake"

        intel_compile.d1 = fake_d1
        intel_compile.candidates = lambda: [_item(intel_id)]
        intel_compile.build_facts = lambda it: fact
        intel_compile.call_gateway = fake_gateway

    def _assert_not_repo_judgment(self):
        for sql in self.queries:
            self.assertNotIn("evolve_candidates", sql)
            self.assertNotIn("gh_repo_pool", sql)
            self.assertNotIn("kind='repo'", sql)

    def test_changed_hash_rewrites_page_and_llm_selects_it(self):
        intel_id = "paper-refresh-1"
        fact = _fact(intel_id, MARKER + " full text captured on the second pass")
        new_hash = _source_hash(fact)
        old_hash = "0" * 16
        self.assertNotEqual(new_hash, old_hash)
        store = {intel_id: _existing(intel_id, old_hash)}
        self._install(store, fact)

        intel_compile.mode_facts()

        upserts = [sql for sql in self.queries if "INSERT INTO wiki_pages" in sql]
        self.assertEqual(len(upserts), 1, "changed hash did not rewrite the existing page")
        self.assertIn("books_n=excluded.books_n", upserts[0])
        self.assertIn("ON CONFLICT(page_id)", upserts[0])
        row = store[intel_id]
        self.assertEqual(set(store), {intel_id})
        self.assertEqual(row["page_id"], "intel:" + intel_id)
        self.assertEqual(row["name_s"], intel_id)
        self.assertNotEqual(row["name_s"], fact["title"])
        self.assertEqual(row["source_hash"], new_hash)
        self.assertNotEqual(row["source_hash"], old_hash)
        self.assertNotEqual(row["compiled_at"], 1)
        self.assertEqual(row["books_n"], 1)
        self.assertIn(MARKER, row["facts_json"])
        self.assertIn(MARKER, row["body_md"])
        self.assertNotIn(ARTICLE, row["name_s"])
        self._assert_not_repo_judgment()

        intel_compile.mode_llm()

        self.assertEqual(len(self.gateway), 1, "mode_llm books_n=1 query did not select the refreshed page")
        self.assertIn(MARKER, self.gateway[0])
        self.assertIn(ARTICLE, self.gateway[0])
        self.assertTrue(any("books_n=1" in sql and "kind='intel'" in sql for sql in self.queries))
        self._assert_not_repo_judgment()

    def test_unchanged_source_hash_skips_rewrite(self):
        intel_id = "paper-same-hash"
        fact = _fact(intel_id, MARKER + " same text as the stored hash")
        same = _source_hash(fact)
        store = {intel_id: _existing(intel_id, same)}
        self._install(store, fact)

        intel_compile.mode_facts()

        upserts = [sql for sql in self.queries if "INSERT INTO wiki_pages" in sql]
        self.assertEqual(upserts, [], "unchanged source_hash rewrote the page")
        row = store[intel_id]
        self.assertEqual(row["source_hash"], same)
        self.assertEqual(row["books_n"], 0)
        self.assertEqual(row["compiled_at"], 1)
        self.assertEqual(row["body_md"], "OLD_BODY")
        self.assertEqual(row["page_id"], "intel:" + intel_id)
        self._assert_not_repo_judgment()

        intel_compile.mode_llm()

        self.assertEqual(self.gateway, [], "unchanged page was sent to judgment")
        self._assert_not_repo_judgment()


if __name__ == "__main__":
    unittest.main()
