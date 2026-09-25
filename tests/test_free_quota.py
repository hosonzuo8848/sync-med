# -*- coding: utf-8 -*-
"""free_quota_usage() / fmt_free_quota() regression (2026-09-26).

Not network: _cf() is stubbed to answer the two calls free_quota_usage() makes
(the D1 database list, then one GROUP BY provider query). Covers:
  - exactly one SQL query is issued, and it carries yesterday's UTC date range
  - percentage math is correct
  - refresh=daily under 50% is red-flagged; refresh=daily over 50% is not
  - refresh=once / refresh=none are never red-flagged, even under 50%, even with
    a known limit (whitebox on _quota_row so the gate is tested independent of
    "limit happens to be unknown")
  - a braked lane (groq) shows a real 0, not "unwired", and is never red-flagged
  - an unwired lane (llm7, before its provider name exists in the logs) renders
    as "unwired" text, not a 0%/None row
  - a per_key source (llm7) scales its displayed limit to however many distinct
    provider names actually showed up in yesterday's data

Usage: python -m pytest tests/test_free_quota.py -q
   or: python -m unittest tests.test_free_quota -v
"""
import os
import sys
import unittest
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

# fleet_watch.py only reads these at call time (not at import time), but set
# them up front so every test in this file can rely on the "env present" path.
os.environ["CF_ACCOUNT_ID"] = "acc"
os.environ["D1_API_TOKEN"] = "tok"
os.environ.setdefault("GITHUB_TOKEN", "t")   # module-level TOKEN read; unused by these tests

import fleet_watch as FW  # noqa: E402

_REAL_CF = FW._cf


def tearDownModule():
    FW._cf = _REAL_CF


NOW = datetime(2026, 9, 26, 3, 0, 0, tzinfo=timezone.utc)   # "yesterday" (UTC) is 2026-09-25


def make_cf(rows_by_provider):
    """Fake _cf(path, body): answers the db-list call and the one query call,
    recording every call so a test can inspect what was actually sent."""
    calls = []

    def fake_cf(path, body=None):
        calls.append((path, body))
        if path == "/d1/database?per_page=100":
            return {"success": True, "result": [{"name": "guyaofang-db", "uuid": "u1"}]}
        if path.endswith("/query"):
            results = [{"provider": p, "calls": c, "ok_calls": ok, "tokens": t}
                       for p, (c, ok, t) in rows_by_provider.items()]
            return {"result": [{"results": results}]}
        raise AssertionError("unexpected _cf path: %s" % path)

    return fake_cf, calls


class TestSingleQuery(unittest.TestCase):
    def test_exactly_one_sql_query_with_yesterdays_range(self):
        fake, calls = make_cf({})
        FW._cf = fake
        r = FW.free_quota_usage(NOW)
        self.assertTrue(r["ok"], r.get("skip_reason"))
        self.assertEqual(r["day"], "2026-09-25")
        query_calls = [c for c in calls if c[0].endswith("/query")]
        self.assertEqual(len(query_calls), 1, "must issue exactly one SQL query")
        sql = query_calls[0][1]["sql"]
        self.assertIn("2026-09-25 00:00:00", sql)
        self.assertIn("2026-09-26 00:00:00", sql)
        self.assertIn("GROUP BY provider", sql)
        self.assertEqual(len(calls), 2, "db-list + one query, nothing else")


