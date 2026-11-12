# -*- coding: utf-8 -*-
"""A cut-off daily score reply is not a finished batch of zero hits.

analyze_batch_async used to return [] when the reply never closed its
JSON array. analyze_all_parallel then printed [OK] and a hit count of 0,
and the daily report went out as if that batch had been judged.

A model that actually returns [] has made a judgment. That stays empty
and still prints [OK]. This test does not call the network, D1, or a model.
"""
import asyncio
import os
import sys
import unittest
from io import StringIO
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "intel_radar"))

import daily_report_v3 as daily  # noqa: E402


ITEM = {"title": "t", "abstract": "a", "source": "s"}


class TruncatedDailyBatch(unittest.TestCase):
    def setUp(self):
        self._llm = daily._call_llm_sync
        self._sleep = daily.BATCH_SLEEP
        daily.BATCH_SLEEP = 0

    def tearDown(self):
        daily._call_llm_sync = self._llm
        daily.BATCH_SLEEP = self._sleep

    def _run(self, reply):
        def fake(model, messages, max_tokens=1500, use_gateway=True):
            return reply

        daily._call_llm_sync = fake
        buf = StringIO()
        with redirect_stdout(buf):
            picks = asyncio.run(
                daily.analyze_all_parallel([ITEM], False, ["m"])
            )
        return picks, buf.getvalue()

    def test_truncated_payload_is_not_an_ok_zero(self):
        buf = StringIO()

        def fake(model, messages, max_tokens=1500, use_gateway=True):
            return '[{"index": 1, "score": 5, "category": "rag", "reason":'

        daily._call_llm_sync = fake
        with redirect_stdout(buf):
            with self.assertRaises(daily.BatchUnscored) as ctx:
                asyncio.run(daily.analyze_all_parallel([ITEM], False, ["m"]))
        self.assertIn("truncated", str(ctx.exception).lower())
        self.assertNotIn("[OK]", buf.getvalue())

    def test_explicit_empty_array_stays_a_judgment(self):
        picks, out = self._run("[]")
        self.assertEqual(picks, [])
        self.assertIn("[OK]", out)

    def test_closed_array_still_scores(self):
        picks, out = self._run(
            '[{"index": 1, "score": 5, "category": "rag", "reason": "keep"}]'
        )
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["score"], 5)
        self.assertEqual(picks[0]["_model"], "m")
        self.assertIn("[OK]", out)
