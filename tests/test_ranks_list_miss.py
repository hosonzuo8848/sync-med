# -*- coding: utf-8 -*-
"""A failed rank directory listing is not zero periods.

list_files() used to return [] after a non-404 error, and the round still
looked like an empty board. A 404 year stays a real empty list. A listed
markdown file stays. This test does not call GitHub.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import ranks  # noqa: E402


class Missing(Exception):
    code = 404


class FailedRankList(unittest.TestCase):
    def setUp(self):
        self._gh = ranks.gh_json

    def tearDown(self):
        ranks.gh_json = self._gh

    def test_unread_year_is_not_zero_periods(self):
        def boom(_path):
            raise RuntimeError("down")

        ranks.gh_json = boom
        files, failed = ranks.list_files("o/r", ["2026"])
        self.assertTrue(failed)
        self.assertEqual(files, [])

    def test_missing_year_stays_empty(self):
        def missing(_path):
            raise Missing("gone")

        ranks.gh_json = missing
        files, failed = ranks.list_files("o/r", ["2026"])
        self.assertFalse(failed)
        self.assertEqual(files, [])

    def test_a_real_period_stays(self):
        def ok(_path):
            return [{"type": "file", "name": "01.md", "path": "2026/01.md"}]

        ranks.gh_json = ok
        files, failed = ranks.list_files("o/r", ["2026"])
        self.assertFalse(failed)
        self.assertEqual(files, [("2026", "01.md", "2026/01.md")])

    def test_finish_code_on_an_unread_list(self):
        self.assertEqual(ranks.finish_code(0, False, False, list_failed=True), 1)
        self.assertEqual(ranks.finish_code(0, False, False), 0)
