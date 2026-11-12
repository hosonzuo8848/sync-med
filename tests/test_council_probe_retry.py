# -*- coding: utf-8 -*-
"""A dead probe must not spend the retry budget on a permanent failure."""
import io
import os
import sys
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "council"))

import llm_roster as roster  # noqa: E402


class ProbeRetry(unittest.TestCase):
    def setUp(self):
        self.old_cap = roster.MAX_CALLS
        self.old_state = dict(roster._state)
        self.old_log = list(roster.CALL_LOG)
        self.old_chat = roster._openai_chat
        self.old_sleep = roster.time.sleep
        roster.MAX_CALLS = 20
        roster._state["calls"] = 0
        roster._state["fails"] = 0
        roster._state["neurons"] = 0.0
        del roster.CALL_LOG[:]
        self.sleeps = []
        roster.time.sleep = self.sleeps.append

    def tearDown(self):
        roster.MAX_CALLS = self.old_cap
        roster._state.clear()
        roster._state.update(self.old_state)
        del roster.CALL_LOG[:]
        roster.CALL_LOG.extend(self.old_log)
        roster._openai_chat = self.old_chat
        roster.time.sleep = self.old_sleep

    def _seat(self):
        return {
            "id": "dead", "keys": ["k"], "kind": "openai",
            "base": "https://example.invalid", "model": "m", "extra": None,
        }

    def _http(self, code):
        def chat(*_a, **_k):
            raise urllib.error.HTTPError(
                "https://example.invalid", code, "x", None, io.BytesIO(b"no"))
        roster._openai_chat = chat

    def test_permanent_401_spends_one_probe_call(self):
        self._http(401)
        self.assertFalse(roster.probe(self._seat(), retries=2))
        self.assertEqual(roster._state["calls"], 1)
        self.assertEqual(self.sleeps, [])

    def test_429_still_uses_the_probe_retries(self):
        self._http(429)
        self.assertFalse(roster.probe(self._seat(), retries=2))
        self.assertEqual(roster._state["calls"], 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_empty_body_is_not_retried(self):
        def chat(*_a, **_k):
            return ("", {"in": 0, "out": 0, "neurons": 0.0})
        roster._openai_chat = chat
        self.assertFalse(roster.probe(self._seat(), retries=2))
        self.assertEqual(roster._state["calls"], 1)
        self.assertEqual(self.sleeps, [])

    def test_race_call_does_not_retry_401(self):
        self._http(401)
        with self.assertRaises(roster.SeatFailed) as caught:
            roster.call(self._seat(), "s", "u", retries=1, stage="race")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(roster._state["calls"], 1)

    def test_race_call_retries_500(self):
        self._http(500)
        with self.assertRaises(roster.SeatFailed) as caught:
            roster.call(self._seat(), "s", "u", retries=1, stage="race")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(roster._state["calls"], 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_transport_error_is_retryable(self):
        self.assertTrue(roster._retryable(urllib.error.URLError("timed out")))
        self.assertFalse(roster._retryable(RuntimeError("empty response")))
