# -*- coding: utf-8 -*-
"""Offline checks for the crawl4ai browser tier. No network, no browser."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.pop("GITHUB_ACTIONS", None)
os.environ.pop("EAGLE_BROWSER", None)

import eagle_fetch as ef  # noqa: E402

assert ef.crawl4ai_fetcher_or_none() is None

try:
    ef.crawl4ai_fetcher("https://example.com/a", 1)
    raise SystemExit("browser tier ran outside Actions")
except RuntimeError as exc:
    assert "refused" in str(exc), exc

body = "<html><body>" + ("paragraph " * 40) + "</body></html>"
ef._fetch_http = lambda url, timeout: (200, "<html><body></body></html>")
ef._fetch_jina = lambda url, timeout, api_key="": (500, "")
seen = []


def fake_browser(url, timeout):
    seen.append(url)
    return 200, body


result = ef.fetch_page("https://example.com/a", browser_fetcher=fake_browser, timeout=1)
assert result.ok and result.tier == "browser", (result.tier, result.notes)
assert result.chars > 120, result.chars
assert seen == ["https://example.com/a"]

os.environ["EAGLE_BROWSER"] = "1"
os.environ["EAGLE_BROWSER_MAX"] = "1"
ef._browser_used = 1
try:
    ef.crawl4ai_fetcher("https://example.com/b", 1)
    raise SystemExit("browser cap did not stop")
except RuntimeError as exc:
    assert "cap" in str(exc), exc

print("offline-ok tier=browser chars=%d cap_refused=1 local_refused=1" % result.chars)
