# -*- coding: utf-8 -*-
"""A failed month-board read is not a full harvest.

load_repo_candidates() used to return only the file fallback after the
pool query raised, and main() still exited 0. A real empty pool stays a
successful read and can still merge the files. The star-sort fallback is
a second query that answered, not an unread pool. This test does not
call D1.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import adopt  # noqa: E402


class FailedPoolRead(unittest.TestCase):
    def setUp(self):
        self._d1 = adopt.d1

    def tearDown(self):
        adopt.d1 = self._d1

    def test_failed_pool_is_not_a_full_harvest(self):
        def boom(_sql):
            raise RuntimeError("down")

        adopt.d1 = boom
        _rows, failed = adopt.load_repo_candidates()
        self.assertTrue(failed)

    def test_a_real_empty_pool_can_still_merge(self):
        adopt.d1 = lambda _sql: []
        rows, failed = adopt.load_repo_candidates()
        self.assertFalse(failed)
        self.assertIsInstance(rows, list)

    def test_star_fallback_that_answers_is_not_unread(self):
        def fake(sql):
            if "CASE WHEN" in sql:
                raise RuntimeError("no column")
            return [{"repo": "a/b", "topics": ""}]

        adopt.d1 = fake
        rows, failed = adopt.load_repo_candidates()
        self.assertFalse(failed)
        self.assertTrue(any(r.get("repo") == "a/b" for r in rows))
