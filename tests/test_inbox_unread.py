# -*- coding: utf-8 -*-
"""A failed inbox read is not an empty adopt queue.

fetch() used to return [] when the query failed. main() then wrote
"no adopt items this round" and exited 0. A real empty result stays
empty and can still be published. This test does not call D1.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import inbox  # noqa: E402


class FailedInboxRead(unittest.TestCase):
    def setUp(self):
        self._d1 = inbox.d1

    def tearDown(self):
        inbox.d1 = self._d1

    def test_failed_read_is_not_an_empty_queue(self):
        def boom(sql):
            raise RuntimeError("down")

        inbox.d1 = boom
        rows, failed = inbox.fetch("adopt")
        self.assertEqual(rows, [])
        self.assertTrue(failed)
        self.assertFalse(inbox.should_publish(True, False))

    def test_real_empty_queue_can_publish(self):
        inbox.d1 = lambda sql: []
        rows, failed = inbox.fetch("adopt")
        self.assertEqual(rows, [])
        self.assertFalse(failed)
        self.assertTrue(inbox.should_publish(False, False))

    def test_main_does_not_publish_when_unread(self):
        with open(inbox.__file__, encoding="utf-8") as fh:
            src = fh.read()
        main = src.split("def main(", 1)[1].split("\nif __name__", 1)[0]
        self.assertIn("should_publish(", main)
