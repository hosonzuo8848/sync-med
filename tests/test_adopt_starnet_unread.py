# -*- coding: utf-8 -*-
"""A corrupt starnet file is not an empty reader list.

cognition_paths() used to swallow every read error and return no readers,
so the round stayed single-brain and still exited 0. A missing file stays
a real empty list. A file with no qualified row stays empty too. This
test does not call D1 or a model.
"""
import json
import os
import tempfile
import unittest
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import adopt  # noqa: E402


class FailedStarnetRead(unittest.TestCase):
    def setUp(self):
        self._brain = dict(adopt._BRAIN)
        self._paths = adopt._PATHS["v"]
        adopt._BRAIN["body"] = "champion-body"
        adopt._BRAIN["vid"] = None
        adopt._PATHS["v"] = None

    def tearDown(self):
        adopt._BRAIN.clear()
        adopt._BRAIN.update(self._brain)
        adopt._PATHS["v"] = self._paths

    def test_corrupt_file_is_not_no_readers(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            os.write(fd, b"{")
            os.close(fd)
            rows, failed = adopt.read_cognition(path)
        finally:
            os.remove(path)
        self.assertTrue(failed)
        self.assertEqual(rows, [])

    def test_missing_file_stays_empty(self):
        rows, failed = adopt.read_cognition(os.path.join(HERE, "no-such-race.json"))
        self.assertFalse(failed)
        self.assertEqual(rows, [])

    def test_a_real_reader_stays(self):
        body = "\n".join(adopt.MODULE_NAMES) + "\nreader"
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"baseline": 0, "rows": [
                    {"who": "reader", "body": body, "score": 1},
                ]}, fh)
            rows, failed = adopt.read_cognition(path)
        finally:
            os.remove(path)
        self.assertFalse(failed)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["who"], "reader")
