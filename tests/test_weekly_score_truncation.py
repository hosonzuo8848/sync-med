# -*- coding: utf-8 -*-
"""A cut-off score reply is not a finished batch of zero hits.

score_items used to do pick_json(...) or []. A reply that never closes
its JSON value makes pick_json return None, and None or [] is []. The
batch then prints the completion line and returns no rows. main() files
the weekly issue as if those items had been judged and none mattered.

A model that actually returns [] has made a judgment. That stays empty.
This test does not call the network, D1, or a model.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "intel_radar"))

import weekly_hunter_v1 as hunter  # noqa: E402


def _item(i):
    return {
        "lane": "lane",
        "title": "title-%d" % i,
        "brief": "brief",
        "url": "https://example.test/%d" % i,
    }


class TruncatedScoreBatch(unittest.TestCase):
    def setUp(self):
        self._llm = hunter.llm

    def tearDown(self):
        hunter.llm = self._llm

    def _score(self, reply):
        def fake(system, user, max_tokens=1600, timeout=120):
            return reply

        hunter.llm = fake
        return hunter.score_items([_item(0), _item(1)])

    def test_truncated_payload_is_not_a_finished_empty_score(self):
        # No closing brace and no closing bracket: pick_json returns None.
        # The old `or []` turned that into a completed score of nothing.
        with self.assertRaises(Exception) as ctx:
            self._score('[{"i": 0, "leading": 5, "transplant":')
        self.assertIn("truncated", str(ctx.exception).lower())

    def test_explicit_empty_array_stays_a_judgment(self):
        self.assertEqual(self._score("[]"), [])

    def test_closed_array_still_scores(self):
        scored = self._score(
            '[{"i": 0, "leading": 5, "transplant": 4, "move": "keep"}]'
        )
        self.assertEqual(len(scored), 1)
        self.assertEqual(scored[0]["leading"], 5)
        self.assertEqual(scored[0]["transplant"], 4)
        self.assertEqual(scored[0]["move"], "keep")

    def test_object_payload_is_not_a_finished_empty_score(self):
        # A closed object, or an array that never closes but contains one,
        # is not a list of judgments. The old loop treated that as a
        # finished batch and still printed the completion line.
        with self.assertRaises(hunter.ScoreIncomplete) as ctx:
            self._score('[{"i": 0, "leading": 5, "transplant": 4, "move": "keep"}')
        self.assertIn("truncated", str(ctx.exception).lower())
