# -*- coding: utf-8 -*-
"""fleet_health must send the gateway key to /api/gateway/usage and must
not print a failed read as "nothing cooling".

The usage endpoint is moving behind X-Gateway-Key. probe_usage used to
send no key and turned any error into ([], []), which the report printed
as "none (all active)". This test does not call the network.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "intel_radar"))

import fleet_health as fh  # noqa: E402


class UsageRead(unittest.TestCase):
    def setUp(self):
        self._fetch = fh.fetch
        self._key = os.environ.get("GW_KEY")
        os.environ["GW_KEY"] = "k-test"

    def tearDown(self):
        fh.fetch = self._fetch
        if self._key is None:
            os.environ.pop("GW_KEY", None)
        else:
            os.environ["GW_KEY"] = self._key

    def test_sends_the_gateway_key(self):
        seen = {}

        def fake(url, body=None, timeout=120, headers=None):
            seen["headers"] = headers or {}
            return {"providers": [], "cooling": []}

        fh.fetch = fake
        fh.probe_usage()
        self.assertEqual(seen["headers"].get("X-Gateway-Key"), "k-test")

    def test_failed_read_is_not_nothing_cooling(self):
        def boom(url, body=None, timeout=120, headers=None):
            raise RuntimeError("HTTP Error 401")

        fh.fetch = boom
        _u, cooling, failed = fh.probe_usage()
        self.assertTrue(failed)
        self.assertIn("unread", fh.cooling_line(cooling, failed))

    def test_a_real_empty_cooling_stays(self):
        fh.fetch = lambda url, body=None, timeout=120, headers=None: {"providers": [], "cooling": []}
        _u, cooling, failed = fh.probe_usage()
        self.assertFalse(failed)
        self.assertIn("none (all active)", fh.cooling_line(cooling, failed))
