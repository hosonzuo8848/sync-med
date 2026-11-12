# -*- coding: utf-8 -*-
"""A failed rank write is not a measured zero.

save() used to return 0 when the insert raised, and main() still exited 0.
The report then looked like an empty board. A real empty row list stays 0
and is not a failed write. This test does not call D1 or GitHub.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import ranks  # noqa: E402

ROW = {
    "repo": "a/b",
    "url": "https://github.com/a/b",
    "description": "d",
    "stars": 1,
    "born": "",
    "found_by": "rank:daily:2026",
    "star_delta": 3,
    "rank": 1,
    "period": "2026-1",
    "kind": "daily",
    "d_delta": 1,
    "w_delta": 0,
    "m_delta": 0,
}


class FailedRankWrite(unittest.TestCase):
    def setUp(self):
        self._d1 = ranks.d1

    def tearDown(self):
        ranks.d1 = self._d1

    def test_failed_flush_is_not_a_saved_zero(self):
        def boom(sql):
            if "INSERT" in sql:
                raise RuntimeError("down")
            return []

        ranks.d1 = boom
        n, failed = ranks.save([ROW])
        self.assertTrue(failed)
        self.assertEqual(n, 0)

    def test_a_real_empty_save_stays_zero(self):
        ranks.d1 = lambda sql: []
        n, failed = ranks.save([])
        self.assertFalse(failed)
        self.assertEqual(n, 0)

    def test_failed_growth_flush_is_not_zero_saved(self):
        def boom(sql):
            if "INSERT" in sql:
                raise RuntimeError("down")
            return []

        ranks.d1 = boom
        n, failed = ranks.save_growth([ROW])
        self.assertTrue(failed)
        self.assertEqual(n, 0)

    def test_finish_code_only_when_rows_were_unread(self):
        self.assertEqual(ranks.finish_code(4, True, False), 1)
        self.assertEqual(ranks.finish_code(4, False, True), 1)
        self.assertEqual(ranks.finish_code(0, False, False), 0)
        self.assertEqual(ranks.finish_code(4, False, False), 0)

    def test_pool_count_failure_is_unread_not_a_number(self):
        self.assertEqual(ranks.pool_total_field(12, False), 12)
        self.assertEqual(ranks.pool_total_field(0, False), 0)
        self.assertEqual(ranks.pool_total_field(None, True), "unread")
