# -*- coding: utf-8 -*-
"""Race-clock wording: an old stored comparison is not "the job did not run".

last_race is MAX(race_results.created_at). judge() has no second timestamp
for the Evolve Race workflow, so a stale value must stay an issue and must
name the comparison. Offline: judge() does not open a socket.

Usage: python -m pytest tests/test_evolve_controller_race_clock.py -q
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import evolve_controller as EC  # noqa: E402


class RaceClockWording(unittest.TestCase):
    def test_old_comparison_names_comparison_and_stays_an_issue(self):
        # 2026-11-12 07:38 UTC, the run that printed 2458 hours.
        now = 1794469080
        last_comparison = now - 2458 * 3600
        issues, oks = EC.judge({
            "at": now,
            "content_24h": EC.TH["content_min_per_day"],
            "runs_24h": [],
            "last_race": last_comparison,
            "last_discover": now,
            "candidates": 1,
        })
        stall = [s for s in issues if "\u8bc4\u4f30\u73af" in s]
        self.assertEqual(len(stall), 1)
        text = stall[0]
        self.assertIn("\u6bd4\u8f83", text)
        self.assertNotIn("\u6ca1\u8dd1", text)
        self.assertIn("\u4f5c\u4e1a", text)
        self.assertEqual(text.count("2458"), 1)
        self.assertIn(str(EC.TH["race_stale_hours"]), text)
        self.assertFalse(any("\u8bc4\u4f30\u73af" in s for s in oks))


if __name__ == "__main__":
    unittest.main()
