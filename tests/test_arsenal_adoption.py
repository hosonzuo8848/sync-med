# -*- coding: utf-8 -*-
"""DROPPED rows must not be stamped integrated, and must not count as landings.

DietrichGebert/ponytail is a recorded refusal. The old writer ignored the
note and stamped every parsed row status=integrated, note="adopted <date>".
The old snapshot counted every parsed row as landed, so a newer DROPPED row
became last_id and could clear the stall alarm.
Rows are injected in memory via a monkeypatch of load_adoption. This file
does not read adoption.txt and does not touch a ledger on disk.
"""
import os
import sys
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts", "intel_radar"))

import arsenal_radar  # noqa: E402


PONYTAIL = "DietrichGebert/ponytail"
PONYTAIL_NOTE = (
    "DROPPED: Claude Code plugin; same duty already covered; install failed"
)
LANDED = "dreammis/social-auto-upload"
LANDED_NOTE = "sau CLI, MIT; content factory queue adapter"


def _adoption(rows):
    # monkeypatch load_adoption: in-memory rows only, never adoption.txt.
    return patch.object(arsenal_radar, "load_adoption", return_value=rows)


def _snapshot(rows, today, window_days=7):
    # Candidate files stay unread: this test must not open arsenal.json.
    with _adoption(rows), patch.object(
            arsenal_radar, "_count_known_candidates", return_value=0):
        return arsenal_radar.adoption_snapshot(
            today=today, window_days=window_days)


