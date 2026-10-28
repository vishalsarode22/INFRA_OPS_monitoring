"""
Review fix 2: logon blocks know WHY. A locked user gets one shared retry
every IBO_LOGON_LOCKED_RETRY_HOURS; a wrong password stays blocked; the
dashboard lists and clears blocks; the card message says how to clear.
"""
import importlib
from datetime import datetime, timedelta

import pytest

from collectors import rfc_collector as rc
from collectors import rfc_logon_guard as guard


class LogonError(Exception):
    key = "RFC_LOGON_FAILURE"


def _cfg(password="pw"):
    return {"rfc": {"ashost": "sap-prd.example", "sysnr": "00", "client": "100",
                    "username": "MONITOR", "password": password}}


@pytest.fixture
def sap(monkeypatch):
    state = {"answer": LogonError("Password logon no longer possible - too many failed attempts"),
             "attempts": 0}

    class Conn:
        def __init__(self, config=None, **params):
            state["attempts"] += 1
            if state["answer"] is not None:
                raise state["answer"]

        def call(self, name, **kw):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(rc, "Connection", Conn)
    monkeypatch.setattr(rc, "PYRFC_AVAILABLE", True)
    monkeypatch.setattr(rc, "_tcp_reachable", lambda *a, **k: (True, ""))
    rc.clear_cooldown()
    return state


def _open():
    with rc.SapSession("PRD", _cfg()) as s:
        return s.ok, s.error, s.logon_blocked


def _age_retry(hours=7):
    """Pretend the retry time has passed."""
    con = guard._connect()
    past = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    con.execute("UPDATE blocks SET next_retry_at = ?", (past,))
    con.commit()
    con.close()


@pytest.mark.parametrize("text,reason", [
    ("Name or password is incorrect (repeat logon)", "invalid_password"),
    ("User is locked. Please notify the person responsible", "locked"),
    ("Password logon no longer possible - too many failed attempts", "locked"),
    ("Password has expired; choose a new one", "expired"),
    ("Password must be changed", "expired"),
    ("[28000] Login failed.", "invalid_password"),
])
def test_classify(text, reason):
    assert guard.classify_reason(text) == reason


def test_locked_user_blocks_with_retry_time_and_clear_hint(sap):
    ok, err, blocked = _open()
    assert not ok and blocked
    assert "locked" in err and "--clear PRD" in err and "dashboard" in err
    b = guard.list_blocks("PRD")[0]
    assert b["reason"] == "locked" and b["next_retry_at"]
    # Before the retry time: no further attempt.
    _open()
    assert sap["attempts"] == 1


def test_locked_user_is_retried_once_after_the_interval_and_success_clears(sap):
    _open()
    _age_retry()
    sap["answer"] = None                          # admin unlocked in SU01
    ok, err, blocked = _open()
    assert ok and not blocked and sap["attempts"] == 2
    assert guard.list_blocks() == []


def test_locked_retry_claimed_by_only_one_process(sap):
    _open()
    _age_retry()
    target = guard.LogonTarget.from_rfc_params("PRD", rc.build_rfc_params(_cfg()))
    assert guard._claim_retry(target) is True
    assert guard._claim_retry(target) is False    # the next process loses the claim
    _open()
    assert sap["attempts"] == 1                   # retry slot already spent elsewhere


def test_locked_retry_refused_as_wrong_password_becomes_permanent(sap):
    _open()
    _age_retry()
    sap["answer"] = LogonError("Name or password is incorrect (repeat logon)")
    ok, err, blocked = _open()
    assert blocked and sap["attempts"] == 2
    b = guard.list_blocks("PRD")[0]
    assert b["reason"] == "invalid_password" and b["next_retry_at"] is None
    _age_retry(100)                                # even long after: never retried
    _open()
    assert sap["attempts"] == 2


