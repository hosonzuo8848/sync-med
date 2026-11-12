# -*- coding: utf-8 -*-
"""A stored comparison is written only when the table can hold created_at."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "content_factory"))

from race import comparison_stamp_sql  # noqa: E402


class StampSql(unittest.TestCase):
    def test_missing_created_at_writes_nothing(self):
        self.assertIsNone(comparison_stamp_sql(["scope"], "biocomp", 2, 0.5, 100))

    def test_created_at_only(self):
        sql = comparison_stamp_sql(["created_at"], "biocomp", 2, 0.5, 100)
        self.assertEqual(sql, "INSERT INTO race_results (created_at) VALUES (100)")

    def test_known_optional_columns_are_filled(self):
        sql = comparison_stamp_sql(
            ["created_at", "scope", "n_produced", "baseline", "kind", "note"],
            "herb", 3, 0.25, 200)
        self.assertIn("created_at", sql)
        self.assertIn("200", sql)
        self.assertIn("'herb'", sql)
        self.assertIn("3", sql)
        self.assertIn("0.25", sql)
        self.assertIn("'comparison'", sql)
        self.assertNotIn("note", sql)


if __name__ == "__main__":
    unittest.main()
