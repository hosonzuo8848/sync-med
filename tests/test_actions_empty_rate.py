# -*- coding: utf-8 -*-
"""Zero workflow runs are not a 0% success rate.

build_md used to print 0.0% when the Actions window had no runs, and
0% for a workflow row whose n was 0. A real 0 out of N stays 0.
This test does not call GitHub, Cloudflare, or D1.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import platform_report as pr  # noqa: E402


def _md(actions):
    return pr.build_md(
        "daily",
        "2026-11-12",
        datetime.date(2026, 11, 12),
        datetime.date(2026, 11, 13),
        {},
        {},
        None,
        None,
        {"actions": actions},
    )


class EmptyActionsWindow(unittest.TestCase):
    def test_zero_runs_are_not_zero_percent(self):
        self.assertIsNone(pr.success_rate(0, 0))
        text = _md({
            "success": 0,
            "total": 0,
            "failure": 0,
            "cancelled": 0,
            "per_workflow": {
                "wf": {"n": 0, "success": 0, "failure": 0, "cancelled": 0},
            },
        })
        self.assertIn("no sample", text)
        self.assertNotIn("0.0%", text)
        self.assertNotIn("| 0% |", text)

    def test_real_zero_of_n_stays_zero(self):
        self.assertEqual(pr.success_rate(0, 4), 0.0)
        text = _md({
            "success": 0,
            "total": 4,
            "failure": 4,
            "cancelled": 0,
            "per_workflow": {
                "wf": {"n": 4, "success": 0, "failure": 4, "cancelled": 0},
            },
        })
        self.assertIn("0.0%", text)
        self.assertIn("0%", text)
        self.assertNotIn("no sample", text)
