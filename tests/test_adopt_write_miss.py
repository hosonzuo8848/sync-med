# -*- coding: utf-8 -*-
"""A verdict that did not land is not an adoption.

The adopt loop appended the proposal before the insert. A failed insert
still left the repo in the adopt list and the process exited 0, so the
issue step could publish a judgment that is not in the database. A real
stored adopt still lands. dry_run still records without a write. This
test does not call D1 or a model.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import adopt  # noqa: E402

LINE = {"repo": "a/b", "stars": 3}


def _out():
    return {"adopt": [], "watch": [], "skip": 0, "fail": 0}


class FailedVerdictWrite(unittest.TestCase):
    def test_failed_write_is_not_an_adoption(self):
        out = _out()
        landed = adopt.record_verdict(out, "adopt", LINE, stored=False)
        self.assertFalse(landed)
        self.assertEqual(out["adopt"], [])
        self.assertEqual(out["fail"], 1)

    def test_a_real_stored_adopt_stays(self):
        out = _out()
        landed = adopt.record_verdict(out, "adopt", LINE, stored=True)
        self.assertTrue(landed)
        self.assertEqual(out["adopt"], [LINE])
        self.assertEqual(out["fail"], 0)

    def test_dry_run_keeps_the_proposal(self):
        out = _out()
        landed = adopt.record_verdict(out, "watch", LINE, stored=False, dry_run=True)
        self.assertTrue(landed)
        self.assertEqual(out["watch"], [LINE])
        self.assertEqual(out["fail"], 0)

    def test_a_real_stored_skip_stays_a_skip(self):
        out = _out()
        landed = adopt.record_verdict(out, "skip", LINE, stored=True)
        self.assertTrue(landed)
        self.assertEqual(out["skip"], 1)
        self.assertEqual(out["adopt"], [])

    def test_finish_code_only_when_a_write_failed(self):
        self.assertEqual(adopt.finish_code(True), 1)
        self.assertEqual(adopt.finish_code(False), 0)
