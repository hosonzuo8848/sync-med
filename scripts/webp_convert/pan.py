# -*- coding: utf-8 -*-
"""Minimal 123pan open-platform client for the webp-convert pipeline.

- A shard never logs in on its own. It gets the shared token (run.py token)
  and, on 401, asks its on_401 hook for a replacement (run.py Renewer).
- Every call passes a per-class rate limiter: list / dl / up / misc.
  file/list has a hard 3 req/s per ACCOUNT limit shared by every consumer of
  that account, so the defaults stay well below it.
- The body code is always checked, never just the HTTP status:
    no answer / not JSON (5xx page) network hiccup -> the same call is retried in place
                                   after NET_BACKOFF (2, 5, 15 s), then PanError("net")
    upload code=1 "internal error" same in-place retries (INTERNAL_WORD), then PanError(1)
    401                            token pushed out by a newer login -> on_401 / TokenInvalid
                                   (checked first: its text says "exceeded the limit")
    429 or the "too frequent" text rate limited -> back off, retry
    anything else                  PanError, no retry here
- Breaker: BREAK_AFTER consecutive failed calls of one class ending in the same
  code raise Tripped, and the shard stops. Typical causes: code=1 on every
  write (123 account-level write ban; reads keep working) or rate limiting that
  outlasts every retry. Hammering on only extends the ban.
- Every failed attempt is counted as "<api>:<kind>" into the calling thread's
  `tl.sink` Counter, so run.py can book it per volume. api = CATEGORY[where]
  (token / mkdir / upload / list / download / other); kind = net, http<status>,
  401, 429 or api<code>. `tl.msgs` keeps the latest raw answer per key
  ("code=<c> <message>", or the exception for no answer), token-free, <= 200 chars.
  These go to the private ledger only, never to the public log.
- No instant-upload probe: 318 probes in the first 20-volume run found 0 hits,
  and each miss leaves an unfinished pre-upload session on 123. A page whose
  upload may have landed is simply sent again (duplicate=2 overwrites).
"""
import collections
import hashlib
import re
import threading
import time

import requests

API = "https://open-api.123pan.com"
RATE_WORD = "\u9891\u7e41"          # "too frequent" in 123's rate-limit message
BREAK_AFTER = 5
NET_BACKOFF = (2, 5, 15)            # seconds before each retry of a call that got no usable answer
SLEEP = time.sleep                  # selftest swaps in a no-op
# the `where` label of each call -> the API category booked with its failures.
# single/create is one call that covers precheck, upload and completion.
CATEGORY = {"access_token": "token", "user_info": "token", "mkdir": "mkdir", "single": "upload",
            "list": "list", "download_info": "download"}
MSG_MAX = 200
JWT_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
# code=1 with this text on an upload is a hiccup of 123's upload service (runs
# 36200294899 / 36192502762: 31 of them, most gone on retry), not a write ban: the
# upload is retried in place like a network failure; the breaker still stops a streak
INTERNAL_WORD = "\u670d\u52a1\u5185\u90e8\u9519\u8bef"      # "internal server error"


class TokenInvalid(Exception):
    """401 and no replacement token: the shard stops."""


class PanError(Exception):
    def __init__(self, where, code):
        super().__init__("%s code=%s" % (where, code))
        self.code = code


class Tripped(PanError):
    """The same failure BREAK_AFTER times in a row: stop the shard."""


class Limiter:
    """Evenly spaced calls, thread safe. per_sec <= 0 disables it."""

    def __init__(self, per_sec):
        self.gap = 1.0 / per_sec if per_sec > 0 else 0.0
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        if not self.gap:
            return
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)


def login(client_id, client_secret):
    """Only the token broker calls this. Returns (token, expiredAt-string)."""
    r = requests.post(API + "/api/v1/access_token", timeout=60,
                      headers={"Platform": "open_platform"},
                      json={"clientID": client_id, "clientSecret": client_secret})
    j = r.json()
    d = j.get("data") or {}
    if j.get("code") != 0 or not d.get("accessToken"):
        raise PanError("access_token", j.get("code"))
    return d["accessToken"], d.get("expiredAt") or ""


