# -*- coding: utf-8 -*-
"""Offline rank for the eagle-eye daily issue headline.

The 2026-11-11 issue opened on a repo whose description was empty, so the
lead suggestion had nothing to quote. Two candidates: the first description
is empty and newer; the second description is not. After ranking, item 1
must have a non-empty description. No network.
"""
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts", "intel_radar"))

import intel_compile  # noqa: E402


def _candidate(description, summarized_at):
    # Same tags and source, so the old key falls through to freshness.
    # The blank row is newer and therefore used to sort first.
    return {
        "description": description,
        "facts_json": json.dumps({"tags": ["rag"]}),
        "src": "gh-search",
        "summarized_at": summarized_at,
        "title": "repo",
    }


class TestDigestHeadline(unittest.TestCase):
    def test_blank_description_does_not_lead(self):
        blank = _candidate("", 200)
        filled = _candidate("retrieval over scanned pages", 100)
        ranked = intel_compile.rank_digest_rows([blank, filled])
        lead = str(ranked[0].get("description") or "").strip()
        self.assertTrue(lead, "item 1 description is empty: %r" % (ranked[0].get("description"),))

    def test_blank_github_description_in_facts_does_not_lead(self):
        # Digest rows from D1 have no top-level description. The text is
        # facts_json.github.description. A ranker that only reads the
        # top-level key leaves this pair in the old order.
        def row(description, summarized_at):
            return {
                "facts_json": json.dumps({
                    "tags": ["rag"],
                    "github": {"repo": "octo/repo", "description": description},
                }),
                "src": "gh-search",
                "summarized_at": summarized_at,
                "title": "octo/repo",
            }

        ranked = intel_compile.rank_digest_rows([row("", 200), row("retrieval over scanned pages", 100)])
        gh = json.loads(ranked[0]["facts_json"])["github"]["description"]
        self.assertTrue(str(gh or "").strip(), "item 1 github description is empty: %r" % (gh,))


if __name__ == "__main__":
    unittest.main()
