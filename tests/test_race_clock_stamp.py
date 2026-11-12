# -*- coding: utf-8 -*-
"""A stored comparison is written only when the table can hold created_at."""
import contextlib
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "evolve"))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "content_factory"))

import race  # noqa: E402
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

    def test_pragma_notnull_without_default_writes_nothing(self):
        cols = [
            {"name": "created_at", "notnull": 1, "dflt_value": None},
            {"name": "race_id", "notnull": 1, "dflt_value": None},
        ]
        self.assertIsNone(comparison_stamp_sql(cols, "biocomp", 2, 0.5, 100))

    def test_pragma_notnull_with_default_still_writes(self):
        cols = [
            {"name": "created_at", "notnull": 1, "dflt_value": None},
            {"name": "race_id", "notnull": 1, "dflt_value": "''"},
        ]
        sql = comparison_stamp_sql(cols, "biocomp", 2, 0.5, 100)
        self.assertEqual(sql, "INSERT INTO race_results (created_at) VALUES (100)")

    def test_filled_notnull_columns_still_write(self):
        cols = [
            {"name": "created_at", "notnull": 1, "dflt_value": None},
            {"name": "scope", "notnull": 1, "dflt_value": None},
            {"name": "n_produced", "notnull": 1, "dflt_value": None},
            {"name": "baseline", "notnull": 1, "dflt_value": None},
            {"name": "kind", "notnull": 1, "dflt_value": None},
            {"name": "note", "notnull": 0, "dflt_value": None},
        ]
        sql = comparison_stamp_sql(cols, "herb", 3, 0.25, 200)
        self.assertEqual(
            sql,
            "INSERT INTO race_results (created_at,scope,n_produced,baseline,kind) "
            "VALUES (200,'herb',3,0.25,'comparison')")

    def test_rows_without_notnull_flag_do_not_guess(self):
        cols = [{"name": "created_at"}, {"name": "race_id"}]
        sql = comparison_stamp_sql(cols, "biocomp", 2, 0.5, 100)
        self.assertEqual(sql, "INSERT INTO race_results (created_at) VALUES (100)")

    def test_omitted_baseline_notnull_writes_nothing(self):
        cols = [
            {"name": "created_at", "notnull": 0, "dflt_value": None},
            {"name": "baseline", "notnull": 1, "dflt_value": None},
        ]
        self.assertIsNone(comparison_stamp_sql(cols, "biocomp", 2, None, 100))

    def test_missing_created_at_on_pragma_rows_writes_nothing(self):
        cols = [
            {"name": "scope", "notnull": 0, "dflt_value": None},
            {"name": "race_id", "notnull": 1, "dflt_value": None},
        ]
        self.assertIsNone(comparison_stamp_sql(cols, "biocomp", 2, 0.5, 100))


class StampWrite(unittest.TestCase):
    def _run(self, fake, call):
        orig = race.d1
        race.d1 = fake
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                call()
            return buf.getvalue()
        finally:
            race.d1 = orig

    def test_stamp_prints_unfilled_notnull_and_skips_insert(self):
        calls = []

        def fake_d1(sql, params=None):
            calls.append(sql)
            return [
                {"name": "created_at", "notnull": 1, "dflt_value": None},
                {"name": "race_id", "notnull": 1, "dflt_value": None},
                {"name": "note", "notnull": 0, "dflt_value": None},
                {"name": "rev", "notnull": 1, "dflt_value": "0"},
                {"name": "author", "notnull": 1, "dflt_value": None},
                {"name": "scope", "notnull": 1, "dflt_value": None},
            ]

        out = self._run(fake_d1, lambda: race.stamp_comparison("biocomp", 2, 0.5))
        self.assertEqual(calls, ["PRAGMA table_info(race_results)"])
        self.assertIn(
            "clock stamp skipped: NOT NULL columns unfilled: race_id, author", out)
        self.assertNotIn("created_at is absent", out)
        self.assertNotIn("INSERT", out)

    def test_stamp_without_notnull_flag_still_inserts(self):
        calls = []

        def fake_d1(sql, params=None):
            calls.append(sql)
            if str(sql).startswith("PRAGMA"):
                return [{"name": "created_at"}, {"name": "note"}]
            return []

        out = self._run(fake_d1, lambda: race.stamp_comparison("biocomp", 2, 0.5))
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith("PRAGMA"))
        self.assertTrue(
            calls[1].startswith("INSERT INTO race_results (created_at) VALUES ("))
        self.assertTrue(calls[1].endswith(")"))
        self.assertNotIn("note", calls[1])
        self.assertIn("wrote 1 comparison row", out)

    def test_stamp_refuses_zero_produced_without_d1(self):
        def fake_d1(sql, params=None):
            raise AssertionError("d1 must not be called")

        out = self._run(fake_d1, lambda: race.stamp_comparison("biocomp", 0, 0.5))
        self.assertIn("clock stamp refused: no candidate", out)

    def test_stamp_missing_created_at_prints_absent(self):
        calls = []

        def fake_d1(sql, params=None):
            calls.append(sql)
            return [{"name": "scope", "notnull": 0, "dflt_value": None}]

        out = self._run(fake_d1, lambda: race.stamp_comparison("biocomp", 2, 0.5))
        self.assertEqual(calls, ["PRAGMA table_info(race_results)"])
        self.assertIn("clock stamp skipped: race_results.created_at is absent", out)


if __name__ == "__main__":
    unittest.main()
