"""Fixes from the multi-user simulation (Oct 2026)."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from test_fix_monitoring_log import env  # noqa: F401  (PS4=Metro, CEQ=Centor)

BROWSER = {"Sec-Fetch-Site": "same-origin"}


def _login(app_module, user, pw):
    c = TestClient(app_module.app)
    assert c.post("/login", data={"username": user, "password": pw, "next": "/"}, headers=BROWSER,
                  follow_redirects=False).status_code == 303
    c.headers["X-CSRF-Token"] = c.get("/api/me").json()["csrf_token"]
    return c


def test_password_change_ends_existing_sessions(env, monkeypatch):  # noqa: F811
    app_module, auth, _ops = env
    monkeypatch.setenv("IBO_AUTH", "1")
    auth.add_user("ada", "admin-password!", "admin")
    c = _login(app_module, "ada", "admin-password!")
    assert c.get("/api/me").status_code == 200
    auth.set_password("ada", "new-password-123")
    assert c.get("/api/me").json().get("authenticated") is not True


def test_office_colleagues_are_not_locked_out_by_one_persons_typos(env):  # noqa: F811
    _app, auth, _ops = env
    auth._failures.clear()
    for u in ("a", "b", "c", "d", "e"):
        for _ in range(4):
            auth._note_failure(u, "10.0.0.1")
    assert not auth.login_locked("someone_else", "10.0.0.1")      # 20 failures from the NAT: still fine
    assert auth.login_locked("a", "10.0.0.1") is False            # 4 < 5 per user
    auth._note_failure("a", "10.0.0.1")
    assert auth.login_locked("a", "10.0.0.1")                     # per-user lock still works


def test_simultaneous_monitor_clicks_one_started_rest_409(env, monkeypatch):  # noqa: F811
    app_module, _auth, _ops = env
    gate = threading.Event()

    def fake_run(name, **kw):
        app_module._run_state["running"] = True
        app_module._run_state["current_system"] = name
        gate.wait(5)
        app_module._run_state["running"] = False

    monkeypatch.setattr(app_module, "_run_one_system_background", fake_run)
    monkeypatch.setattr(app_module, "_begin_manual_hold", lambda: None)
    app_module._release_manual_claim()
    codes = []

    def click():
        try:
            app_module.run_now_single("PS4")
            codes.append(200)
        except app_module.HTTPException as exc:
            codes.append(exc.status_code)

    ts = [threading.Thread(target=click) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    gate.set()
    time.sleep(0.2)
    assert sorted(codes) == [200, 409, 409, 409, 409, 409]


def test_scoped_operator_cannot_pause_snooze_or_stop_other_customers(env, monkeypatch):  # noqa: F811
    app_module, auth, _ops = env
    monkeypatch.setenv("IBO_AUTH", "1")
    auth.add_user("mop", "metro-operator-1", "operator", "Metro")
    c = _login(app_module, "mop", "metro-operator-1")
    assert c.post("/api/scheduler/pause").status_code == 403
    assert c.post("/api/scheduler/snooze?minutes=30").status_code == 403
    app_module._run_state.update(running=True, current_system="CEQ")
    try:
        r = c.post("/api/run-stop")
        assert r.status_code == 403 and "outside your customers" in r.json()["detail"]
    finally:
        app_module._run_state.update(running=False, current_system=None, cancel_requested=False,
                                     stop_requested=False)


def test_run_history_is_scoped_and_hides_other_customers_names(env, monkeypatch):  # noqa: F811
    app_module, auth, _ops = env
    monkeypatch.setenv("IBO_AUTH", "1")
    from core import run_history
    monkeypatch.setattr(run_history, "query", lambda **k: [
        {"system": "PS4", "result": "skipped", "failure_reason": "CEQ was still running"},
        {"system": "CEQ", "result": "success", "failure_reason": ""}])
    auth.add_user("metro", "metro-password-1", "viewer", "Metro")
    c = _login(app_module, "metro", "metro-password-1")
    rows = c.get("/api/run-history").json()
    assert [r["system"] for r in rows] == ["PS4"]
    assert "CEQ" not in rows[0]["failure_reason"]
    assert "CEQ" not in c.get("/api/run-history.csv").text


def test_config_lock_serialises_writers():
    from core import config_lock
    inside, peak = [0], [0]
    lock = threading.Lock()

    @config_lock.locked
    def write():
        with lock:
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
        time.sleep(0.01)
        with config_lock.held():        # re-entrant (a system save calls the .env writer)
            pass
        with lock:
            inside[0] -= 1

    ts = [threading.Thread(target=write) for _ in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert peak[0] == 1


def test_rate_limit_counts_per_session_with_a_sane_default(monkeypatch):
    from dashboard import rate_limit as R
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "100")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "3600")
    s = R.RateLimitSettings.from_env()
    assert (s.requests, s.window_seconds) == (3000, 600)            # old example values upgraded
    from core.auth import COOKIE
    a = R.RateLimitMiddleware._session_key({"headers": [(b"cookie", f"x=1; {COOKIE}=tokA".encode())]})
    b = R.RateLimitMiddleware._session_key({"headers": [(b"cookie", f"{COOKIE}=tokB".encode())]})
    assert a and b and a != b
    assert R.RateLimitMiddleware._session_key({"headers": []}) == ""
