# -*- coding: utf-8 -*-
"""One 123 access_token login for a whole GitHub Actions run.

Used by the prep job of sync.yml / ocr_ndl.yml / warm_dlink.yml so a run logs
in exactly once (per credential pair) instead of every matrix shard calling
/api/v1/access_token on its own and revoking each other's token (123 keeps
only the newest login per account). The prep job captures this script's
stdout, applies ::add-mask:: to it, and hands it to the run job as a job
output; the run job re-applies ::add-mask:: itself (job outputs are not
masked across jobs -- each job runs on a fresh runner) before using it. See
the "pan login (once for the whole run)" step in those workflow files.

Prints ONLY the bare token to stdout -- callers must mask before it can reach
any later log line. Prints nothing and exits non-zero on failure.

env: PAN_CID, PAN_SEC (clientID/clientSecret pair), optional PAN_BASE.
"""
import os
import sys

import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")


def login(cid, sec):
    r = requests.post(PAN + "/api/v1/access_token",
                       headers={"Platform": "open_platform", "Content-Type": "application/json"},
                       json={"clientID": cid, "clientSecret": sec}, timeout=30)
    tok = (r.json().get("data") or {}).get("accessToken")
    if not tok:
        sys.exit("pan_login: token fetch failed: " + r.text[:200])
    return tok


if __name__ == "__main__":
    print(login(os.environ["PAN_CID"], os.environ["PAN_SEC"]))
