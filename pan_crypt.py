# -*- coding: utf-8 -*-
"""Symmetric encrypt/decrypt helpers for handing a 123 access token from a
workflow's prep job to its run-job shards through a job output, WITHOUT ever
putting the plaintext token in that output.

Why this exists (2026-09-26 rework): GitHub Actions silently drops any job
output whose value it thinks might be a secret -- once a value has been
through ::add-mask::, writing that same value into $GITHUB_OUTPUT is
rejected server-side ("Skip output '<name>' since it may contain secret"),
and the downstream job reads an EMPTY string, no error anywhere. Confirmed
on a throwaway branch in this repo (run 36201413266): the masked output came
back masked_len=0 while a plain sibling output in the same job came back
plain_len=11. Handing the plaintext token through a job output therefore
never actually reached a shard -- every run silently fell through to
"prefetched token absent -> log in myself", i.e. the original fix did not
fix anything.

Fix: the prep job encrypts the token with the SAME secret the run job
already has (PAN_SEC / PAN_CLIENT_SECRET, whichever credential pair that
workflow uses) as the passphrase, and writes only the CIPHERTEXT to the job
output. Ciphertext is not a registered secret value, so GitHub does not
touch it. The run job decrypts with its own copy of the same secret,
immediately masks the plaintext (mask is per-job-runner, never shared --
see the workflow comments), and hands it to the script through $GITHUB_ENV
(env-file writes are not subject to the output-scanning behavior above).
The plaintext token is never echoed and never written to $GITHUB_OUTPUT.

CLI:
  python pan_crypt.py encrypt   (stdin: plaintext token,  env PAN_PASS)  -> stdout: ciphertext (base64, one line)
  python pan_crypt.py decrypt   (stdin: ciphertext,       env PAN_PASS)  -> stdout: plaintext token; exit 1 + nothing on failure

Shells out to the `openssl enc -aes-256-cbc -pbkdf2` CLI (present on every
GitHub Actions Ubuntu runner) instead of adding a Python crypto dependency.
"""
import os
import subprocess
import sys

OPENSSL_BASE = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-a", "-A", "-pass", "env:PAN_PASS"]


def _run(args, data, passphrase):
    env = dict(os.environ)
    env["PAN_PASS"] = passphrase
    return subprocess.run(OPENSSL_BASE + args, input=data.encode("utf-8"),
                           capture_output=True, env=env)


def encrypt(token, passphrase):
    r = _run(["-salt"], token, passphrase)
    if r.returncode != 0:
        sys.exit("pan_crypt encrypt failed: " + r.stderr.decode(errors="replace")[:200])
    return r.stdout.decode().strip()


def decrypt(cipher, passphrase):
    """None on any failure (wrong passphrase, corrupt/empty ciphertext) --
    callers must treat None same as "no prefetched token" and fall back."""
    if not cipher:
        return None
    r = _run(["-d"], cipher, passphrase)
    if r.returncode != 0:
        return None
    return r.stdout.decode().strip()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    pw = os.environ.get("PAN_PASS", "")
    inp = sys.stdin.read().strip()
    if mode == "encrypt":
        print(encrypt(inp, pw))
    elif mode == "decrypt":
        out = decrypt(inp, pw)
        if out is None:
            sys.exit(1)
        print(out)
    else:
        sys.exit("usage: pan_crypt.py encrypt|decrypt  (stdin=data, env PAN_PASS=passphrase)")
