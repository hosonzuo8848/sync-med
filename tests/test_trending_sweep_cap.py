# -*- coding: utf-8 -*-
"""A star band over the Search window must be cut, or the round must fail.

GitHub Search returns at most 1000 hits for one query. sweep used to take
min(max_pages, pages) (10 x 100), keep the high-star head, and exit 0.
These tests stay on pure functions plus a fake search. They do not call
gh(), d1(), or main().
"""
import inspect
import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts", "evolve"))

import trending  # noqa: E402


def _stars_band(q_str):
    mark = "stars:"
    i = q_str.find(mark)
    if i < 0:
        return ""
    return q_str[i + len(mark):].split()[0]


def _width(band):
    parsed = trending.parse_star_band(band)
    if not parsed or parsed[0] != "range":
        return None
    return parsed[2] - parsed[1] + 1


def _range_role(band, lo, hi):
    if band == "%d..%d" % (lo, hi):
        return "parent"
    parsed = trending.parse_star_band(band)
    if not parsed or parsed[0] != "range":
        return "other"
    a, b = parsed[1], parsed[2]
    if lo <= a <= b <= hi and not (a == lo and b == hi):
        return "child"
    return "other"


def _owners(lo, hi, bands):
    owners = {n: [] for n in range(lo, hi + 1)}
    for band in bands:
        parsed = trending.parse_star_band(band)
        if not parsed:
            continue
        kind, a, b = parsed
        if kind == "range":
            span = range(a, b + 1)
        elif kind == "eq":
            span = (a,)
        elif kind == "gt":
            span = range(max(lo, a + 1), hi + 1)
        else:
            continue
        for n in span:
            if lo <= n <= hi:
                owners[n].append(band)
    return owners


def _item(name, stars):
    return {
        "full_name": name,
        "html_url": "https://github.com/" + name,
        "description": "d",
        "stargazers_count": stars,
        "forks_count": 0,
        "language": "Python",
        "topics": [],
        "license": None,
        "created_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-11-01T00:00:00Z",
    }


