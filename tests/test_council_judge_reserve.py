# -*- coding: utf-8 -*-
"""The last budgeted call is kept for the verdict."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "council"))

import llm_roster as roster  # noqa: E402


class JudgeReserve(unittest.TestCase):
    def setUp(self):
        self.old_cap = roster.MAX_CALLS
        self.old_state = dict(roster._state)
        roster.MAX_CALLS = 3
        roster._state["calls"] = 2

    def tearDown(self):
        roster.MAX_CALLS = self.old_cap
        roster._state.clear()
        roster._state.update(self.old_state)

    def test_probe_stops_when_only_the_judge_slot_remains(self):
        with self.assertRaises(roster.BudgetExhausted):
            roster._reserve_call("probe")
        self.assertEqual(roster._state["calls"], 2)

    def test_verdict_may_use_the_last_slot(self):
        n = roster._reserve_call("verdict")
        self.assertEqual(n, 3)
        with self.assertRaises(roster.BudgetExhausted):
            roster._reserve_call("verdict")


class SensenovaThinking(unittest.TestCase):
    def test_sensenova_seat_disables_thinking(self):
        row = next(r for r in roster._FREE_BENCH if r[0] == "sensenova")
        self.assertEqual(row[6], {"thinking": {"type": "disabled"}})


if __name__ == "__main__":
    unittest.main()
