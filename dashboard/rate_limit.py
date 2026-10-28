"""
Per-IP rate limiting for every /api route.

HOW IT WORKS
------------
Each client IP address gets a "sliding window": the limiter remembers the
time of each API request that IP made in the last WINDOW seconds. When a new
request arrives it first forgets anything older than the window, then:

  * fewer than LIMIT requests remembered -> the request goes through, and the
    response carries X-RateLimit-Limit / -Remaining / -Reset headers;
  * LIMIT or more                          -> the request is refused with
    HTTP 429 and a JSON body saying how long to wait. The route handler is
    never called, so a refused request costs almost nothing.

A sliding window (rather than "100 per clock hour") means nobody can send 100
requests at 10:59 and another 100 at 11:00.

Only paths under /api are counted. Pages (/wall, /overview ...) and /static
files are never limited, so the UI shell itself always loads and can show
the 429 message.

CONFIGURATION (.env)
--------------------
    RATE_LIMIT_ENABLED=true            # false switches the limiter off
    RATE_LIMIT_REQUESTS=3000           # requests allowed per window, per session (else per IP)
    RATE_LIMIT_WINDOW_SECONDS=600      # window length (600 = 10 minutes)

Counted per SIGNED-IN SESSION when the request carries one, else per IP.
Before: 100 per hour per IP. One open wall page polls about 18 times a
minute, so a single user was refused within minutes, and everyone behind the
office NAT or the reverse proxy shared those 100 (load test: first 429 0.7 s
after 20 people signed in).
    RATE_LIMIT_EXEMPT_PATHS=           # comma-separated; "/api/x*" = prefix
    RATE_LIMIT_TRUST_PROXY=false       # true only behind a reverse proxy

RATE_LIMIT_TRUST_PROXY: when the app sits behind nginx/IIS, every request
arrives from the proxy's IP, so all users would share one bucket. Setting
this to true reads the real client IP from X-Forwarded-For instead. Leave it
false when clients connect directly -- otherwise anyone can put a fake IP in
that header and get a fresh 100 requests every time.

STORAGE
-------
In memory, per process. A restart clears every counter. With several
uvicorn workers each worker counts separately (this app runs one).
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Iterable, Optional, Tuple


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


@dataclass(frozen=True)
class RateLimitSettings:
    enabled: bool = True
    requests: int = 3000
    window_seconds: int = 600
    exempt_paths: Tuple[str, ...] = ()
    trust_proxy: bool = False
    path_prefix: str = "/api"

    @classmethod
    def from_env(cls) -> "RateLimitSettings":
        exempt = tuple(p.strip() for p in
                       os.getenv("RATE_LIMIT_EXEMPT_PATHS", "").split(",")
                       if p.strip())
        requests = _env_int("RATE_LIMIT_REQUESTS", 3000)
        window = _env_int("RATE_LIMIT_WINDOW_SECONDS", 600)
        if requests == 100 and window == 3600:
            # The old shipped example values, copied into most .env files. They
            # refused a single wall page within minutes; use the new default.
            import logging
            logging.getLogger(__name__).warning(
                "RATE_LIMIT_REQUESTS=100 / RATE_LIMIT_WINDOW_SECONDS=3600 are the old example values "
                "and block normal use; using 3000 per 600 s per session instead. Remove those two "
                "lines from .env (or set other values) to silence this.")
            requests, window = 3000, 600
        return cls(
            enabled=_env_bool("RATE_LIMIT_ENABLED", True),
            requests=requests,
            window_seconds=window,
            exempt_paths=exempt,
            trust_proxy=_env_bool("RATE_LIMIT_TRUST_PROXY", False),
        )


# ---------------------------------------------------------------------------
# The counter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_after: int    # seconds until the oldest counted request expires
    retry_after: int    # seconds to wait before retrying (0 when allowed)


class SlidingWindowLimiter:
    """Thread-safe sliding-window request log, one deque per client key."""

    # Forget idle clients every N calls so the dict cannot grow without bound.
    _SWEEP_EVERY = 500

    def __init__(self, limit: int, window_seconds: float,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = int(limit)
        self.window = float(window_seconds)
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def hit(self, key: str) -> Decision:
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            self._calls += 1
            if self._calls % self._SWEEP_EVERY == 0:
                self._forget_idle(cutoff)

            q = self._hits.setdefault(key, deque())
            while q and q[0] <= cutoff:
                q.popleft()

            if len(q) >= self.limit:
                wait = max(1, math.ceil(q[0] + self.window - now))
                return Decision(False, self.limit, 0, wait, wait)

            q.append(now)
            reset = max(1, math.ceil(q[0] + self.window - now))
            return Decision(True, self.limit, self.limit - len(q), reset, 0)

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def _forget_idle(self, cutoff: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
            del self._hits[k]


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------

def _human_wait(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = math.ceil(seconds / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _human_window(seconds: int) -> str:
    if seconds % 3600 == 0:
        h = seconds // 3600
        return "hour" if h == 1 else f"{h} hours"
    if seconds % 60 == 0:
        m = seconds // 60
        return "minute" if m == 1 else f"{m} minutes"
    return f"{seconds} seconds"


def _path_matches(path: str, patterns: Iterable[str]) -> bool:
    for p in patterns:
        if p.endswith("*"):
            if path.startswith(p[:-1]):
                return True
        elif path == p or path == p.rstrip("/"):
            return True
    return False


class RateLimitMiddleware:
    """
    Pure ASGI middleware (no BaseHTTPMiddleware), so it adds no per-request
    task overhead and a refused request never reaches FastAPI routing.
    """

    def __init__(self, app, settings: Optional[RateLimitSettings] = None,
                 limiter: Optional[SlidingWindowLimiter] = None):
        self.app = app
        self.settings = settings or RateLimitSettings.from_env()
        self.limiter = limiter or SlidingWindowLimiter(
            self.settings.requests, self.settings.window_seconds)

    # -- helpers ------------------------------------------------------------

    def _is_limited_path(self, path: str) -> bool:
        prefix = self.settings.path_prefix
        if not (path == prefix or path.startswith(prefix + "/")):
            return False
        return not _path_matches(path, self.settings.exempt_paths)

    def _client_ip(self, scope) -> str:
        if self.settings.trust_proxy:
            for name, value in scope.get("headers") or ():
                if name == b"x-forwarded-for":
                    # The LAST entry is the one our own proxy appended; the
                    # first ones are client-supplied and could be forged for a
                    # fresh quota on every request.
                    last = value.decode("latin-1").split(",")[-1].strip()
                    if last:
                        return last
        client = scope.get("client")
        return client[0] if client else "unknown"

    @staticmethod
    def _session_key(scope) -> str:
        """A short hash of the session cookie, so each signed-in browser has its own quota."""
        import hashlib
        try:
            from core.auth import COOKIE
        except Exception:  # noqa: BLE001
            COOKIE = "ibo_session"
        for name, value in scope.get("headers") or ():
            if name != b"cookie":
                continue
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE and v:
                    return "s:" + hashlib.sha256(v.encode()).hexdigest()[:16]
        return ""

    @staticmethod
    def _headers(d: Decision) -> list:
        return [
            (b"x-ratelimit-limit", str(d.limit).encode()),
            (b"x-ratelimit-remaining", str(d.remaining).encode()),
            (b"x-ratelimit-reset", str(d.reset_after).encode()),
        ]

    # -- ASGI entry point ---------------------------------------------------

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or not self.settings.enabled
                or not self._is_limited_path(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return

        ip = self._client_ip(scope)
        session = self._session_key(scope)
        decision = self.limiter.hit(session or ip)

        if not decision.allowed:
            body = json.dumps({
                "error": "rate_limit_exceeded",
                "detail": (
                    f"Too many requests. {'This session' if session else f'This IP address ({ip})'} has used all "
                    f"{decision.limit} API requests allowed per "
                    f"{_human_window(self.settings.window_seconds)}. "
                    f"Try again in {_human_wait(decision.retry_after)}."),
                "limit": decision.limit,
                "window_seconds": self.settings.window_seconds,
                "retry_after_seconds": decision.retry_after,
            }).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(decision.retry_after).encode()),
                ] + self._headers(decision),
            })
            await send({"type": "http.response.body", "body": body})
            return

        extra = self._headers(decision)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers") or []) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)
