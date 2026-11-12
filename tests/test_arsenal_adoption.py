# -*- coding: utf-8 -*-
"""DROPPED rows in adoption.txt must not be stamped integrated.

DietrichGebert/ponytail is a recorded refusal. The old writer ignored the
note and stamped every parsed row status=integrated, note="adopted <date>".
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


if __name__ == "__main__":
    unittest.main()