class TestArsenalAdoption(unittest.TestCase):
    def test_dropped_stays_dropped_and_landing_stays_integrated(self):
        rows = [
            {"id": PONYTAIL, "date": "2026-08-05", "note": PONYTAIL_NOTE},
            {"id": LANDED, "date": "2026-07-29", "note": LANDED_NOTE},
        ]
        doc = {"entries": [
            {"repo": PONYTAIL, "status": "pending", "status_note": ""},
            {"repo": LANDED, "status": "pending", "status_note": ""},
        ]}
        with _adoption(rows):
            arsenal_radar.apply_adoption_to_ledger(doc)
        by = {e["repo"]: e for e in doc["entries"]}

        pony = by[PONYTAIL]
        self.assertEqual(pony["status"], "dropped")
        self.assertEqual(pony["status_note"], PONYTAIL_NOTE)
        self.assertNotIn("adopted", pony["status_note"].lower())

        landed = by[LANDED]
        self.assertEqual(landed["status"], "integrated")
        self.assertEqual(landed["status_note"], "adopted 2026-07-29")

    def test_missing_dropped_row_is_created_dropped(self):
        # Lower-case prefix, surrounding whitespace. Old writer created this
        # row as integrated / "adopted 2026-08-05".
        note = "  dropped: refused after a real install attempt  "
        rows = [{"id": PONYTAIL, "date": "2026-08-05", "note": note}]
        doc = {"entries": []}
        with _adoption(rows):
            arsenal_radar.apply_adoption_to_ledger(doc)
        self.assertEqual(len(doc["entries"]), 1)
        e = doc["entries"][0]
        self.assertEqual(e["repo"], PONYTAIL)
        self.assertEqual(e["status"], "dropped")
        self.assertEqual(e["status_note"], note)
        self.assertNotIn("adopted", e["status_note"].lower())
        self.assertEqual(e["url"], "https://github.com/%s" % PONYTAIL)
        self.assertEqual(e["found_by"], {"dimension": "adoption.txt"})

    def test_integrated_landing_is_not_rewritten(self):
        rows = [
            {"id": "RapidAI/RapidOCR", "date": "", "note": "second OCR line"},
            {"id": PONYTAIL, "date": "2026-08-05", "note": PONYTAIL_NOTE},
        ]
        doc = {"entries": [
            {"repo": "RapidAI/RapidOCR", "status": "integrated",
             "status_note": "kept on purpose"},
            # A previous run already lied. A DROPPED note must correct it;
            # the integrated-skip applies only to real landings.
            {"repo": PONYTAIL, "status": "integrated",
             "status_note": "adopted 2026-08-05"},
        ]}
        with _adoption(rows):
            n = arsenal_radar.apply_adoption_to_ledger(doc)
        by = {e["repo"]: e for e in doc["entries"]}
        kept = by["RapidAI/RapidOCR"]
        self.assertEqual(kept["status"], "integrated")
        self.assertEqual(kept["status_note"], "kept on purpose")
        pony = by[PONYTAIL]
        self.assertEqual(pony["status"], "dropped")
        self.assertEqual(pony["status_note"], PONYTAIL_NOTE)
        self.assertNotIn("adopted", pony["status_note"].lower())
        self.assertEqual(n, 1)

    def test_undated_landing_uses_stock_note(self):
        rows = [{
            "id": "xfyun-HunyuanOCR-STABLE",
            "date": "",
            "note": "service not a repo",
        }]
        doc = {"entries": []}
        with _adoption(rows):
            arsenal_radar.apply_adoption_to_ledger(doc)
        e = doc["entries"][0]
        self.assertEqual(e["status"], "integrated")
        self.assertEqual(e["status_note"], "adopted (stock)")
        self.assertEqual(e["url"], "")

    def test_later_dropped_is_not_the_latest_landing(self):
        # The refusal is newer, so the old snapshot used it as last_id and
        # cleared stall (age 5 < 7). The real landing is older and outside
        # the window, so a correct count still stalls.
        rows = [
            {"id": LANDED, "date": "2026-07-29", "note": LANDED_NOTE},
            {"id": PONYTAIL, "date": "2026-08-05", "note": PONYTAIL_NOTE},
        ]
        snap = _snapshot(rows, "2026-08-10")
        self.assertEqual(snap["landed"], 1)
        self.assertEqual(snap["landed_recent"], 0)
        self.assertEqual(snap["last_id"], LANDED)
        self.assertEqual(snap["last_date"], "2026-07-29")
        self.assertEqual(snap["days_since"], 12)
        self.assertTrue(snap["stall"])

    def test_only_dropped_leaves_days_since_none(self):
        # Same calendar day as the refusal. The old snapshot printed
        # days_since 0 and stall False, which looks like a landing today.
        rows = [{
            "id": PONYTAIL,
            "date": "2026-08-05",
            "note": "  dropped: refused after a real install attempt  ",
        }]
        snap = _snapshot(rows, "2026-08-05")
        self.assertEqual(snap["landed"], 0)
        self.assertEqual(snap["landed_recent"], 0)
        self.assertIsNone(snap["days_since"])
        self.assertEqual(snap["last_id"], "")
        self.assertEqual(snap["last_date"], "")
        self.assertTrue(snap["stall"])

    def test_undated_landing_counts_but_dropped_cannot_set_days_since(self):
        # Stock (no date) still counts as landed and still must not set the
        # clock. A dated refusal next to it must not supply days_since either.
        rows = [
            {"id": "xfyun-HunyuanOCR-STABLE", "date": "",
             "note": "service not a repo"},
            {"id": PONYTAIL, "date": "2026-08-05", "note": PONYTAIL_NOTE},
        ]
        snap = _snapshot(rows, "2026-08-05")
        self.assertEqual(snap["landed"], 1)
        self.assertEqual(snap["landed_recent"], 0)
        self.assertIsNone(snap["days_since"])
        self.assertEqual(snap["last_id"], "")
        self.assertEqual(snap["last_date"], "")
        self.assertTrue(snap["stall"])

    def test_same_day_dropped_does_not_replace_last_landing(self):
        # Stable date sort kept the later DROPPED row as last_id when both
        # landed on the same day. last_id has to stay the real landing.
        rows = [
            {"id": LANDED, "date": "2026-08-05", "note": LANDED_NOTE},
            {"id": PONYTAIL, "date": "2026-08-05", "note": PONYTAIL_NOTE},
        ]
        snap = _snapshot(rows, "2026-08-10")
        self.assertEqual(snap["landed"], 1)
        self.assertEqual(snap["landed_recent"], 1)
        self.assertEqual(snap["last_id"], LANDED)
        self.assertEqual(snap["last_date"], "2026-08-05")
        self.assertEqual(snap["days_since"], 5)

    def test_not_dropped_substring_still_lands(self):
        note = "not DROPPED; kept"
        rows = [{"id": LANDED, "date": "2026-08-01", "note": note}]
        doc = {"entries": []}
        with _adoption(rows):
            arsenal_radar.apply_adoption_to_ledger(doc)
        snap = _snapshot(rows, "2026-08-03")
        self.assertEqual(doc["entries"][0]["status"], "integrated")
        self.assertEqual(doc["entries"][0]["status_note"], "adopted 2026-08-01")
        self.assertEqual(snap["landed"], 1)
        self.assertEqual(snap["landed_recent"], 1)
        self.assertEqual(snap["last_id"], LANDED)
        self.assertEqual(snap["days_since"], 2)

    def test_dropped_prefix_lives_in_one_function(self):
        with open(arsenal_radar.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertTrue(
            "def adoption_note_is_dropped(" in src,
            "missing adoption_note_is_dropped")
        self.assertEqual(src.count("adoption_note_is_dropped("), 3)
        self.assertEqual(src.count('startswith("DROPPED")'), 1)


if __name__ == "__main__":
    unittest.main()
