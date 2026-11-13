# -*- coding: utf-8 -*-
"""A failed rank download is not an empty period.

The period loop used to skip a raised fetch and, when every period
failed, warn that the board format had changed. A markdown body that
was actually read still parses. Zero items after a real read is still
a format change. This test does not call GitHub.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import ranks  # noqa: E402


class FailedPeriodRead(unittest.TestCase):
    def test_failed_download_is_not_an_empty_period(self):
        def boom(_repo, _path):
            raise RuntimeError("down")

        md, failed = ranks.read_period(boom, "o/r", "2026/01.md", "2026-01")
        self.assertTrue(failed)
        self.assertEqual(md, "")

    def test_a_real_markdown_stays(self):
        md, failed = ranks.read_period(
            lambda _repo, _path: "# board\n", "o/r", "2026/01.md", "2026-01")
        self.assertFalse(failed)
        self.assertIn("board", md)

    def test_zero_items_after_a_failed_download_is_not_a_format_change(self):
        self.assertFalse(ranks.format_changed(3, 0, True))
        self.assertTrue(ranks.format_changed(3, 0, False))
        self.assertFalse(ranks.format_changed(0, 0, False))

    def test_finish_code_on_an_unread_period(self):
        self.assertEqual(ranks.finish_code(0, False, False, fetch_failed=True), 1)
        self.assertEqual(ranks.finish_code(4, False, False), 0)
