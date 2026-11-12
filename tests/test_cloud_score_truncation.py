# -*- coding: utf-8 -*-
"""A cut-off cloud-report reply is not a finished batch of zero hits.

analyze_relevance used to return [] when the model reply never closed,
and again when the call itself failed. main() then wrote the report and
filed the issue as if those papers had been judged and none mattered.

A model that actually returns [] has made a judgment. That stays empty.
This test does not call the network, D1, or a model, and it does not
call main().
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "intel_radar"))

import daily_report_cloud as cloud  # noqa: E402


PAPER = {
    "title": "t",
    "url": "https://example.test/1",
    "source": "s",
    "abstract": "a",
}


class TruncatedCloudScore(unittest.TestCase):
    def setUp(self):
        self._chat = cloud.llm_chat

    def tearDown(self):
        cloud.llm_chat = self._chat

    def _reply(self, reply):
        def fake(messages, max_tokens=2000):
            return reply

        cloud.llm_chat = fake
        return cloud.analyze_relevance([PAPER])

    def test_truncated_payload_is_not_a_finished_empty_score(self):
        def fake(messages, max_tokens=2000):
            return '[{"index": 1, "reason":'

        cloud.llm_chat = fake
        with self.assertRaises(cloud.BatchUnscored) as ctx:
            cloud.analyze_relevance([PAPER])
        self.assertIn("truncated", str(ctx.exception).lower())

    def test_transport_failure_is_not_a_finished_empty_score(self):
        def fake(messages, max_tokens=2000):
            raise RuntimeError("down")

        cloud.llm_chat = fake
        with self.assertRaises(cloud.BatchUnscored) as ctx:
            cloud.analyze_relevance([PAPER])
        self.assertIn("llm call failed", str(ctx.exception).lower())

    def test_explicit_empty_array_stays_a_judgment(self):
        self.assertEqual(self._reply("[]"), [])

    def test_closed_array_still_scores(self):
        rows = self._reply('[{"index": 1, "reason": "keep"}]')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "keep")
        self.assertEqual(rows[0]["title"], "t")
