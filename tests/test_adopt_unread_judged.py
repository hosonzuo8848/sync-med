# -*- coding: utf-8 -*-
"""A failed already-judged read is not a measured empty set.

already_judged() used to return set() when the query raised. main() then
printed "already judged 0" and re-ran every candidate. A real empty result
stays empty and can still judge. This test does not call D1 or a model.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import adopt  # noqa: E402


class FailedAlreadyJudged(unittest.TestCase):
    def setUp(self):
        self._d1 = adopt.d1

    def tearDown(self):
        adopt.d1 = self._d1

    def test_failed_read_is_not_an_empty_judged_set(self):
        def boom(sql):
            raise RuntimeError("down")

        adopt.d1 = boom
        done, failed = adopt.already_judged()
        self.assertTrue(failed)
        self.assertEqual(done, set())

    def test_a_real_empty_judged_set_stays(self):
        adopt.d1 = lambda sql: []
        done, failed = adopt.already_judged()
        self.assertFalse(failed)
        self.assertEqual(done, set())

    def test_a_real_title_stays(self):
        adopt.d1 = lambda sql: [{"title": "a/b"}]
        done, failed = adopt.already_judged()
        self.assertFalse(failed)
        self.assertEqual(done, {"a/b"})

    def test_should_not_judge_when_unread(self):
        self.assertFalse(adopt.should_judge(True))
        self.assertTrue(adopt.should_judge(False))