class Pan:
    def __init__(self, token, qps, max_rate_retry=8, on_401=None):
        self.token = token
        self.on_401 = on_401            # callable(rejected_token) -> new token or None
        self.lim = {k: Limiter(v) for k, v in qps.items()}
        self.s = requests.Session()
        self.max_rate_retry = max_rate_retry
        self.stats = collections.Counter()
        self.tl = threading.local()     # tl.sink: Counter of failed attempts for the thread's current book
        self.streak = {}                # class -> [code, consecutive failures]
        self._lists = {}
        self._dom = None
        self._lock = threading.Lock()
        self._mk_locks = collections.defaultdict(threading.Lock)

    # ---- transport -------------------------------------------------------
    def _note(self, where, kind, detail, tok):
        sink = getattr(self.tl, "sink", None)
        if sink is None:
            return
        key = "%s:%s" % (CATEGORY.get(where, "other"), kind)
        sink[key] += 1
        msgs = getattr(self.tl, "msgs", None)
        if msgs is not None:
            for t in {tok, self.token} - {None, ""}:
                detail = detail.replace(t, "<tok>")
            msgs[key] = JWT_RE.sub("<tok>", " ".join(str(detail).split()))[:MSG_MAX]

    def _fail(self, cls, where, code):
        with self._lock:
            s = self.streak.setdefault(cls, [None, 0])
            s[1] = s[1] + 1 if s[0] == code else 1
            s[0] = code
            n = s[1]
        if n >= BREAK_AFTER:
            self.stats["tripped"] += 1
            raise Tripped(where, code)
        raise PanError(where, code)

    def _call(self, cls, method, url, where, **kw):
        net_fail = rate_hit = 0
        renewed = False
        timeout = kw.pop("timeout", 90)
        while True:
            self.lim[cls].wait()
            self.stats["call_" + cls] += 1
            tok = self.token
            r = None
            try:
                r = self.s.request(method, url, timeout=timeout, headers={
                    "Platform": "open_platform", "Authorization": "Bearer " + tok}, **kw)
                j = r.json()
            except Exception as e:                              # noqa: BLE001
                # no answer at all, or an answer that is not JSON (a 5xx page):
                # retry this very call (this page) in place, never the whole volume
                if r is None:
                    self._note(where, "net", "%s %s" % (type(e).__name__, e), tok)
                else:
                    self._note(where, "http%s" % getattr(r, "status_code", ""),
                               "http %s %s" % (getattr(r, "status_code", ""), getattr(r, "text", "")), tok)
                net_fail += 1
                if net_fail > len(NET_BACKOFF):
                    self._fail(cls, where, "net")
                SLEEP(NET_BACKOFF[net_fail - 1])
                continue
            code = j.get("code")
            if code == 0:
                with self._lock:
                    self.streak.pop(cls, None)
                return j.get("data") or {}
            said = "code=%s %s" % (code, j.get("message", ""))
            if code == 401:
                self.stats["token_401"] += 1
                self._note(where, "401", said, tok)
                new = None if renewed or not self.on_401 else self.on_401(tok)
                if not new:
                    raise TokenInvalid(where)
                self.token, renewed = new, True
                continue
            if code == 429 or RATE_WORD in str(j.get("message", "")):
                rate_hit += 1
                self.stats["rate_limited"] += 1
                self._note(where, "429", said, tok)
                if rate_hit > self.max_rate_retry:
                    self._fail(cls, where, 429)
                SLEEP(min(60, 2 ** rate_hit))
                continue
            self._note(where, "api%s" % code, said, tok)
            if cls == "up" and code == 1 and INTERNAL_WORD in str(j.get("message", "")):
                net_fail += 1                           # same budget and backoff as a network failure
                if net_fail > len(NET_BACKOFF):
                    self._fail(cls, where, code)
                SLEEP(NET_BACKOFF[net_fail - 1])
                continue
            self._fail(cls, where, code)

    # ---- read ------------------------------------------------------------
    def list_all(self, dir_id, fresh=False):
        """Non-trashed children of a folder: [{name, type, id, size}]. Cached."""
        dir_id = int(dir_id)
        if not fresh and dir_id in self._lists:
            return self._lists[dir_id]
        out, last = [], 0
        while True:
            d = self._call("list", "GET", API + "/api/v2/file/list", "list",
                           params={"parentFileId": dir_id, "limit": 100, "lastFileId": last})
            for f in d.get("fileList") or []:
                if f.get("trashed", 0) == 0:
                    out.append({"name": f.get("filename"), "type": f.get("type"),
                                "id": int(f.get("fileId") or f.get("fileID")),
                                "size": int(f.get("size") or 0)})
            last = d.get("lastFileId", -1)
            if last in (-1, None) or not d.get("fileList"):
                break
        self._lists[dir_id] = out
        return out

    def child(self, dir_id, name, is_dir):
        for f in self.list_all(dir_id):
            if f["name"] == name and (f["type"] == 1) == is_dir:
                return f
        return None

    def walk(self, path, create=False):
        """Folder path -> folder id (None if missing and create=False)."""
        cur = 0
        for seg in [s for s in path.split("/") if s]:
            f = self.child(cur, seg, True)
            if f is None:
                if not create:
                    return None
                f = {"id": self.mkdir(cur, seg)}
            cur = f["id"]
        return cur

    def find_file(self, path):
        """File path -> (file id, size) or (None, 0)."""
        head, _, name = path.rstrip("/").rpartition("/")
        parent = self.walk(head)
        f = self.child(parent, name, False) if parent is not None else None
        return (f["id"], f["size"]) if f else (None, 0)

    def download_url(self, file_id):
        d = self._call("dl", "GET", API + "/api/v1/file/download_info", "download_info",
                       params={"fileId": int(file_id)})
        return d.get("downloadUrl")

    def user_info(self):
        return self._call("misc", "GET", API + "/api/v1/user/info", "user_info")

    # ---- write (never called by selftest against the real 123) ----------
    def mkdir(self, parent, name):
        with self._mk_locks[(parent, name)]:
            f = self.child(parent, name, True)
            if f:
                return f["id"]
            try:
                d = self._call("misc", "POST", API + "/upload/v1/file/mkdir", "mkdir",
                               json={"parentID": str(parent), "name": name})
                new_id = int(d["dirID"])
            except PanError:
                # created by someone else in between -> re-read instead of failing
                f = next((x for x in self.list_all(parent, fresh=True)
                          if x["name"] == name and x["type"] == 1), None)
                if not f:
                    raise
                new_id = f["id"]
            self._lists.setdefault(int(parent), []).append(
                {"name": name, "type": 1, "id": new_id, "size": 0})
            return new_id

    def _domain(self):
        with self._lock:
            if self._dom:
                return self._dom
        d = self._call("misc", "GET", API + "/upload/v2/file/domain", "domain")
        dom = d[0] if isinstance(d, list) else d
        with self._lock:
            self._dom = dom
        return dom

    def upload(self, dir_id, name, data):
        """Upload one small file in one step; overwrite a same-name file (duplicate=2)."""
        etag = hashlib.md5(data).hexdigest()
        self._call("up", "POST", self._domain() + "/upload/v2/file/single/create", "single",
                   files={"file": (name, data, "application/octet-stream")},
                   data={"parentFileID": str(dir_id), "filename": name, "etag": etag,
                         "size": str(len(data)), "duplicate": "2"}, timeout=300)
        self.stats["uploaded"] += 1
