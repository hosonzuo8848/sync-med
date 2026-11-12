# -*- coding: utf-8 -*-
"""A scrapped plan stays on the race table and out of critique, votes, the referee, and the report body.

The 2026-11-12 dry run let a number-less monologue through on ok=True. It was
vetoed, and any veto made the machine verdict watch. These tests fail that
behavior: the monologue body must not enter a critique prompt, an all-scrapped
slate must not call the referee, and a veto of only scrapped plans must not
become watch. A veto of a numbered plan stays watch. The report pastes only
debatable full texts. The critique intro counts debatable plans minus one.
The lead names only vendors that have a debatable plan.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "council"))

import council  # noqa: E402
from zh import t  # noqa: E402

TOPIC = {"title": "gap", "current": "1", "target": "9", "organ": "recall", "basis": "d1"}
JUDGE = {"id": "judge-1", "vendor": "judge", "house": "judge"}
A_BODY = "PLAN-A-NUMBERED-BODY coverage 2% -> 8%"
B_BODY = "PLAN-B-MONOLOGUE-NO-NUMBER rethink the whole stack"


def _plan(letter, text, numbered, ok=True):
    return {
        "letter": letter,
        "text": text,
        "ok": ok,
        "numbered": numbered,
        "scrapped": bool(ok) and not numbered,
        "where": "",
        "numbers": "2% -> 8%" if numbered else "",
        "sec": 1.0,
        "err": "",
        "seat": {"id": "seat-" + letter, "vendor": "vendor-" + letter,
                 "house": "house-" + letter},
    }


def _crit(letter, verdicts):
    return {
        "critic": {"id": "seat-" + letter},
        "letter": letter,
        "text": "notes",
        "verdicts": verdicts,
        "ranking": [],
        "flat": False,
        "err": "",
    }


class ScrapGate(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.real_call = council.roster.call

        def fake(seat, system, user, **kw):
            self.calls.append({
                "id": seat.get("id"),
                "stage": kw.get("stage"),
                "user": user,
            })
            return "mocked-reply"

        council.roster.call = fake

    def tearDown(self):
        council.roster.call = self.real_call

    def test_debatable_requires_ok_and_numbered(self):
        self.assertTrue(council.debatable(_plan("A", A_BODY, True)))
        self.assertFalse(council.debatable(_plan("B", B_BODY, False)))
        self.assertFalse(council.debatable(_plan("C", "blank", True, ok=False)))

    def test_race_table_still_prints_the_scrapped_plan(self):
        table = council.race_table([
            _plan("A", A_BODY, True),
            _plan("B", B_BODY, False),
        ])
        self.assertIn(A_BODY, table)
        self.assertIn(B_BODY, table)

    def test_report_body_pastes_numbered_plan_only_and_peer_count_is_zero(self):
        # Old build_report pasted every ok text and set the critique intro to
        # ok_count-1. That prints B_BODY as a candidate and a peer count of 1
        # when only A is debatable. race_table must still show both rows.
        plans = [_plan("A", A_BODY, True), _plan("B", B_BODY, False)]
        table = council.race_table(plans)
        self.assertIn(A_BODY, table)
        self.assertIn(B_BODY, table)
        self.assertIn(t("lbl_scrapped"), table)
        self.assertIn(t("lbl_number_ok"), table)
        body = "\n".join(council.report_plan_full_blocks(plans))
        self.assertIn(A_BODY, body)
        self.assertNotIn(B_BODY, body)
        self.assertEqual(council.critique_peer_count(plans), 0)
        self.assertEqual(
            council.critique_peer_count([
                _plan("A", A_BODY, True),
                _plan("C", "PLAN-C-NUMBERED-BODY coverage 3% -> 9%", True),
            ]),
            1)
        self.assertEqual(council.critique_peer_count([_plan("B", B_BODY, False)]), 0)

    def test_scrapped_body_stays_out_of_the_critique_prompt(self):
        plans = [_plan("A", A_BODY, True), _plan("B", B_BODY, False)]
        got = council.critique(TOPIC, plans, [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["id"], "seat-B")
        self.assertEqual(self.calls[0]["stage"], "critique")
        self.assertIn(A_BODY, self.calls[0]["user"])
        self.assertNotIn(B_BODY, self.calls[0]["user"])
        self.assertEqual([row["letter"] for row in got], ["B"])

    def test_all_scrapped_skips_critique_and_the_referee(self):
        plans = [
            _plan("A", "PLAN-A-MONOLOGUE-NO-NUMBER", False),
            _plan("B", B_BODY, False),
        ]
        self.assertEqual(council.critique(TOPIC, plans, []), [])
        text, err = council.verdict(TOPIC, plans, [], "matrix", JUDGE)
        self.assertEqual((text, err), ("", "no-numbered-plan"))
        self.assertEqual(self.calls, [])

    def test_referee_prompt_omits_the_scrapped_body(self):
        plans = [_plan("A", A_BODY, True), _plan("B", B_BODY, False)]
        text, err = council.verdict(TOPIC, plans, [], "matrix", JUDGE)
        self.assertEqual((text, err), ("mocked-reply", ""))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["id"], "judge-1")
        self.assertEqual(self.calls[0]["stage"], "verdict")
        self.assertIn(A_BODY, self.calls[0]["user"])
        self.assertNotIn(B_BODY, self.calls[0]["user"])

    def test_veto_of_a_scrapped_plan_is_not_counted(self):
        plans = [_plan("A", A_BODY, True), _plan("B", B_BODY, False)]
        veto = t("lbl_veto")
        critiques = [
            _crit("B", {"A": {"verdict": veto, "fatal": "real gap"}}),
            _crit("A", {"B": {"verdict": veto, "fatal": "no number"}}),
        ]
        matrix, vetoes = council.vote_matrix(plans, critiques)
        self.assertEqual(list(matrix), ["A"])
        self.assertEqual([v["letter"] for v in vetoes], ["A"])
        md = council.matrix_md(plans, critiques, matrix)
        self.assertIn(t("plan_word") + "A", md)
        self.assertNotIn(t("plan_word") + "B", md)

    def test_only_scrapped_plans_hold_even_with_vetoes(self):
        record = {
            "plans": [_plan("B", B_BODY, False)],
            "vetoes": [
                {"letter": "B", "reason": "no"},
                {"letter": "B", "reason": "still no"},
            ],
            "verdict": "referee wrote a paragraph anyway",
            "verdict_err": "",
            "critiques": [{"flat": False}],
        }
        self.assertEqual(
            council._machine_verdict(record), ("hold", "no-surviving-plan"))
        record["verdict"] = ""
        record["verdict_err"] = "no-numbered-plan"
        self.assertEqual(
            council._machine_verdict(record), ("hold", "no-surviving-plan"))

    def test_veto_of_a_numbered_plan_is_still_watch(self):
        record = {
            "plans": [_plan("A", A_BODY, True)],
            "vetoes": [{"letter": "A", "reason": "gap"}],
            "verdict": "referee kept the dissent",
            "verdict_err": "",
            "critiques": [{"flat": False}],
        }
        self.assertEqual(council._machine_verdict(record), ("watch", "veto:1"))

    def test_lead_names_debatable_vendors_only(self):
        # Old build_report joined every ok seat into the lead. A numbered
        # gateway plan plus a scrapped sensenova monologue then listed both
        # vendors as if sensenova had a plan the meeting could vote on.
        a = _plan("A", A_BODY, True)
        b = _plan("B", B_BODY, False)
        a["seat"]["vendor"] = "gateway"
        b["seat"]["vendor"] = "sensenova"
        topic = dict(TOPIC, sev=0.5, sql="select 1")
        mixed = {
            "topic": topic,
            "plans": [a, b],
            "matrix_md": "matrix",
            "critiques": [],
            "verdict": "",
            "verdict_err": "",
            "dissent": "none",
        }
        only_scrap = dict(mixed, plans=[b])
        self.assertEqual(council.report_seat_vendors([mixed]), ["gateway"])
        self.assertEqual(council.report_seat_vendors([only_scrap]), [])

        def report_for(results):
            return council.build_report(
                "2026-11-12", [topic], results, [], [], [],
                (0, "", ""), JUDGE, True, {}, "1", "http://example.test/1")

        lead = report_for([mixed]).split(t("rpt_h2_triage", n=1), 1)[0]
        self.assertIn("**gateway**", lead)
        self.assertNotIn("sensenova", lead)
        empty_lead = report_for([only_scrap]).split(t("rpt_h2_triage", n=1), 1)[0]
        self.assertIn("**-**", empty_lead)
        self.assertNotIn("sensenova", empty_lead)
        full = report_for([mixed])
        self.assertIn(t("rpt_h2_race", n=2), full)
        self.assertIn("sensenova", full)


if __name__ == "__main__":
    unittest.main()
