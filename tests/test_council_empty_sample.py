# -*- coding: utf-8 -*-
"""An empty rate denominator is not a measured 0%.

total=0 used to emit value 0.0 against target 90. severity then scored
(90-0)/90 * the xunmai weight 1.15 = 1.15, and is_gap fires above 0.15,
so a week with no calls was triaged as a recall failure.

Daodu coverage did the same against target 8: zero books published 0.0,
and (8-0)/8 * the daodu weight 1.0 = 1.0, which also crosses 0.15.
That is an empty shelf, not a measured coverage. 0 guides out of 100
books is a real 0% and must stay a gap.

A real sample of 0/4 is still a measured 0% and must stay a gap.
xunmai_vol of 0 is a real count and must stay on the board.
"""
import inspect
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "council"))

import probes  # noqa: E402


_XUNMAI_SQL = (
    "SELECT COUNT(*) total, "
    "SUM(CASE WHEN public_evidence_count>0 THEN 1 ELSE 0 END) with_ev "
    "FROM sue_call_logs WHERE created_at >= strftime('%s','now','-7 days')"
)
_DAODU_HAVE_SQL = (
    "SELECT COUNT(DISTINCT book_id) c FROM book_daodu_ai WHERE status='visible'"
)
_DAODU_BOOK_SQL = "SELECT COUNT(*) c FROM books_assets_v2 WHERE frontend_visible=1"
_D1_ENV = ("CF_ACCOUNT_ID", "D1_DATABASE_ID", "D1_API_TOKEN")


