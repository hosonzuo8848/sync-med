# -*- coding: utf-8 -*-
"""A cut-off competitor score reply is not a finished batch of zero hits.

analyze_batch used to return [] when the reply never closed its JSON
array. analyze_all then printed a hit count of 0 and kept going, so the
weekly issue went out as if that batch had been judged.

A model that actually returns [] has made a judgment. That stays empty.
This test does not call the network, D1, or a model.
"""
import os
import sys
import unittest
from io import StringIO
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "intel_radar"))

import competitor_reg_radar as radar  # noqa: E402


ITEM = {"title": "t", "abstract": "a", "source": "s"}


class TruncatedCompetitorBatch(unittest.TestCase):
    def setUp(self):
        self._llm = radar._call_llm_sync

    def tearDown(self):
        radar._call_llm_sync = self._llm

    def _run(self, reply):
        def fake(model, messages, max_tokens=1500, use_gateway=True):
            return reply

        radar._call_llm_sync = fake
        buf = StringIO()
        with redirect_stdout(buf):
            picks = radar.analyze_all(
                [ITEM], ["m"], False, batch_size=20, sleep_s=0
            )
        return picks, buf.getvalue()

    def test_truncated_payload_is_not_a_zero_hit_line(self):
        def fake(model, messages, max_tokens=1500, use_gateway=True):
            return '[{"index": 1, "score": 5, "reason":'

        radar._call_llm_sync = fake
        buf = StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(radar.BatchUnscored) as ctx:
                radar.analyze_all([ITEM], ["m"], False, batch_size=20, sleep_s=0)
        self.assertIn("truncated", str(ctx.exception).lower())
        self.assertNotIn("] ->", buf.getvalue())

    def test_explicit_empty_array_stays_a_judgment(self):
        picks, out = self._run("[]")
        self.assertEqual(picks, [])
        self.assertIn("] ->", out)

    def test_closed_array_still_scores(self):
        picks, out = self._run(
            '[{"index": 1, "score": 5, "category": "reg", "reason": "keep"}]'
        )
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["score"], 5)
        self.assertIn("] ->", out)

    def test_transport_failure_is_not_a_zero_hit_line(self):
        def fake(model, messages, max_tokens=1500, use_gateway=True):
            raise RuntimeError("down")

        radar._call_llm_sync = fake
        buf = StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(radar.BatchUnscored) as ctx:
                radar.analyze_all([ITEM], ["m"], False, batch_size=20, sleep_s=0)
        self.assertIn("llm call failed", str(ctx.exception).lower())
        self.assertNotIn("] ->", buf.getvalue())

    def test_object_payload_is_not_a_zero_hit_line(self):
        def fake(model, messages, max_tokens=1500, use_gateway=True):
            return '{"index": 1, "score": 5, "category": "reg", "reason": "keep"}'

        radar._call_llm_sync = fake
        buf = StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(radar.BatchUnscored) as ctx:
                radar.analyze_all([ITEM], ["m"], False, batch_size=20, sleep_s=0)
        self.assertIn("truncated", str(ctx.exception).lower())
        self.assertNotIn("] ->", buf.getvalue())