def test_locked_retry_still_locked_waits_for_next_slot(sap, monkeypatch):
    monkeypatch.setenv("IBO_LOGON_LOCKED_RETRY_HOURS", "6")
    _open()
    _age_retry()
    _open()
    assert sap["attempts"] == 2
    b = guard.list_blocks("PRD")[0]
    assert b["reason"] == "locked"
    assert datetime.fromisoformat(b["next_retry_at"]) > datetime.now() + timedelta(hours=5)
    _open()
    assert sap["attempts"] == 2


def test_expired_password_stays_blocked(sap):
    sap["answer"] = LogonError("Password has expired. Change your password")
    ok, err, blocked = _open()
    assert blocked and "expired" in err
    _age_retry(100)
    _open()
    assert sap["attempts"] == 1


def test_old_rows_without_reason_are_classified(sap):
    sap["answer"] = LogonError("Name or password is incorrect")
    _open()
    con = guard._connect()
    con.execute("UPDATE blocks SET reason = NULL")
    con.commit()
    con.close()
    assert guard.list_blocks()[0]["reason"] == "invalid_password"


# ------------------------------------------------------------------ dashboard

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def dash(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_DB_PATH", str(tmp_path / "auth.sqlite"))
    monkeypatch.setenv("IBO_AUTH", "1")
    from core import store, auth
    importlib.reload(store)
    auth._failures.clear()
    auth._secret_cache.clear()
    import dashboard.app as app_module
    monkeypatch.setattr(app_module, "get_systems", lambda: [{"name": "PRD", "client": "100"}])
    auth.add_user("vic", "viewer-password", "viewer")
    auth.add_user("otto", "operator-password", "operator")
    auth.add_user("ada", "admin-password-1", "admin")
    yield app_module
    monkeypatch.undo()
    importlib.reload(store)


def _login(app_module, user, pw):
    c = TestClient(app_module.app)
    c.post("/login", data={"username": user, "password": pw, "next": "/"},
           headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    return c, {"X-CSRF-Token": c.get("/api/me").json()["csrf_token"]}


def test_required_role_for_new_routes():
    import dashboard.app as app_module
    assert app_module._required_role("/api/logon-blocks", "GET") == "viewer"
    # Admin only since the security review: a clear with an unchanged wrong
    # password costs one failed SAP logon; repeated, it locks the SAP user.
    assert app_module._required_role("/api/logon-blocks/PRD/clear", "POST") == "admin"


def test_dashboard_lists_and_admin_clears(sap, dash):
    _open()
    viewer, vh = _login(dash, "vic", "viewer-password")
    r = viewer.get("/api/logon-blocks")
    assert r.status_code == 200
    blocks = r.json()["blocks"]
    assert blocks[0]["system"] == "PRD" and blocks[0]["reason"] == "locked"
    assert blocks[0]["since"] and blocks[0]["next_retry_at"]
    assert viewer.post("/api/logon-blocks/PRD/clear", headers=vh).status_code == 403
    assert guard.list_blocks("PRD")

    op, oh = _login(dash, "otto", "operator-password")
    assert op.post("/api/logon-blocks/PRD/clear", headers=oh).status_code == 403   # operator: no
    assert guard.list_blocks("PRD")
    ad, ah = _login(dash, "ada", "admin-password-1")
    assert ad.post("/api/logon-blocks/PRD/clear").status_code in (401, 403)   # no CSRF
    import dashboard.app as app_module
    app_module._logon_clears.clear()
    r = ad.post("/api/logon-blocks/PRD/clear", headers=ah)
    assert r.status_code == 200 and r.json()["cleared"] == 1
    assert guard.list_blocks("PRD") == []
    # A second clear within the cool-down is refused with the reason.
    r = ad.post("/api/logon-blocks/PRD/clear", headers=ah)
    assert r.status_code == 429 and "fix the password" in r.json()["detail"]
    assert ad.post("/api/logon-blocks/NOPE/clear", headers=ah).status_code in (403, 404)


def test_wall_card_has_clear_button():
    import os
    html = open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "dashboard", "static", "wall.html"), encoding="utf-8").read()
    assert "clearLogonBlock(" in html and "/api/logon-blocks/" in html
    assert 'IB.post("/api/logon-blocks/' in html
