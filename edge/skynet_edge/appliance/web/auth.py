"""Device password, sessions and login rate limiting for the setup page.

* The password is stored as a salted scrypt hash (``hashlib.scrypt``,
  N=2^14, r=8, p=1: 16 MiB and ~50 ms per check on an Orin's CPU) in
  ``/var/lib/skynet/setup/auth.json`` (0600). PBKDF2-SHA256 (600 000
  iterations) is used if this Python's OpenSSL lacks scrypt.
* Sessions are random 256-bit tokens kept in memory only (a restart of the
  setup service logs everyone out), sent as an ``HttpOnly; SameSite=Strict``
  cookie, valid for 12 hours of inactivity. Changing the password ends all
  sessions.
* Failed logins: at most 10 per client address and 30 in total per
  15 minutes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from ..util import read_json, write_json

MIN_PASSWORD = 8
MAX_PASSWORD = 256
SESSION_TTL_S = 12 * 3600.0
_SCRYPT = {"n": 1 << 14, "r": 8, "p": 1}
_PBKDF2_ITER = 600_000


def hash_password(password: str, salt: Optional[bytes] = None) -> Dict[str, Any]:
    salt = salt or secrets.token_bytes(16)
    pw = password.encode("utf-8")
    try:
        digest = hashlib.scrypt(pw, salt=salt, n=_SCRYPT["n"], r=_SCRYPT["r"], p=_SCRYPT["p"], dklen=32,
                                maxmem=64 * 1024 * 1024)
        return {"algo": "scrypt", **_SCRYPT, "salt": salt.hex(), "hash": digest.hex()}
    except (AttributeError, ValueError):
        digest = hashlib.pbkdf2_hmac("sha256", pw, salt, _PBKDF2_ITER, 32)
        return {"algo": "pbkdf2-sha256", "iterations": _PBKDF2_ITER, "salt": salt.hex(), "hash": digest.hex()}


def verify_password(password: str, rec: Dict[str, Any]) -> bool:
    try:
        salt = bytes.fromhex(rec["salt"])
        want = bytes.fromhex(rec["hash"])
        pw = password.encode("utf-8")
        if rec.get("algo") == "scrypt":
            got = hashlib.scrypt(pw, salt=salt, n=int(rec["n"]), r=int(rec["r"]), p=int(rec["p"]), dklen=len(want),
                                 maxmem=64 * 1024 * 1024)
        elif rec.get("algo") == "pbkdf2-sha256":
            got = hashlib.pbkdf2_hmac("sha256", pw, salt, int(rec["iterations"]), len(want))
        else:
            return False
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def check_new_password(password: Any) -> str:
    if not isinstance(password, str):
        raise ValueError("password must be text")
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"use at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise ValueError("password is too long")
    if any(c in password for c in "\n\r\x00:"):
        raise ValueError("password may not contain line breaks or ':'")
    return password


class PasswordStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def is_set(self) -> bool:
        rec = read_json(self.path)
        return isinstance(rec, dict) and "hash" in rec

    def set(self, password: str) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
            write_json(self.path, hash_password(check_new_password(password)), mode=0o600)

    def verify(self, password: str) -> bool:
        rec = read_json(self.path)
        return isinstance(rec, dict) and isinstance(password, str) and verify_password(password, rec)


class Sessions:
    def __init__(self, ttl_s: float = SESSION_TTL_S, clock=time.monotonic):
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._by_hash: Dict[str, float] = {}

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._expire()
            self._by_hash[self._h(token)] = self._clock() + self.ttl_s
        return token

    def valid(self, token: Optional[str]) -> bool:
        if not token or len(token) > 100:
            return False
        h = self._h(token)
        now = self._clock()
        with self._lock:
            exp = self._by_hash.get(h)
            if exp is None or exp < now:
                self._by_hash.pop(h, None)
                return False
            self._by_hash[h] = now + self.ttl_s  # sliding expiry
            return True

    def revoke(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._by_hash.pop(self._h(token), None)

    def revoke_all(self) -> None:
        with self._lock:
            self._by_hash.clear()

    def _expire(self) -> None:
        now = self._clock()
        for h in [h for h, exp in self._by_hash.items() if exp < now]:
            del self._by_hash[h]


class RateLimiter:
    """Sliding-window counter per key plus a global cap."""

    def __init__(self, per_key: int, total: int, window_s: float, clock=time.monotonic):
        self.per_key = per_key
        self.total = total
        self.window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._events: Dict[str, Deque[float]] = {}
        self._all: Deque[float] = deque()

    def _prune(self, dq: Deque[float], now: float) -> None:
        while dq and dq[0] <= now - self.window_s:
            dq.popleft()

    def blocked(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            dq = self._events.get(key, deque())
            self._prune(dq, now)
            self._prune(self._all, now)
            return len(dq) >= self.per_key or len(self._all) >= self.total

    def record(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            self._events.setdefault(key, deque()).append(now)
            self._all.append(now)
            if len(self._events) > 10_000:
                self._events.clear()
