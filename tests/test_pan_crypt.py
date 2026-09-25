# -*- coding: utf-8 -*-
"""Offline self-check for pan_crypt.py's encrypt/decrypt round trip, and a
plain-text guard on the three workflows that hand a 123 token from a prep
job to its run-job shards (see the "pan login once" / "decrypt prefetched
pan token(s)" steps in sync.yml / ocr_ndl.yml / warm_dlink.yml).

Why this exists (2026-09-26 rework): the first version of this handoff put
the masked plaintext token straight into a job output. GitHub Actions
silently drops a job output whose value has already been through
::add-mask:: ("Skip output '<name>' since it may contain secret", confirmed
on a throwaway branch in this repo, run 36201413266) -- the downstream job
read an EMPTY string and the script silently fell through to "log in
myself", with no error anywhere. The fix encrypts the token with the
credential secret the run job already has before it goes into the job
output, and only ever writes the token itself into $GITHUB_ENV, masked,
never into an output and never echoed. This test guards against
reintroducing either mistake.

No network calls: encrypt()/decrypt() shell out to the local `openssl`
binary only, never to 123's API. `python -m pytest tests/test_pan_crypt.py -q`
"""
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
WF_DIR = os.path.join(REPO, ".github", "workflows")

import pan_crypt  # noqa: E402


class TestRoundTrip(unittest.TestCase):

    def test_encrypt_then_decrypt_recovers_the_token(self):
        token = "fake-token-abc123XYZ"
        passphrase = "fake-passphrase-does-not-matter"
        cipher = pan_crypt.encrypt(token, passphrase)
        self.assertNotIn(token, cipher, "ciphertext must not contain the plaintext token")
        self.assertEqual(pan_crypt.decrypt(cipher, passphrase), token)

    def test_wrong_passphrase_does_not_recover_the_token(self):
        cipher = pan_crypt.encrypt("fake-token-abc123XYZ", "right-passphrase")
        self.assertIsNone(pan_crypt.decrypt(cipher, "wrong-passphrase"))

    def test_empty_cipher_is_treated_as_no_prefetched_token(self):
        # what a shard sees when prep's output genuinely came through empty
        # (e.g. prep itself failed) -- must fail closed, not raise.
        self.assertIsNone(pan_crypt.decrypt("", "any-passphrase"))

    def test_empty_token_round_trips_too(self):
        # a workflow step guards against this before calling encrypt (prep's
        # login step exits 1 on an empty login token), but the primitive
        # itself should not choke on the edge case.
        cipher = pan_crypt.encrypt("", "pw")
        self.assertEqual(pan_crypt.decrypt(cipher, "pw"), "")


PLAINTEXT_TOKEN_OUTPUT = re.compile(r'echo\s+"token=\$TOKEN"\s*>>\s*"?\$GITHUB_OUTPUT"?')
PLAINTEXT_TOKEN_ECHO = re.compile(r'echo\s+(?!"::add-mask::)[^\n]*\$TOKEN\b')


class TestNoPlaintextTokenLeavesTheRunner(unittest.TestCase):
    """Static grep guard -- not a substitute for the runtime behavior, but
    cheap and catches the exact regression class this rework fixes."""

    def _workflow_text(self, fn):
        with open(os.path.join(WF_DIR, fn), encoding="utf-8") as f:
            return f.read()

    def test_no_workflow_writes_a_raw_token_to_github_output(self):
        for fn in ("sync.yml", "ocr_ndl.yml", "warm_dlink.yml"):
            with self.subTest(workflow=fn):
                self.assertIsNone(PLAINTEXT_TOKEN_OUTPUT.search(self._workflow_text(fn)),
                                   "%s writes a raw (unencrypted) token to GITHUB_OUTPUT" % fn)

    def test_no_workflow_writes_a_token_variable_to_github_output(self):
        # The only thing allowed into $GITHUB_OUTPUT is the CIPHERTEXT (see
        # test_ciphertext_output_is_allowed below). A raw token variable reaching
        # $GITHUB_OUTPUT -- in any echo form -- is the exact regression this rework fixes
        # (job outputs get silently dropped once masked, see pan_crypt.py's module doc).
        # Writing the same variable to $GITHUB_ENV is fine (that's the whole design) and
        # must not be flagged here.
        to_output = re.compile(r'>>\s*"?\$GITHUB_OUTPUT"?')
        token_var = re.compile(r"\$\{?(TOKEN|TOK|TOK2)\}?\b")
        for fn in ("sync.yml", "ocr_ndl.yml", "warm_dlink.yml"):
            with self.subTest(workflow=fn):
                bad = [ln for ln in self._workflow_text(fn).splitlines()
                       if to_output.search(ln) and token_var.search(ln)]
                self.assertEqual(bad, [], "%s: a token variable reaches $GITHUB_OUTPUT: %r" % (fn, bad))

    def test_every_token_variable_going_to_github_env_is_masked_first(self):
        # Every workflow that writes a token-family variable into $GITHUB_ENV must also
        # call ::add-mask:: on that same variable somewhere in the file -- otherwise this
        # job's own later logs (or a future accidental print in the Python script) could
        # echo the plaintext back out.
        to_env = re.compile(r'echo\s+"(\w+)=\$(TOKEN|TOK|TOK2)"\s*>>\s*"?\$GITHUB_ENV"?')
        for fn in ("sync.yml", "ocr_ndl.yml", "warm_dlink.yml"):
            with self.subTest(workflow=fn):
                text = self._workflow_text(fn)
                for m in to_env.finditer(text):
                    var = m.group(2)
                    self.assertIn("::add-mask::$%s" % var, text,
                                  "%s: $%s reaches GITHUB_ENV without a matching ::add-mask::" % (fn, var))

    def test_ciphertext_output_is_allowed(self):
        # sanity check on the test itself: the cipher IS meant to reach
        # GITHUB_OUTPUT, so the detector above must not flag that line.
        for fn in ("sync.yml", "ocr_ndl.yml", "warm_dlink.yml"):
            with self.subTest(workflow=fn):
                self.assertIn("cipher=$CIPHER", self._workflow_text(fn))


if __name__ == "__main__":
    unittest.main()
