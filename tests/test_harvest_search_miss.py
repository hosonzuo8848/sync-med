# -*- coding: utf-8 -*-
"""A failed GitHub search is not an empty result page.

gh_search used to return [] on any error. main() then saved the partial
list and exited 0, so the round looked like those queries had been
answered and found nothing.

A real empty page stays empty and the round can still exit 0.
This test does not call GitHub, D1, or a model.
"""
import os
import sys
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import harvest  # noqa: E402


class FailedSearchIsNotEmpty(unittest.TestCase):
    def setUp(self):
        self._open = harvest.urllib.request.urlopen

    def tearDown(self):
        harvest.urllib.request.urlopen = self._open

    def test_failed_search_is_not_an_empty_page(self):
        def boom(*_a, **_k):
            raise urllib.error.URLError("down")

        harvest.urllib.request.urlopen = boom
        items, failed = harvest.gh_search("q")
        self.assertEqual(items, [])
        self.assertTrue(failed)

    def test_empty_page_is_a_real_answer(self):
        class Resp:
            def read(self):
                return b'{"items":[]}'

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        harvest.urllib.request.urlopen = lambda *_a, **_k: Resp()
        items, failed = harvest.gh_search("q")
        self.assertEqual(items, [])
        self.assertFalse(failed)

    def test_a_miss_fails_the_round(self):
        self.assertEqual(harvest.finish_code(1), 1)
        self.assertEqual(harvest.finish_code(0), 0)
        with open(harvest.__file__, encoding="utf-8") as fh:
            src = fh.read()
        main = src.split("def main(", 1)[1].split("\nif __name__", 1)[0]
        self.assertIn("finish_code(", main)
