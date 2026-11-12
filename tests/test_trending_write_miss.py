# -*- coding: utf-8 -*-
"""A failed trending write is not a saved zero.

save() used to return 0 when the insert raised, and main() still exited
from the sweep miss alone. A real empty row dict stays 0 and is not a
failed write. This test does not call D1 or GitHub.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import trending  # noqa: E402

ROW = {
    "repo": "a/b",
    "url": "https://github.com/a/b",
    "description": "d",
    "stars": 3,
    "forks": 1,
    "lang": "py",
    "topics": "",
    "license": "",
    "created_at": "2026-01-01",
    "pushed_at": "2026-11-01",
    "found_by": "trending:created",
}


class FailedTrendingWrite(unittest.TestCase):
    def setUp(self):
        self._d1 = trending.d1

    def tearDown(self):
        trending.d1 = self._d1

    def test_failed_flush_is_not_a_saved_zero(self):
        def boom(sql):
            if "INSERT" in sql:
                raise RuntimeError("down")
            return []

        trending.d1 = boom
        n, failed = trending.save({"a/b": ROW})
        self.assertTrue(failed)
        self.assertEqual(n, 0)

    def test_a_real_empty_save_stays_zero(self):
        trending.d1 = lambda sql: []
        n, failed = trending.save({})
        self.assertFalse(failed)
        self.assertEqual(n, 0)

    def test_write_failure_exits_even_when_the_sweep_kept_everything(self):
        self.assertEqual(trending.exit_code(0, write_failed=True), 1)
        self.assertEqual(trending.exit_code(0), 0)
        self.assertEqual(trending.exit_code(4), 1)

    def test_report_marks_the_failed_write_and_keeps_a_real_zero(self):
        bad = trending.report_payload({"a/b": ROW}, 0, 9, 1, None, write_failed=True)
        self.assertTrue(bad["write_failed"])
        self.assertEqual(bad["saved"], 0)
        good = trending.report_payload({}, 0, 0, 0, None)
        self.assertFalse(good["write_failed"])
        self.assertEqual(good["saved"], 0)
        self.assertEqual(good["pool_total"], 0)