class EmptyRecallSample(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in _D1_ENV}
        self._d1 = probes.d1_query

    def tearDown(self):
        probes.d1_query = self._d1
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _collect(self, recall_row, boom=False, books=0, daodu=0):
        calls = []

        def fake(sql):
            # The real d1_query talks to Cloudflare. This stand-in is the
            # only query function the test may run.
            calls.append(sql)
            if boom and "public_evidence_count" in sql:
                raise RuntimeError("d1 down")
            if sql == _XUNMAI_SQL:
                return [recall_row]
            if sql == _DAODU_HAVE_SQL:
                return [{"c": daodu}]
            if sql == _DAODU_BOOK_SQL:
                return [{"c": books}]
            if "GROUP BY" in sql:
                return [{"k": "hit_paywall", "c": 1}]
            return [{"c": 0}]

        probes.d1_query = fake
        metrics, skipped = probes.collect_metrics()
        self.assertIs(probes.d1_query, fake)
        self.assertIn(_XUNMAI_SQL, calls)
        self.assertIn(_DAODU_HAVE_SQL, calls)
        self.assertIn(_DAODU_BOOK_SQL, calls)
        return metrics, skipped

    def _recalls(self, metrics):
        return [m for m in metrics if m["key"] == "xunmai_recall"]

    def test_total_zero_emits_no_zero_value_recall(self):
        metrics, skipped = self._collect({"total": 0, "with_ev": 0})
        self.assertFalse(any(
            m["key"] == "xunmai_recall" and m["value"] == 0 for m in metrics))
        self.assertEqual(self._recalls(metrics), [])
        self.assertIn("xunmai: no sample", skipped)
        vols = [m for m in metrics if m["key"] == "xunmai_vol"]
        self.assertEqual(len(vols), 1)
        self.assertEqual(vols[0]["value"], 0.0)
        self.assertEqual(vols[0]["target"], 100.0)

    def test_total_zero_does_not_cross_the_gap_line(self):
        metrics, _skipped = self._collect({"total": 0, "with_ev": 0})
        for m in self._recalls(metrics):
            self.assertLessEqual(probes.severity(m), 0.15)
            self.assertFalse(probes.is_gap(m))
        self.assertEqual(self._recalls(metrics), [])

    def test_null_evidence_sum_on_empty_window_is_still_no_sample(self):
        # SQLite SUM over zero rows is NULL. The probe coerces that to 0
        # before the rate, and must still refuse to publish a 0% gap.
        metrics, skipped = self._collect({"total": 0, "with_ev": None})
        for m in self._recalls(metrics):
            self.assertNotEqual(m["value"], 0)
            self.assertLessEqual(probes.severity(m), 0.15)
        self.assertEqual(self._recalls(metrics), [])
        self.assertIn("xunmai: no sample", skipped)

    def test_two_of_four_is_fifty_percent(self):
        metrics, skipped = self._collect({"total": 4, "with_ev": 2})
        recalls = self._recalls(metrics)
        self.assertEqual(len(recalls), 1)
        m = recalls[0]
        self.assertEqual(m["value"], 50.0)
        self.assertIn("2/4", m["display"])
        self.assertEqual(m["display"], "2/4(50.0%)")
        self.assertEqual(m["target"], 90.0)
        self.assertEqual(m["sql"], _XUNMAI_SQL)
        self.assertNotIn("xunmai: no sample", skipped)

    def test_zero_hits_inside_a_real_sample_stays_a_gap(self):
        metrics, skipped = self._collect({"total": 4, "with_ev": 0})
        recalls = self._recalls(metrics)
        self.assertEqual(len(recalls), 1)
        m = recalls[0]
        self.assertEqual(m["value"], 0.0)
        self.assertIn("0/4", m["display"])
        self.assertGreater(probes.severity(m), 0.15)
        self.assertTrue(probes.is_gap(m))
        self.assertNotIn("xunmai: no sample", skipped)

    def test_query_error_stays_on_the_exception_skip_path(self):
        metrics, skipped = self._collect({"total": 4, "with_ev": 2}, boom=True)
        self.assertEqual(self._recalls(metrics), [])
        xunmai_skips = [s for s in skipped if s.startswith("xunmai:")]
        self.assertEqual(xunmai_skips, ["xunmai: d1 down"])

    def test_recall_from_counts_is_pure(self):
        self.assertIsNone(probes.recall_from_counts(0, 0))
        self.assertIsNone(probes.recall_from_counts(0, 5))
        m = probes.recall_from_counts(4, 2)
        self.assertEqual(m["value"], 50.0)
        self.assertIn("2/4", m["display"])
        self.assertEqual(m["target"], 90.0)
        self.assertEqual(m["key"], "xunmai_recall")
        self.assertEqual(probes.ORGAN_WEIGHT["xunmai"], 1.15)

    def _daodu(self, metrics):
        return [m for m in metrics if m["key"] == "daodu_cov"]

    def test_zero_books_emits_no_daodu_coverage(self):
        # Old daodu_cov wrote 0.0 whenever the book count was 0, so this
        # assertion is red on that implementation.
        metrics, skipped = self._collect({"total": 4, "with_ev": 2}, books=0, daodu=0)
        self.assertEqual(self._daodu(metrics), [])
        self.assertIn("daodu_cov: no sample", skipped)
        self.assertEqual(len(self._recalls(metrics)), 1)

    def test_zero_guides_on_a_real_shelf_stays_a_gap(self):
        metrics, skipped = self._collect(
            {"total": 4, "with_ev": 2}, books=100, daodu=0)
        rows = self._daodu(metrics)
        self.assertEqual(len(rows), 1)
        m = rows[0]
        self.assertEqual(m["value"], 0.0)
        self.assertEqual(m["target"], 8.0)
        self.assertEqual(m["display"], "0/100(0.00%)")
        self.assertGreater(probes.severity(m), 0.15)
        self.assertTrue(probes.is_gap(m))
        self.assertNotIn("daodu_cov: no sample", skipped)

    def test_zero_denominator_is_judged_once(self):
        # Both probes must call the one predicate. A second `total == 0`
        # (or daodu's old `if total else 0.0`) keeps an empty shelf scored
        # as a measured 0%.
        self.assertTrue(probes.denominator_empty(0))
        self.assertFalse(probes.denominator_empty(100))
        src = inspect.getsource(probes)
        self.assertEqual(src.count("total == 0"), 1)
        self.assertEqual(src.count("denominator_empty("), 3)
        self.assertNotIn("if total", inspect.getsource(probes.collect_metrics))
        self.assertIn(
            "denominator_empty(total)", inspect.getsource(probes.recall_from_counts))
        self.assertIn(
            "denominator_empty(total)", inspect.getsource(probes.collect_metrics))


if __name__ == "__main__":
    unittest.main()
