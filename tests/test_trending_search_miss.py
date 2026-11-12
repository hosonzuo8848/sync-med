# -*- coding: utf-8 -*-
"""A failed trending search is not an empty band.

gh() used to return (0, []) when the request raised, and sweep treated
that as a real zero-hit slice. A closed response with total_count 0 stays
empty. This test does not call GitHub.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import trending  # noqa: E402


class FailedTrendingSearch(unittest.TestCase):
    def test_failed_query_is_not_an_empty_band(self):
        def boom(*_a, **_k):
            raise RuntimeError("down")

        old = trending.urllib.request.urlopen
        trending.urllib.request.urlopen = boom
        try:
            total, items, failed = trending.gh("stars:>100")
        finally:
            trending.urllib.request.urlopen = old
        self.assertTrue(failed)
        self.assertEqual(items, [])
        self.assertEqual(total, 0)

    def test_a_real_zero_count_stays_empty(self):
        class Resp(object):
            def read(self):
                return b'{"total_count": 0, "items": []}'

        old = trending.urllib.request.urlopen
        trending.urllib.request.urlopen = lambda *_a, **_k: Resp()
        try:
            total, items, failed = trending.gh("stars:>100")
        finally:
            trending.urllib.request.urlopen = old
        self.assertFalse(failed)
        self.assertEqual(total, 0)
        self.assertEqual(items, [])

    def test_sweep_does_not_treat_a_failed_probe_as_empty(self):
        def search(_q, page=1, per_page=100):
            return 0, [], True

        _rows, gap = trending.sweep(
            days=30, tracks=("created",), max_pages=10, per_page=100,
            search=search, sleeper=lambda: None,
            bands_by_track={"created": ["100..200"]})
        self.assertTrue(gap.get("search_failed"))
        self.assertEqual(trending.exit_code(gap.get("missed"), search_failed=True), 1)

    def test_a_real_empty_probe_stays_a_finished_band(self):
        def search(_q, page=1, per_page=100):
            return 0, []

        _rows, gap = trending.sweep(
            days=30, tracks=("created",), max_pages=10, per_page=100,
            search=search, sleeper=lambda: None,
            bands_by_track={"created": ["100..200"]})
        self.assertFalse(gap.get("search_failed"))
        self.assertEqual(gap.get("missed"), 0)
        self.assertEqual(trending.exit_code(0), 0)

    def test_a_failed_page_is_not_the_end_of_the_band(self):
        def search(_q, page=1, per_page=100):
            if per_page == 1:
                return 50, []
            return 0, [], True

        _rows, gap = trending.sweep(
            days=30, tracks=("created",), max_pages=10, per_page=100,
            search=search, sleeper=lambda: None,
            bands_by_track={"created": ["100..200"]})
        self.assertTrue(gap.get("search_failed"))
        self.assertEqual(len(_rows), 0)
