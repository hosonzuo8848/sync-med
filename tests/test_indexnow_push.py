# -*- coding: utf-8 -*-
"""IndexNow must run on schedule and must not hide a rejected push.

A scheduled run has no workflow input, so INDEXNOW_BATCH arrived as an
empty string and int('') crashed before any URL was sent. A push that
came back 4xx/5xx, or a sub-sitemap that could not be read, still exited
0. A clean run stays 0. This test does not call the network.
"""
import importlib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "seo"))


def _load(batch):
    old = os.environ.get("INDEXNOW_BATCH")
    os.environ["INDEXNOW_BATCH"] = batch
    try:
        sys.modules.pop("indexnow_push", None)
        return importlib.import_module("indexnow_push")
    finally:
        if old is None:
            os.environ.pop("INDEXNOW_BATCH", None)
        else:
            os.environ["INDEXNOW_BATCH"] = old


class IndexNowPush(unittest.TestCase):
    def test_empty_batch_from_a_scheduled_run_uses_the_default(self):
        mod = _load("")
        self.assertEqual(mod.BATCH, 5000)

    def test_an_explicit_batch_stays(self):
        mod = _load("200")
        self.assertEqual(mod.BATCH, 200)

    def test_a_rejected_push_is_not_success(self):
        mod = _load("")
        self.assertEqual(mod.finish_code(5000, 0, False), 1)
        self.assertEqual(mod.finish_code(5000, 4000, False), 1)

    def test_an_unread_sub_sitemap_is_not_success(self):
        mod = _load("")
        self.assertEqual(mod.finish_code(100, 100, True), 1)

    def test_a_clean_push_stays_zero(self):
        mod = _load("")
        self.assertEqual(mod.finish_code(100, 100, False), 0)