class TestTrendingSweepCap(unittest.TestCase):
    def setUp(self):
        self._gh = trending.gh
        trending.gh = self._live_search

    def tearDown(self):
        trending.gh = self._gh

    def _live_search(self, *args, **kwargs):
        raise AssertionError("live GitHub search")

    def _sweep(self, **kwargs):
        buf = StringIO()
        kwargs.setdefault("sleeper", lambda: None)
        with redirect_stdout(buf):
            return trending.sweep(**kwargs)

    def test_collector_floors_stay_put(self):
        self.assertEqual(
            trending.STAR_BANDS,
            ["100..200", "200..500", "500..1000", "1000..3000", ">3000"],
        )
        self.assertEqual(
            trending.PUSHED_STAR_BANDS,
            ["500..1000", "1000..5000", ">5000"],
        )
        src = inspect.getsource(trending.sweep)
        self.assertIn('stars:>500', src)
        self.assertIn('stars:>100', src)
        self.assertNotIn("min(max_pages", src)

    def test_search_window_is_1000_not_a_wider_page_budget(self):
        self.assertEqual(trending.search_cap(10, 100), 1000)
        self.assertEqual(trending.search_cap(50, 100), 1000)

    def test_2500_is_not_a_ten_page_fetch(self):
        action, payload = trending.band_action(
            2500, "500..1000", 1000, 100, 0, trending.MAX_BAND_SPLITS)
        self.assertNotEqual(action, "fetch")
        if action == "split":
            self.assertEqual(payload, ("500..750", "751..1000"))
            owners = _owners(500, 1000, payload)
            for n, got in owners.items():
                self.assertEqual(len(got), 1, n)
        else:
            self.assertEqual(action, "short")
            self.assertEqual(payload, 1500)
            self.assertNotEqual(trending.exit_code(payload), 0)

    def test_real_pushed_slices_partition_when_they_overflow(self):
        action, payload = trending.band_action(
            2500, "1000..5000", 1000, 100, 0, trending.MAX_BAND_SPLITS)
        self.assertEqual(action, "split")
        self.assertEqual(payload, ("1000..3000", "3001..5000"))
        owners = _owners(1000, 5000, payload)
        for n, got in owners.items():
            self.assertEqual(len(got), 1, n)
        self.assertEqual(trending.split_star_band(">5000"), ("5001..10000", ">10000"))
        self.assertEqual(trending.split_star_band(">10000"), ("10001..20000", ">20000"))
        self.assertEqual(trending.split_star_band(">3000"), ("3001..6000", ">6000"))
        owners = _owners(5001, 30000, ("5001..10000", ">10000"))
        for n, got in owners.items():
            self.assertEqual(len(got), 1, n)

    def test_exact_star_cannot_split_and_is_a_shortfall(self):
        self.assertIsNone(trending.split_star_band("750"))
        self.assertIsNone(trending.split_star_band("750..750"))
        action, missed = trending.band_action(
            2500, "750", 1000, 100, 0, trending.MAX_BAND_SPLITS)
        self.assertEqual((action, missed), ("short", 1500))
        self.assertNotEqual(trending.exit_code(missed), 0)
        self.assertEqual(trending.exit_code(0), 0)
        action, missed = trending.band_action(
            2500, "500..1000", 1000, 100,
            trending.MAX_BAND_SPLITS, trending.MAX_BAND_SPLITS)
        self.assertEqual(action, "short")
        self.assertEqual(missed, 1500)

    def test_under_cap_still_fetches_the_whole_band(self):
        calls = []

        def fake(q_str, page=1, per_page=100):
            calls.append((q_str, page, per_page))
            band = _stars_band(q_str)
            if band != "500..1000":
                return 0, []
            if per_page == 1:
                return 250, []
            if page == 1:
                return 250, [_item("u/%s" % i, 600) for i in range(100)]
            if page == 2:
                return 250, [_item("u/%s" % (100 + i), 550) for i in range(100)]
            if page == 3:
                return 250, [_item("u/%s" % (200 + i), 510) for i in range(50)]
            return 250, []

        rows, gap = self._sweep(
            days=30, tracks=("pushed",), max_pages=10, per_page=100, search=fake)
        self.assertEqual(gap["missed"], 0)
        self.assertEqual(trending.exit_code(gap["missed"]), 0)
        self.assertEqual(len(rows), 250)
        content = [c for c in calls if c[2] == 100]
        self.assertEqual(len(content), 3)
        self.assertFalse(any("stars:500..750" in c[0] for c in calls))

    def test_band_of_2500_splits_or_the_round_fails(self):
        # Parent total is 2500. A child wider than 200 still reports 1200,
        # so one cut that then keeps 1000 of that child is still a miss.
        probes = []
        content = []
        totals = {}

        def fake(q_str, page=1, per_page=100):
            band = _stars_band(q_str)
            role = _range_role(band, 500, 1000)
            if per_page == 1:
                probes.append(band)
                if role == "parent":
                    total = 2500
                elif role == "child" and (_width(band) or 0) > 200:
                    total = 1200
                elif role == "child":
                    total = 100
                else:
                    total = 0
                totals[band] = total
                return total, []
            content.append(band)
            if role == "parent" and page <= 10:
                return 2500, [
                    _item("parent/%s-%s" % (page, i), 900) for i in range(per_page)]
            if role == "child" and page == 1:
                return totals.get(band, 0), [
                    _item("child-%s/%s" % (band, i), 600) for i in range(30)]
            return 0, []

        rows, gap = self._sweep(
            days=30, tracks=("pushed",), max_pages=10, per_page=100, search=fake)
        code = trending.exit_code(gap["missed"])
        parent_pages = [b for b in content if b == "500..1000"]
        children = [b for b in probes if _range_role(b, 500, 1000) == "child"]
        silent = (
            gap["missed"] == 0 and code == 0 and parent_pages and not children)
        self.assertFalse(
            silent,
            "kept the first 1000 of a 2500-hit band and returned success")
        payload = trending.report_payload(rows, len(rows), 0, 0, gap)
        self.assertEqual(payload["missed"], gap["missed"])
        self.assertEqual(payload["swept"], len(rows))
        self.assertIn("saved", payload)
        if gap["missed"] == 0 and code == 0:
            over = [b for b in content if totals.get(b, 0) > 1000]
            self.assertEqual(over, [])
            self.assertNotIn("500..1000", content)
            fetched = []
            seen = set()
            for band in content:
                if band not in seen:
                    seen.add(band)
                    fetched.append(band)
            owners = _owners(500, 1000, fetched)
            for n, got in owners.items():
                self.assertEqual(len(got), 1, "%s %s" % (n, got))
            self.assertTrue(rows)
            self.assertTrue(all(str(name).startswith("child-") for name in rows))
            # Both the first cut and a narrower cut must have been probed.
            self.assertTrue(any((_width(b) or 0) > 200 for b in children))
            self.assertTrue(any(0 < (_width(b) or 0) <= 200 for b in children))
        else:
            self.assertGreaterEqual(gap["missed"], 1500)
            self.assertNotEqual(code, 0)
            self.assertFalse(code == 0 and len(rows) <= 1000)

    def test_open_tail_of_2500_is_cut_or_failed(self):
        probes = []
        content = []

        def fake(q_str, page=1, per_page=100):
            band = _stars_band(q_str)
            if per_page == 1:
                probes.append(band)
                return (2500, []) if band == ">5000" else (80, [])
            content.append(band)
            if band == ">5000" and page <= 10:
                return 2500, [
                    _item("parent/%s-%s" % (page, i), 9000) for i in range(per_page)]
            if page == 1:
                return 80, [_item("child-%s/%s" % (band, i), 8000) for i in range(10)]
            return 0, []

        rows, gap = self._sweep(
            days=30, tracks=("pushed",), max_pages=10, per_page=100, search=fake,
            bands_by_track={"pushed": [">5000"]})
        code = trending.exit_code(gap["missed"])
        if gap["missed"] == 0 and code == 0:
            self.assertNotIn(">5000", content)
            self.assertIn("5001..10000", probes)
            self.assertIn(">10000", probes)
            self.assertTrue(rows)
            self.assertTrue(all(str(name).startswith("child-") for name in rows))
        else:
            self.assertGreaterEqual(gap["missed"], 1500)
            self.assertNotEqual(code, 0)

    def test_unsplittable_2500_keeps_the_head_and_fails_the_round(self):
        def fake(q_str, page=1, per_page=100):
            band = _stars_band(q_str)
            if band != "750":
                return 0, []
            if per_page == 1:
                return 2500, []
            if 1 <= page <= 10:
                return 2500, [
                    _item("same/%s-%s" % (page, i), 750) for i in range(per_page)]
            return 2500, []

        rows, gap = self._sweep(
            days=30, tracks=("pushed",), max_pages=10, per_page=100, search=fake,
            bands_by_track={"pushed": ["750"]})
        self.assertEqual(len(rows), 1000)
        self.assertEqual(gap["missed"], 1500)
        self.assertNotEqual(trending.exit_code(gap["missed"]), 0)
        payload = trending.report_payload(rows, len(rows), 9, 1, gap)
        self.assertEqual(payload["missed"], 1500)
        self.assertEqual(payload["saved"], len(rows))
        self.assertEqual(len(payload["short_bands"]), 1)
        self.assertEqual(payload["short_bands"][0]["missed"], 1500)
        self.assertEqual(payload["short_bands"][0]["kept"], 1000)

    def test_main_writes_the_miss_and_does_not_return_zero_blindly(self):
        src = inspect.getsource(trending.main)
        self.assertIn("rows, gap = sweep(", src)
        self.assertIn("exit_code(", src)
        self.assertIn("report_payload(", src)
        self.assertNotIn("return 0", src)


if __name__ == "__main__":
    unittest.main()
