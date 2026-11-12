# -*- coding: utf-8 -*-
"""The usage footer must format after a finished meeting. No network."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from council import _usage_kwargs  # noqa: E402
from zh import ZH, t  # noqa: E402

snap = {"calls": 4, "fails": 1, "neurons": 0.0}
text = t("usage_tpl", **_usage_kwargs(snap, "gateway=2", "00:02:00"))
assert "{neurons" not in text
assert "00:02:00" in text

fields = [name for _, name, _, _ in __import__("string").Formatter().parse(ZH["usage_tpl"]) if name]
missing = [name for name in fields if name not in _usage_kwargs(snap, "-", "00:00:00")]
assert not missing, missing
print("usage-tpl-ok fields=%d" % len(fields))
