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


if __name__ == "__main__":
    unittest.main()
