# -*- coding: utf-8 -*-
"""A failed controller read is not a measured zero.

scalar() used to catch every error and return 0. judge() then scored
"0 new rows" as a real gap, and track_goals() could write that 0 back
and halt a goal for making no progress.

A stored 0 stays 0. A missing read stays missing. This test does not
call Cloudflare, D1, or a model.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import evolve_controller as ec  # noqa: E402


def _goal():
    return {
        "goal_key": "g1",
        "scope": "content_biocomp",
        "target_value": 100,
        "last_value": 10,
        "rounds_used": 0,
        "no_progress_rounds": 2,
        "no_progress_limit": 3,
        "unit": "n",
        "max_rounds": 99,
    }


class FailedReadIsNotZero(unittest.TestCase):
    def setUp(self):
        self._d1 = ec.d1

    def tearDown(self):
        ec.d1 = self._d1

    def test_scalar_raises_when_the_read_fails(self):
        def boom(sql):
            raise RuntimeError("down")

        ec.d1 = boom
        with self.assertRaises(RuntimeError):
            ec.scalar("SELECT 1")

    def test_scalar_keeps_a_stored_zero(self):
        ec.d1 = lambda sql: [{"c": 0}]
        self.assertEqual(ec.scalar("SELECT 0"), 0)

    def test_scalar_empty_result_is_not_zero(self):
        ec.d1 = lambda sql: []
        self.assertIsNone(ec.scalar("SELECT MAX(x)"))

    def test_judge_does_not_score_a_missing_count(self):
        issues, _oks = ec.judge({
            "at": 1_700_000_000,
            "content_24h": None,
            "runs_24h": [],
            "last_race": None,
            "last_discover": None,
        })
        self.assertFalse(any("24h" in item for item in issues))

    def test_judge_still_scores_a_real_zero(self):
        issues, _oks = ec.judge({
            "at": 1_700_000_000,
            "content_24h": 0,
            "runs_24h": [],
            "last_race": None,
            "last_discover": None,
        })
        self.assertTrue(any("24h" in item for item in issues))

    def test_missing_total_does_not_halt_a_goal(self):
        _reached, halted, _running = ec.track_goals(
            {"at": 1, "bio_total": None, "herb_total": 5, "champion": [],
             "goals": [_goal()]},
            True,
        )
        self.assertEqual(halted, [])