class TestPercentAndRedFlag(unittest.TestCase):
    def test_percentage_math_and_red_under_50(self):
        # groq2: 300 / 1000 = 30% -> daily, not braked -> red
        fake, _ = make_cf({"groq2": (300, 300, 0)})
        FW._cf = fake
        r = FW.free_quota_usage(NOW)
        row = next(x for x in r["rows"] if x["key"] == "groq2")
        self.assertAlmostEqual(row["pct"], 30.0)
        self.assertTrue(row["alert"])

    def test_daily_over_50_not_red(self):
        fake, _ = make_cf({"groq3": (600, 600, 0)})
        FW._cf = fake
        r = FW.free_quota_usage(NOW)
        row = next(x for x in r["rows"] if x["key"] == "groq3")
        self.assertAlmostEqual(row["pct"], 60.0)
        self.assertFalse(row["alert"])

    def test_braked_groq_shows_real_zero_never_red(self):
        fake, _ = make_cf({})   # no rows at all for "groq" -- braked, not unwired
        FW._cf = fake
        r = FW.free_quota_usage(NOW)
        row = next(x for x in r["rows"] if x["key"] == "groq")
        self.assertFalse(row["unwired"])
        self.assertEqual(row["used"], 0)
        self.assertAlmostEqual(row["pct"], 0.0)
        self.assertFalse(row["alert"], "a braked lane must never be red-flagged")

    def test_refresh_gate_independent_of_limit(self):
        """Whitebox on _quota_row: refresh=once must suppress red even with a
        known limit and a low percentage, proving the gate isn't only working
        by accident because once/none sources happen to have limit=None today."""
        src = {"key": "x", "label": "X", "match": ("exact", "x"), "unit": "calls",
               "limit": 1000, "refresh": "once", "note": "n"}
        row = FW._quota_row(src, {"x": {"calls": 100, "ok_calls": 100, "tokens": 0}})
        self.assertAlmostEqual(row["pct"], 10.0)
        self.assertFalse(row["alert"], "refresh=once must never be red-flagged")

    def test_once_and_none_sources_never_red(self):
        fake, _ = make_cf({"tc_dsflash": (10, 10, 0), "sn_flash": (5, 5, 0)})
        FW._cf = fake
        r = FW.free_quota_usage(NOW)
        tc_row = next(x for x in r["rows"] if x["key"] == "tc_")
        sn_row = next(x for x in r["rows"] if x["key"] == "sn_")
        self.assertFalse(tc_row["alert"])
        self.assertFalse(sn_row["alert"])


class TestUnwiredAndPerKey(unittest.TestCase):
    def test_llm7_shows_unwired_when_absent(self):
        fake, _ = make_cf({"zhipu": (5, 5, 100)})
        FW._cf = fake
        r = FW.free_quota_usage(NOW)
        row = next(x for x in r["rows"] if x["key"] == "llm7")
        self.assertTrue(row["unwired"])
        self.assertIsNone(row["used"])
        self.assertFalse(row["alert"])
        lines = FW.fmt_free_quota(r)
        self.assertTrue(any(row["label"] in ln and "未接入" in ln for ln in lines),
                         "rendered table must show the unwired label next to LLM7's row")

    def test_llm7_wired_scales_limit_to_keys_seen(self):
        # 2 of the 3 llm7 lanes reported data today -> limit scales to 2 * 1,000,000, not 3 * 1,000,000
        fake, _ = make_cf({"llm7a": (10, 10, 900000), "llm7b": (5, 5, 200000)})
        FW._cf = fake
        r = FW.free_quota_usage(NOW)
        row = next(x for x in r["rows"] if x["key"] == "llm7")
        self.assertFalse(row["unwired"])
        self.assertEqual(row["used"], 1_100_000)
        self.assertEqual(row["limit"], 2_000_000)
        self.assertAlmostEqual(row["pct"], 55.0)
        self.assertFalse(row["alert"])   # 55% >= 50%


class TestFailureIsolation(unittest.TestCase):
    def test_missing_env_gives_skip_reason_not_a_crash(self):
        old = os.environ.pop("D1_API_TOKEN")
        try:
            r = FW.free_quota_usage(NOW)
            self.assertFalse(r["ok"])
            self.assertTrue(r["skip_reason"])
            lines = FW.fmt_free_quota(r)
            self.assertTrue(any("跳过" in ln for ln in lines))
        finally:
            os.environ["D1_API_TOKEN"] = old

    def test_cf_exception_gives_skip_reason_not_a_crash(self):
        def boom(path, body=None):
            raise RuntimeError("network is down")
        FW._cf = boom
        r = FW.free_quota_usage(NOW)
        self.assertFalse(r["ok"])
        self.assertIn("network is down", r["skip_reason"])


if __name__ == "__main__":
    unittest.main()
