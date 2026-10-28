"""
Rate limiter tests. Uses a tiny Starlette app so nothing here touches the
real dashboard, its scheduler, or any SAP system.
"""

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from dashboard.rate_limit import (
    RateLimitMiddleware, RateLimitSettings, SlidingWindowLimiter)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _ok(request):
    return JSONResponse({"ok": True})


def _client(settings, clock=None):
    app = Starlette(routes=[
        Route("/api/things", _ok),
        Route("/api/scheduler", _ok),
        Route("/wall", _ok),
    ])
    limiter = SlidingWindowLimiter(settings.requests, settings.window_seconds,
                                   clock=clock or FakeClock())
    app.add_middleware(RateLimitMiddleware, settings=settings, limiter=limiter)
    return TestClient(app), limiter


def test_allows_up_to_limit_then_429_with_clear_message():
    c, _ = _client(RateLimitSettings(requests=3, window_seconds=3600))
    for expected_remaining in (2, 1, 0):
        r = c.get("/api/things")
        assert r.status_code == 200
        assert r.headers["x-ratelimit-limit"] == "3"
        assert r.headers["x-ratelimit-remaining"] == str(expected_remaining)

    r = c.get("/api/things")
    assert r.status_code == 429
    body = r.json()
    assert body["error"] == "rate_limit_exceeded"
    assert "3 API requests allowed per hour" in body["detail"]
    assert "Try again in" in body["detail"]
    assert int(r.headers["retry-after"]) == body["retry_after_seconds"] > 0


def test_limit_is_shared_across_all_api_routes():
    c, _ = _client(RateLimitSettings(requests=2, window_seconds=60))
    assert c.get("/api/things").status_code == 200
    assert c.get("/api/scheduler").status_code == 200
    assert c.get("/api/things").status_code == 429


def test_pages_are_never_limited():
    c, _ = _client(RateLimitSettings(requests=1, window_seconds=60))
    c.get("/api/things")
    for _ in range(5):
        assert c.get("/wall").status_code == 200


def test_window_slides():
    clock = FakeClock()
    c, _ = _client(RateLimitSettings(requests=2, window_seconds=60), clock)
    c.get("/api/things")
    clock.t += 30
    c.get("/api/things")
    assert c.get("/api/things").status_code == 429
    clock.t += 31          # first request is now older than 60 s
    assert c.get("/api/things").status_code == 200
    assert c.get("/api/things").status_code == 429


def test_exempt_paths_exact_and_prefix():
    c, _ = _client(RateLimitSettings(
        requests=1, window_seconds=60, exempt_paths=("/api/scheduler", "/api/thi*")))
    for _ in range(5):
        assert c.get("/api/scheduler").status_code == 200
        assert c.get("/api/things").status_code == 200


def test_disabled_limiter_passes_everything():
    c, _ = _client(RateLimitSettings(enabled=False, requests=1, window_seconds=60))
    for _ in range(5):
        assert c.get("/api/things").status_code == 200


def test_forwarded_for_ignored_unless_trusted():
    c, _ = _client(RateLimitSettings(requests=1, window_seconds=60))
    c.get("/api/things", headers={"X-Forwarded-For": "1.1.1.1"})
    # Spoofing a new IP must not buy a fresh allowance.
    r = c.get("/api/things", headers={"X-Forwarded-For": "2.2.2.2"})
    assert r.status_code == 429


def test_forwarded_for_used_when_trusted():
    c, _ = _client(RateLimitSettings(requests=1, window_seconds=60, trust_proxy=True))
    assert c.get("/api/things", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert c.get("/api/things", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
    assert c.get("/api/things", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "250")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "600")
    monkeypatch.setenv("RATE_LIMIT_EXEMPT_PATHS", "/api/scheduler, /api/live*")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    s = RateLimitSettings.from_env()
    assert (s.requests, s.window_seconds, s.enabled) == (250, 600, False)
    assert s.exempt_paths == ("/api/scheduler", "/api/live*")


@pytest.mark.parametrize("bad", ["", "abc", "-5"])
def test_bad_env_values_fall_back_safely(monkeypatch, bad):
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", bad)
    assert RateLimitSettings.from_env().requests >= 1
