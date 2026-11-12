# -*- coding: utf-8 -*-
"""screen_candidate must hard-skip repos already in the stack.

The prefilter loop used to call _prefilter only, so unclecode/crawl4ai still
reached the model when crawl4ai was already owned. These cases stay on
screen_candidate: no main(), no D1, no scan of the real stack, no ask().
"""
import inspect
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts", "evolve"))

import adopt  # noqa: E402


def _owned_row():
    # Description hits the existing SIGNAL list ("crawler"), so the old
    # _prefilter-only loop would have sent this repo to judge().
    return {
        "repo": "unclecode/crawl4ai",
        "description": "LLM friendly web crawler and scraper",
    }


class TestScreenCandidateAlreadyHave(unittest.TestCase):
    def test_owned_crawl4ai_does_not_reach_judge(self):
        row = _owned_row()
        pre_ok, _why = adopt._prefilter(row)
        self.assertTrue(pre_ok, "fixture must still clear the unchanged word list")
        ok, why = adopt.screen_candidate(row, {"crawl4ai"})
        self.assertFalse(ok)
        self.assertEqual(why, "already_have")

    def test_unowned_repo_still_passes_existing_prefilter(self):
        row = {
            "repo": "example/page-ocr",
            "description": "ocr for scanned book pages",
        }
        ok, why = adopt.screen_candidate(row, {"crawl4ai"})
        pre_ok, pre_why = adopt._prefilter(row)
        self.assertTrue(pre_ok)
        self.assertEqual((ok, why), (True, pre_why))

    def test_full_name_is_used_when_repo_missing(self):
        row = {
            "full_name": "unclecode/crawl4ai",
            "description": "LLM friendly web crawler and scraper",
        }
        ok, why = adopt.screen_candidate(row, {"crawl4ai"})
        self.assertFalse(ok)
        self.assertEqual(why, "already_have")

    def test_main_loop_calls_screen_candidate(self):
        # Read the source only. Do not execute main(), d1(), or scan_stack().
        src = inspect.getsource(adopt.main)
        self.assertIn("screen_candidate(r, STACK_NAMES)", src)
        self.assertNotIn("_prefilter(", src)

    def test_tally_screen_splits_owned_from_wordlist(self):
        # Three rows: one owned hit, one word-list cut, one that clears the
        # gate. A single dropped counter would report 2 and could not split
        # them. Do not call main(), d1(), or scan_stack().
        owned = _owned_row()
        cut = {
            "repo": "example/notes-app",
            "description": "personal notes about cooking",
        }
        keep = {
            "repo": "example/page-ocr",
            "description": "ocr for scanned book pages",
        }
        stack = {"crawl4ai"}
        pre_ok, _why = adopt._prefilter(owned)
        self.assertTrue(pre_ok, "owned fixture must still clear the word list")
        cut_ok, cut_why = adopt.screen_candidate(cut, stack)
        self.assertFalse(cut_ok)
        self.assertNotEqual(cut_why, "already_have")
        rows = [owned, cut, keep]
        kept, n_owned, n_other = adopt.tally_screen(rows, stack)
        self.assertEqual(n_owned, 1)
        self.assertEqual(n_other, 1)
        self.assertEqual(len(kept), 1)
        self.assertIs(kept[0], keep)
        self.assertNotIn("_pre", owned)
        self.assertNotIn("_pre", cut)
        self.assertNotIn("_pre", keep)

    def test_tally_screen_keeps_input_order(self):
        first = {
            "repo": "example/page-ocr",
            "description": "ocr for scanned book pages",
        }
        second = {
            "repo": "example/embed-index",
            "description": "vector embed index",
        }
        kept, n_owned, n_other = adopt.tally_screen(
            [first, _owned_row(), second], {"crawl4ai"})
        self.assertEqual(n_owned, 1)
        self.assertEqual(n_other, 0)
        self.assertIs(kept[0], first)
        self.assertIs(kept[1], second)

    def test_main_reports_owned_and_other_separately(self):
        # Source only. The old loop added every miss into one dropped.
        src = inspect.getsource(adopt.main)
        self.assertIn("tally_screen(fresh, STACK_NAMES)", src)
        self.assertIn("n_owned", src)
        self.assertIn("n_other", src)
        self.assertNotIn("dropped += 1", src)
        self.assertIn("\\u5bb6\\u5e95\\u8df3\\u8fc7", src)
        self.assertIn("\\u8bcd\\u8868\\u780d\\u6389", src)


if __name__ == "__main__":
    unittest.main()
