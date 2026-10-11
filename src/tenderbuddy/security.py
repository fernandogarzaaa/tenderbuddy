"""Password hashing, JWT access tokens, CSRF tokens and login rate limiting."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

ALGORITHM = "HS256"
MIN_PASSWORD_LENGTH = 10
_BCRYPT_MAX_BYTES = 72


class PasswordPolicyError(ValueError):
    pass


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password.encode()) > _BCRYPT_MAX_BYTES:
        raise PasswordPolicyError("Password must be at most 72 bytes")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:_BCRYPT_MAX_BYTES], hashed.encode())
    except ValueError:
        return False


def create_access_token(subject: str, secret: str, expires_minutes: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": subject, "iat": now, "exp": now + timedelta(minutes=expires_minutes), "type": "access"}
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, secret: str) -> str | None:
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) and sub else None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_value: str | None, submitted: str | None) -> bool:
    if not cookie_value or not submitted:
        return False
    return hmac.compare_digest(cookie_value, submitted)


class RateLimiter:
    """In-memory sliding-window limiter keyed by e.g. client IP + email.

    Per-process: with several workers each keeps its own window, which still
    bounds brute-force attempts to ``limit * workers`` per window.
    """

    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        return q

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, self._clock())) >= self.limit

    def hit(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
