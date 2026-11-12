# -*- coding: utf-8 -*-
"""Illegal judge verdicts must not be rewritten to skip.

The frozen sheet has 20 items: 17 gold keys are skip and 3 are adopt.
A normal gateway body whose verdict is "...", "", or some other word used
to be stored as skip, and mode == want then reported 17 hits. Exceptions
stay on the ERR path. This module never calls the gateway, brain(), or
scan_stack().
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts", "evolve"))

import judge_scorecard as js  # noqa: E402


def _sheet_shape():
    wants = {}
    for q in js.EXAM:
        wants[q["want"]] = wants.get(q["want"], 0) + 1
    return wants


class TestJudgeScorecardInvalid(unittest.TestCase):
    def setUp(self):
        wants = _sheet_shape()
        self.assertEqual(len(js.EXAM), 20)
        self.assertEqual(wants, {"skip": 17, "adopt": 3})

    def _run_main(self, ask, repeat=3):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        argv = ["judge_scorecard.py", "--repeat", str(repeat), "--report", path]
        try:
            with mock.patch.object(js, "_ask_any", side_effect=ask), \
                 mock.patch.object(js, "scan_stack", return_value=(set(), [])), \
                 mock.patch.object(js, "brain", return_value="offline-champ"), \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch("sys.stdout", new=io.StringIO()):
                rc = js.main()
            self.assertEqual(rc, 0)
            with io.open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        finally:
            if os.path.exists(path):
                os.remove(path)
        self.assertEqual(data["exam_n"], 20)
        self.assertEqual(len(data["rows"]), 20)
        return data

    def _hits(self, data):
        return sum(1 for row in data["rows"] if row["correct"])

    def test_placeholder_sheet_scores_zero(self):
        calls = {"n": 0}

        def ask(*args, **kwargs):
            calls["n"] += 1
            return (json.dumps({"verdict": "..."}), "offline")

        data = self._run_main(ask, repeat=3)
        self.assertEqual(calls["n"], 60)
        self.assertEqual(self._hits(data), 0)
        self.assertEqual(data["accuracy"], 0.0)
        self.assertEqual(data["by_class"]["skip"]["correct"], 0)
        self.assertEqual(data["by_class"]["skip"]["total"], 17)
        self.assertEqual(data["by_class"]["adopt"]["correct"], 0)
        self.assertEqual(data["by_class"]["adopt"]["total"], 3)
        self.assertTrue(all(v == "..." for row in data["rows"] for v in row["votes"]))
        self.assertTrue(all(row["mode"] == "..." for row in data["rows"]))
        self.assertFalse(any(row["correct"] for row in data["rows"]))
        self.assertFalse(any(str(row["mode"]).startswith("ERR") for row in data["rows"]))

    def test_empty_verdict_is_not_scored_as_skip(self):
        def ask(*args, **kwargs):
            return (json.dumps({"verdict": ""}), "offline")

        data = self._run_main(ask, repeat=1)
        self.assertEqual(self._hits(data), 0)
        self.assertEqual(data["accuracy"], 0.0)
        self.assertTrue(all(row["mode"] == "" for row in data["rows"]))
        self.assertFalse(any(str(row["mode"]).startswith("ERR") for row in data["rows"]))

    def test_unknown_word_is_not_scored_as_skip(self):
        def ask(*args, **kwargs):
            return (json.dumps({"verdict": "maybe"}), "offline")

        data = self._run_main(ask, repeat=1)
        self.assertEqual(self._hits(data), 0)
        self.assertEqual(data["accuracy"], 0.0)
        self.assertTrue(all(row["mode"] == "maybe" for row in data["rows"]))
        self.assertFalse(any(str(row["mode"]).startswith("ERR") for row in data["rows"]))

    def test_exception_stays_err_and_scores_zero(self):
        def ask(*args, **kwargs):
            raise TimeoutError("offline")

        data = self._run_main(ask, repeat=1)
        self.assertEqual(self._hits(data), 0)
        self.assertTrue(all(row["mode"] == "ERR:TimeoutError" for row in data["rows"]))

    def test_legal_gold_sheet_is_all_correct(self):
        def ask(champ, user, supplier, **kwargs):
            repo = user.split("\n", 1)[0].split(":", 1)[1]
            want = next(q["want"] for q in js.EXAM if q["repo"] == repo)
            return (json.dumps({"verdict": want}), "offline")

        data = self._run_main(ask, repeat=3)
        self.assertEqual(self._hits(data), 20)
        self.assertEqual(data["accuracy"], 1.0)
        self.assertEqual(data["by_class"]["skip"]["correct"], 17)
        self.assertEqual(data["by_class"]["adopt"]["correct"], 3)

    def test_legal_skip_still_matches_only_skip_keys(self):
        def ask(*args, **kwargs):
            return (json.dumps({"verdict": "Skip"}), "offline")

        data = self._run_main(ask, repeat=1)
        self.assertEqual(self._hits(data), 17)
        self.assertEqual(data["accuracy"], 0.85)
        self.assertEqual(data["by_class"]["skip"]["correct"], 17)
        self.assertEqual(data["by_class"]["adopt"]["correct"], 0)
        self.assertTrue(all(row["mode"] == "skip" for row in data["rows"]))

    def test_legal_adopt_and_watch_follow_exact_match(self):
        def ask_adopt(*args, **kwargs):
            return (json.dumps({"verdict": "adopt"}), "offline")

        adopt = self._run_main(ask_adopt, repeat=1)
        self.assertEqual(self._hits(adopt), 3)
        self.assertEqual(adopt["accuracy"], 0.15)
        self.assertEqual(adopt["by_class"]["adopt"]["correct"], 3)
        self.assertEqual(adopt["by_class"]["skip"]["correct"], 0)

        def ask_watch(*args, **kwargs):
            return (json.dumps({"verdict": "watch"}), "offline")

        watch = self._run_main(ask_watch, repeat=1)
        self.assertEqual(self._hits(watch), 0)
        self.assertEqual(watch["accuracy"], 0.0)
        self.assertTrue(all(row["mode"] == "watch" for row in watch["rows"]))

    def test_vote_correct_matches_equality_for_legal_tokens(self):
        for q in js.EXAM:
            for mode in ("adopt", "watch", "skip"):
                self.assertEqual(js.vote_correct(q, mode), mode == q["want"])
        probed = next(q for q in js.EXAM if q["want"] == "skip" and q.get("probe"))
        self.assertFalse(js.vote_correct(probed, "watch"))
        self.assertFalse(js.vote_correct(probed, "..."))
        watch_q = {
            "repo": "example/watch-only",
            "want": "watch",
            "probe": "standing_host",
        }
        self.assertTrue(js.vote_correct(watch_q, "watch"))
        self.assertFalse(js.vote_correct(watch_q, "skip"))
        self.assertFalse(js.vote_correct(watch_q, "adopt"))
        self.assertFalse(js.vote_correct(watch_q, "..."))
        self.assertFalse(js.vote_correct(watch_q, ""))
        self.assertFalse(js.vote_correct(watch_q, "ERR:TimeoutError"))


if __name__ == "__main__":
    unittest.main()
