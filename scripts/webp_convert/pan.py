# -*- coding: utf-8 -*-
"""Minimal 123pan open-platform client for the webp-convert pipeline.

- No login inside a shard. The caller hands in an access token taken from the
  shared token broker (run.py token). A 123 account keeps at most 3 live
  tokens and other jobs use them too, so shards must never log in themselves.
- Every call passes a per-class rate limiter: list / dl / up / misc.
  file/list has a hard 3 req/s per ACCOUNT limit shared by every consumer of
  that account, so the defaults stay well below it.
- 123 reports rate limiting as HTTP 200 + body code 429 (or a "too frequent"
  message), so the body code is always checked, never just the HTTP status.
"""
import collections
import hashlib
import threading
import time

import requests

API = "https://open-api.123pan.com"
# "\u9891\u7e41" = "too frequent" in the 123 error message
RATE_WORDS = ("\u9891\u7e41", "exceed", "limit", "tokens number")


class TokenInvalid(Exception):
    """401 / token rejected. Shards stop instead of logging in again."""


class PanError(Exception):
    def __init__(self, where, code):
        super().__init__("%s code=%s" % (where, code))
        self.code = code


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
    def __init__(self, token, qps, max_rate_retry=8):
        self.h = {"Platform": "open_platform", "Authorization": "Bearer " + token}
        self.lim = {k: Limiter(v) for k, v in qps.items()}
        self.s = requests.Session()
        self.max_rate_retry = max_rate_retry
        self.stats = collections.Counter()
        self._lists = {}
        self._dom = None
        self._lock = threading.Lock()
        self._mk_locks = collections.defaultdict(threading.Lock)

    # ---- transport -------------------------------------------------------
    def _call(self, cls, method, url, where, **kw):
        net_fail = 0
        rate_hit = 0
        timeout = kw.pop("timeout", 90)
        while True:
            self.lim[cls].wait()
            self.stats["call_" + cls] += 1
            try:
                j = self.s.request(method, url, headers=self.h, timeout=timeout, **kw).json()
            except Exception:                                   # noqa: BLE001
                net_fail += 1
                if net_fail >= 3:
                    raise PanError(where, "net")
                time.sleep(3 * net_fail)
                continue
            code = j.get("code")
            if code == 0:
                return j.get("data") or {}
            msg = str(j.get("message", "")).lower()
            # rate check first: one of 123's rate-limit messages says "tokens number"
            if code == 429 or any(w in msg for w in RATE_WORDS):
                rate_hit += 1
                self.stats["rate_limited"] += 1
                if rate_hit > self.max_rate_retry:
                    raise PanError(where, 429)
                time.sleep(min(60, 2 ** rate_hit))
                continue
            if code == 401:
                raise TokenInvalid(where)
            raise PanError(where, code)

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

    def upload(self, dir_id, name, data, try_reuse=True):
        """Upload one small file; overwrite a same-name file (duplicate=2).
        Returns "reuse" (instant upload, no bytes sent) or "up"."""
        etag = hashlib.md5(data).hexdigest()
        if try_reuse:
            try:
                d = self._call("up", "POST", API + "/upload/v2/file/create", "create",
                               json={"parentFileID": int(dir_id), "filename": name, "etag": etag,
                                     "size": len(data), "duplicate": 2, "containDir": False})
                if d.get("reuse"):
                    self.stats["reuse"] += 1
                    return "reuse"
            except PanError as e:
                if e.code == 429:
                    raise
                # create endpoint refused for another reason -> plain upload below
        self._call("up", "POST", self._domain() + "/upload/v2/file/single/create", "single",
                   files={"file": (name, data, "application/octet-stream")},
                   data={"parentFileID": str(dir_id), "filename": name, "etag": etag,
                         "size": str(len(data)), "duplicate": "2"}, timeout=300)
        self.stats["uploaded"] += 1
        return "up"
