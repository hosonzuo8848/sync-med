# -*- coding: utf-8 -*-
"""An unread graph count is not the same as having no graph gap.

measure_gaps() swallowed a failed node-count query with a bare except.
The harvest then continued as if that gap had not been asked. A real
count still adds the gap. This test does not call D1 or a model.
"""
import os
import sys
import unittest
from io import StringIO
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))

import harvest  # noqa: E402


class UnreadGraphCount(unittest.TestCase):
    def setUp(self):
        self._d1 = harvest.d1

    def tearDown(self):
        harvest.d1 = self._d1

    def test_unread_graph_count_is_reported(self):
        def fake(sql):
            if "sue_graph_nodes" in sql:
                raise RuntimeError("down")
            return []

        harvest.d1 = fake
        buf = StringIO()
        with redirect_stdout(buf):
            gaps = harvest.measure_gaps()
        self.assertFalse(any("3%" in g.get("now", "") for g in gaps))
        self.assertIn("graph count unread", buf.getvalue())

    def test_a_real_graph_count_stays(self):
        def fake(sql):
            if "sue_graph_nodes" in sql:
                return [{"c": 12}]
            return []

        harvest.d1 = fake
        gaps = harvest.measure_gaps()
        self.assertTrue(any("12" in g.get("now", "") for g in gaps))
