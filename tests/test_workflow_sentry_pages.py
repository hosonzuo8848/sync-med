# -*- coding: utf-8 -*-
"""The sentry must see every workflow and must speak up on a new failure.

It read one page of 100 workflows while the repo has more, so the tail
(webp-convert among them) was never checked. It also only rewrote the
body of an old Issue, which notifies no one. This test does not call
GitHub or D1.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import workflow_sentry as ws  # noqa: E402


def _page(path):
    from urllib.parse import parse_qs, urlsplit
    return int(parse_qs(urlsplit(path).query)["page"][0])


def _wf(i):
    return {"id": i, "path": ".github/workflows/w%d.yml" % i, "state": "active"}


class SentryPages(unittest.TestCase):
    def test_every_page_is_read(self):
        pages = {1: [_wf(i) for i in range(100)], 2: [_wf(i) for i in range(100, 115)]}

        def fake(path, *a, **k):
            page = _page(path)
            return {"total_count": 115, "workflows": pages.get(page, [])}

        got = ws.list_workflows(fake)
        self.assertEqual(len(got), 115)
        self.assertIn(".github/workflows/w114.yml", [w["path"] for w in got])

    def test_a_single_page_still_works(self):
        def fake(path, *a, **k):
            page = _page(path)
            return {"total_count": 3, "workflows": [_wf(i) for i in range(3)] if page == 1 else []}

        self.assertEqual(len(ws.list_workflows(fake)), 3)

    def test_new_failure_is_reported(self):
        old = ws.failed_marker(["a.yml"])
        self.assertEqual(ws.new_failures(old, ["a.yml", "b.yml"]), ["b.yml"])

    def test_same_failures_stay_quiet(self):
        old = ws.failed_marker(["b.yml", "a.yml"])
        self.assertEqual(ws.new_failures(old, ["a.yml", "b.yml"]), [])

    def test_an_old_body_without_a_marker_reports_everything(self):
        self.assertEqual(ws.new_failures("old text", ["a.yml"]), ["a.yml"])
