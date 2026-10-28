"""
End-to-end flows that the unit tests only cover piece by piece (each of
those mocks the neighbouring module). Here the real modules are chained:

  1. login -> role-scoped API access for every role, on a system inside and
     a system outside the user's project (middleware + core/access +
     core/projects + handlers);
  2. a stale run marker from a dead dashboard -> recovery -> the scheduler
     pass starts the interrupted profile slot exactly once;
  3. an incomplete report -> held -> retried by the scheduler -> held again
     -> last try sent with the partial note (main + core/report_gate +
     dashboard scheduler, one SQLite store);
  4. an owner mail requested -> Basis gets an approval mail with a signed
     link -> GET shows a confirm page only -> POST sends once -> a second
     click is refused; a mail-server failure leaves the link usable.

Only the edges are faked: the SAP sweep itself, the mail transport and the
clock (report-gate retries are 15 min apart).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from test_fix_monitoring_log import SYSTEMS, env  # noqa: E402,F401  (PS4 = Metro, CEQ = Centor)

BROWSER = {"Sec-Fetch-Site": "same-origin"}


# ----------------------------------------------------------------- helpers

@pytest.fixture
def app_state(env):  # noqa: F811
    """Snapshot / restore the dashboard's module-level scheduler state."""
    app_module, _auth, _ops = env
    saved = {
        "_scheduler_state": dict(app_module._scheduler_state),
        "_run_state": dict(app_module._run_state),
        "_retry_once": dict(app_module._retry_once),
        "_catch_up": set(app_module._catch_up),
        "_stamps_before": dict(app_module._stamps_before),
        "_stop_hold": dict(app_module._stop_hold),
    }
    yield env
    for name, value in saved.items():
        obj = getattr(app_module, name)
        obj.clear()
        obj.update(value)


@pytest.fixture
def team(app_state, monkeypatch):
    app_module, auth, ops_db = app_state
    monkeypatch.setenv("IBO_AUTH", "1")
    from core import accounts, config_loader, projects
    started = []
    monkeypatch.setattr(app_module, "_run_one_system_background",
                        lambda s, **k: started.append(s) or True)
    monkeypatch.setattr(app_module, "_begin_manual_hold", lambda: None)
    # ops_api._visible_systems reads config_loader.get_systems (the real
    # config/systems.yaml otherwise): scope it like the app does.
    monkeypatch.setattr(config_loader, "get_systems",
                        lambda *a, **k: auth.filter_systems([dict(s) for s in SYSTEMS]))
    app_module._release_manual_claim()
    projects._denied.clear()
    accounts.ensure_schema()
    users = {
        "admin": ("boss", "admin-password-1", "admin", "*"),
        "manager": ("mia", "manager-pass-1", "manager", "Metro"),
        "team_lead": ("tl", "teamlead-pass-1", "team_lead", "Metro"),
        "consultant": ("con", "consultant-pw-1", "consultant", "Metro"),
        "granted": ("con2", "consultant-pw-2", "consultant", "Metro"),
        "viewer": ("vic", "viewer-pass-12", "viewer", "Metro"),
    }
    for name, pw, role, cust in users.values():
        auth.add_user(name, pw, role, cust)
    # A team leader lets con2 run and handle alerts on PS4 (not settings).
    projects.set_grant({"role": "admin", "username": "boss", "customers": None}, "con2", "Metro",
                       ["PS4"], run=True, alerts=True, settings=False)
    return app_module, users, started


def _login(app_module, user, pw):
    c = TestClient(app_module.app)
    r = c.post("/login", data={"username": user, "password": pw, "next": "/"}, headers=BROWSER,
               follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    c.headers.update(BROWSER)
    c.headers["X-CSRF-Token"] = c.get("/api/me").json()["csrf_token"]
    return c


def _probe(c, system):
    """Status code of one request per permission category on `system`."""
    out = {}
    out["read"] = c.get(f"/api/timeline?system={system}").status_code
    out["run"] = c.post(f"/api/run-now/{system}").status_code
    out["alerts"] = c.post("/api/ops/silence", json={"system": system, "mode": "snooze", "minutes": 30,
                                                     "reason": "integration test"}).status_code
    out["settings"] = c.post("/api/ops/kb", json={"system": system, "title": "t", "fix": "f",
                                                  "rule": ""}).status_code
    return out


OK = "ok"


def _norm(codes: dict) -> dict:
    """Allowed = the request reached its handler and succeeded (2xx; 409 = run already claimed)."""
    return {k: (OK if (200 <= v < 300 or (k == "run" and v == 409)) else v) for k, v in codes.items()}


# ------------------------------------------------- 1. role-scoped access

ALL_OK = {"read": OK, "run": OK, "alerts": OK, "settings": OK}
READ_ONLY = {"read": OK, "run": 403, "alerts": 403, "settings": 403}
INVISIBLE = {"read": 404, "run": 404, "alerts": 404, "settings": 404}

MATRIX = [
    # role,        PS4 (own project),                                 CEQ (other project)
    ("admin",      ALL_OK,                                            ALL_OK),
    ("manager",    ALL_OK,                                            INVISIBLE),
    ("team_lead",  ALL_OK,                                            INVISIBLE),
    ("consultant", READ_ONLY,                                         INVISIBLE),
    ("granted",    {"read": OK, "run": OK, "alerts": OK, "settings": 403}, INVISIBLE),
    ("viewer",     READ_ONLY,                                         INVISIBLE),
]


@pytest.mark.parametrize("role,inside,outside", MATRIX, ids=[m[0] for m in MATRIX])
def test_login_then_role_scoped_access_inside_and_outside_the_project(team, role, inside, outside):
    app_module, users, started = team
    name, pw, _r, _c = users[role]
    c = _login(app_module, name, pw)
    me = c.get("/api/me").json()
    assert me["username"] == name
    got_in = _norm(_probe(c, "PS4"))
    app_module._release_manual_claim()
    got_out = _norm(_probe(c, "CEQ"))
    assert got_in == inside, f"{role} on PS4"
    assert got_out == outside, f"{role} on CEQ"
    # a refused run never reached the sweep
    assert set(started) <= {s for s, exp in (("PS4", inside), ("CEQ", outside)) if exp["run"] == OK}


def test_admin_only_and_global_writes_are_refused_below_admin(team):
    app_module, users, _started = team
    codes = {}
    for role in ("admin", "manager", "team_lead", "granted", "viewer"):
        name, pw, _r, _c = users[role]
        c = _login(app_module, name, pw)
        codes[role] = (c.get("/api/access/audit").status_code,                       # admin read
                       c.post("/api/scheduler/pause").status_code,                   # every system
                       c.post("/api/ops/maintenance", json={"system": "*", "minutes": 30,
                                                            "reason": "all"}).status_code)
        if codes[role][1] == 200:
            assert c.post("/api/scheduler/resume").status_code == 200
    assert codes["admin"] == (200, 200, 200)
    # A manager limited to Metro passes core/access (category "manager") but the
    # handler refuses (_require_unscoped): pausing / a window for every system
    # covers other customers.
    for role in ("manager", "team_lead", "granted", "viewer"):
        assert codes[role] == (403, 403, 403), role


def test_refused_writes_are_audited_and_session_ends_on_grant_change(team):
    app_module, users, _started = team
    from core import projects
    c = _login(app_module, "con", "consultant-pw-1")
    assert c.post("/api/run-now/PS4").status_code == 403
    rows = projects.audit_rows(actor="con", action="denied")
    assert rows and "POST /api/run-now/PS4" in rows[0]["target"]
    # granting run access drops the old session: the next call needs a new login
    projects.set_grant({"role": "admin", "username": "boss", "customers": None}, "con", "Metro",
                       "*", run=True, alerts=False, settings=False)
    assert c.post("/api/run-now/PS4").status_code == 401
    c = _login(app_module, "con", "consultant-pw-1")
    assert c.post("/api/run-now/PS4").status_code in (200, 409)


# ------------------------------------- 2. scheduler + stale run marker

@pytest.fixture
def sched(app_state, tmp_path, monkeypatch):
    app_module, _auth, _ops = app_state
    from core import profiles, schedules as sch
    monkeypatch.setenv("IBO_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(sch, "_PATH", str(tmp_path / "schedules.json"))
    monkeypatch.setattr(sch, "STATE_PATH", str(tmp_path / "schedule_state.json"))
    sch._missed_reported.clear()
    sch._last_good.clear()
    # Slot maths on a fixed clock (5 min after the hour): no flake when a test
    # straddles a full hour. Code outside core/schedules keeps the real clock.
    fixed = datetime.now().replace(minute=5, second=0, microsecond=0)
    monkeypatch.setattr(sch, "_now", lambda now=None: sch.to_local_naive(now) if now is not None else fixed)
    monkeypatch.setattr(profiles, "STATE_PATH", str(tmp_path / "profile_state.json"))
    profiles._last_good.clear()
    monkeypatch.setattr(profiles, "resolve_tcodes", lambda p: ["SM51"])
    cfg = sch._normalise({"enabled": True, "mode": "hours", "every": 1, "at": "00:00",
                          "days": list(sch.WEEKDAYS)})
    prof = {"id": "ps4-hourly", "system": "PS4", "deliver": ["email"], "label": "hourly", "schedule": cfg}
    monkeypatch.setattr(profiles, "load_profiles", lambda: [dict(prof)])
    started = []
    monkeypatch.setattr(app_module, "_run_one_system_background",
                        lambda s, **k: started.append((s, k.get("profile_id"), k.get("planned_at"))) or True)
    app_module._retry_once.clear()
    app_module._catch_up.clear()
    app_module._stop_hold.clear()
    app_module._scheduler_state.update(enabled=True, snooze_until=None, manual_pause=False,
                                       manual_since=None, busy_since=None)
    app_module._run_state["running"] = False
    return app_module, profiles, sch, cfg, started


def test_interrupted_slot_from_a_dead_dashboard_runs_exactly_once(sched):
    app, profiles, sch, cfg, started = sched
    from core import run_history
    slot = sch.previous_slot(cfg)
    profiles.mark_run("ps4-hourly", slot - timedelta(hours=1))      # last hour's run was done
    # The previous dashboard process picked this slot, stamped it and died mid-sweep:
    before = list(profiles.stamps("ps4-hourly"))
    profiles.mark_run("ps4-hourly", slot)
    app._stamps_before["ps4-hourly"] = before
    app._active_run_mark("PS4", "ps4-hourly", "schedule", slot)
    marker = os.path.join(os.environ["IBO_RUNTIME_DIR"], "active_run.json")
    data = json.load(open(marker, encoding="utf-8"))
    data["pid"] = 999_999                                          # that process is gone
    json.dump(data, open(marker, "w", encoding="utf-8"))
    assert profiles.due_profiles() == []                           # stamped: nothing due

    app._recover_interrupted_run()                                 # the new process starts
    assert not os.path.exists(marker)
    failed = [r for r in run_history.query(system="PS4") if r["result"] == "failed"]
    assert failed and "dashboard stopped during this run" in failed[0]["failure_reason"]

    for _ in range(4):                                             # four 20-second passes
        app._scheduler_pass()
    assert started == [("PS4", "ps4-hourly", slot)]
    assert app._scheduler_state.get("loop_error") is None
    # A second start (another restart with no marker) does not replay it either.
    app._recover_interrupted_run()
    app._scheduler_pass()
    assert len(started) == 1


def test_stale_marker_without_saved_stamps_does_not_rerun_the_slot(sched):
    """An older marker format (no stamps_before): recorded as failed, slot not replayed."""
    app, profiles, sch, cfg, started = sched
    slot = sch.previous_slot(cfg)
    profiles.mark_run("ps4-hourly", slot)
    marker = os.path.join(os.environ["IBO_RUNTIME_DIR"], "active_run.json")
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    json.dump({"system": "PS4", "profile_id": "ps4-hourly", "trigger": "schedule",
               "planned_at": slot.isoformat(), "started_at": slot.isoformat(), "pid": 999_999},
              open(marker, "w", encoding="utf-8"))
    app._recover_interrupted_run()
    for _ in range(3):
        app._scheduler_pass()
    assert started == []


# --------------------------- 3. held report -> retried -> sent partial

class _Clock:
    now_value = datetime(2026, 10, 28, 12, 0, 0)


class _FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D401
        return _Clock.now_value


def test_held_report_is_retried_by_the_scheduler_then_sent_with_the_partial_note(app_state, monkeypatch):
    app_module, _auth, _ops = app_state
    main = pytest.importorskip("main")
    from core import config_loader, report_gate
    from notifications import mailer
    monkeypatch.delenv("IBO_REPORT_RETRY_MAX", raising=False)
    monkeypatch.delenv("IBO_REPORT_RETRY_MINUTES", raising=False)
    monkeypatch.delenv("IBO_REPORT_PARTIAL_MAX", raising=False)
    monkeypatch.delenv("IBO_REPORT_GATE", raising=False)
    monkeypatch.setattr(report_gate, "datetime", _FakeDT)
    monkeypatch.setattr(report_gate, "_basis", lambda cfg: ["basis@infrabeat.com"])
    monkeypatch.setattr(config_loader, "get_smtp_config", lambda cfg, require_recipients=True: {})
    internal = []
    monkeypatch.setattr(mailer, "send_html", lambda **k: internal.append(k["subject"]) or True)
    report_gate.set_manual_run(False)
    started = []
    monkeypatch.setattr(app_module, "_run_one_system_background",
                        lambda s, **k: started.append((s, k.get("profile_id"))) or True)
    monkeypatch.setattr(app_module, "_sweep_block_reason", lambda s: None)
    app_module._run_state["running"] = False
    report_gate.clear("PS4")

    t0 = datetime(2026, 10, 28, 12, 0, 0)
    _Clock.now_value = t0
    report_gate.mark_scheduler_alive()
    cfg = {"name": "PS4"}
    gate = {"complete": False, "reasons": ["sheet not filled for 1 check(s): ST02"], "retry": True,
            "no_retry": "", "missing": ["ST02"], "details": {"ST02": "screen did not load"},
            "sheet_error": False, "gui_failed": False}

    # try 1 (the scheduled sweep): held quietly, retry planned 15 min later
    out = main._hold_report_quietly(cfg, gate, {})
    assert out["send_partial"] is False and out["due"] == t0 + timedelta(minutes=15)
    assert internal == []                                       # quiet: no REPORT HELD mail

    # the scheduler, 10 min later: nothing due yet
    _Clock.now_value = t0 + timedelta(minutes=10)
    assert app_module._run_due_report_retry() is False and started == []
    # 16 min later: one retry starts, and only one even if the pass repeats
    _Clock.now_value = t0 + timedelta(minutes=16)
    report_gate.mark_scheduler_alive()
    assert app_module._run_due_report_retry() is True
    assert app_module._run_due_report_retry() is False
    assert started == [("PS4", app_module.REPORT_RETRY_LABEL)]

    # try 2 ends still incomplete: next retry planned
    _Clock.now_value = t0 + timedelta(minutes=20)
    out = main._hold_report_quietly(cfg, gate, {})
    assert out["send_partial"] is False and out["due"] is not None
    assert report_gate.next_attempt("PS4") == 3

    _Clock.now_value = out["due"] + timedelta(seconds=30)
    report_gate.mark_scheduler_alive()
    assert app_module._run_due_report_retry() is True
    assert len(started) == 2

    # try 3 (the last): one check missing -> sent anyway, with the note
    _Clock.now_value += timedelta(minutes=4)
    ctx = {}
    out = main._hold_report_quietly(cfg, gate, ctx)
    assert out == {"due": None, "send_partial": True}
    assert not any("NOT sent" in s or "REPORT HELD" in s for s in internal)

    sent = {}
    monkeypatch.setattr(main, "_email_enabled", lambda: True)
    monkeypatch.setattr(main, "get_smtp_config", lambda c=None: {"to_emails": ["client@metro.example"],
                                                                 "cc_emails": []})
    monkeypatch.setattr(main, "send_final_report",
                        lambda r, smtp, pdf, xls, note_html="": sent.update(note=note_html, to=smtp["to_emails"])
                        or True)
    result = SimpleNamespace(system="PS4", overall_status=SimpleNamespace(value="NORMAL"),
                             cycle_timestamp="2026-10-28 12:40:00")
    deliveries = main._report_deliveries(cfg, result, [], "a.pdf", "b.xlsx", True, ctx,
                                         note_html=report_gate.partial_note(gate))
    assert [k for k, _f, _w in deliveries] == ["final_report"]
    assert deliveries[0][1]() is True
    assert sent["to"] == ["client@metro.example"]
    assert "ST02" in sent["note"] and "screen did not load" in sent["note"] and "3 tries" in sent["note"]
    assert any(s.startswith("[REPORT SENT]") for s in internal)
    # the chain is over: no further retry, ever
    _Clock.now_value += timedelta(hours=1)
    assert report_gate.due_retries() == []
    assert app_module._run_due_report_retry() is False and len(started) == 2


# ------------------------- 4. owner mail: request -> approve link -> once

@pytest.fixture
def owner(env, monkeypatch):  # noqa: F811
    app_module, auth, _ops = env
    monkeypatch.setenv("IBO_AUTH", "1")
    monkeypatch.setenv("IBO_PUBLIC_URL", "https://ibops.example")
    monkeypatch.setenv("IBO_INTERNAL_DOMAINS", "infrabeat.com")
    monkeypatch.delenv("IBO_ACTION_SECRET", raising=False)
    monkeypatch.delenv("IBO_OWNER_MAIL_APPROVAL", raising=False)
    auth.add_user("boss", "admin-password-1", "admin", "*")        # logins are enforced
    from core import owner_mail
    from notifications import mailer
    approvals, sends = [], []
    monkeypatch.setattr(owner_mail, "_smtp", lambda system: {})
    monkeypatch.setattr(mailer, "send_html", lambda **k: approvals.append(k) or True)
    state = {"ok": True}

    def _send_now(subject, html, to, smtp, cc=None, text_fallback=""):
        sends.append({"subject": subject, "to": list(to), "cc": list(cc or [])})
        return state["ok"]
    monkeypatch.setattr(mailer, "send_html_now", _send_now)
    monkeypatch.setattr(mailer, "last_reason", lambda: "550 relay refused", raising=False)
    return app_module, owner_mail, approvals, sends, state


def _request(owner_mail, key="job:Z_FAIL:2026-10-28"):
    return owner_mail.request("PS4", "job_owner", key, ["owner@metro.example"], ["basis@infrabeat.com"],
                              "Job Z_FAIL was cancelled", "<html><body><p>Your job failed</p></body></html>",
                              "Your job failed", "cancelled job Z_FAIL", ["basis@infrabeat.com"])


def _token(approval_mail):
    m = re.search(r"https://ibops\.example/access/action\?t=([A-Za-z0-9_\-.]+)", approval_mail["html"])
    assert m, approval_mail["html"][:500]
    return m.group(1)


def test_owner_mail_request_approve_link_sends_once_second_click_refused(owner):
    app_module, owner_mail, approvals, sends, _state = owner
    rid = _request(owner_mail)
    assert rid and sends == []                                   # nothing to the owner yet
    assert len(approvals) == 1 and approvals[0]["to"] == ["basis@infrabeat.com"]
    assert "owner@metro.example" in approvals[0]["html"]
    tok = _token(approvals[0])
    assert _request(owner_mail) is None                          # same key, same day: asked once

    c = TestClient(app_module.app)                               # not signed in: the link is enough
    page = c.get(f"/access/action?t={tok}")
    assert page.status_code == 200 and "Confirm" in page.text and "owner@metro.example" in page.text
    assert sends == []                                           # a mail scanner's GET does nothing

    done = c.post("/access/action", data={"t": tok, "name": "Alex"})
    assert done.status_code == 200 and "Done" in done.text
    assert sends == [{"subject": "Job Z_FAIL was cancelled", "to": ["owner@metro.example"],
                      "cc": ["basis@infrabeat.com"]}]
    r = owner_mail.row(rid)
    assert r["status"] == "sent" and "Alex" in r["decided_by"]

    again = c.post("/access/action", data={"t": tok, "name": "Someone else"})
    assert again.status_code == 400 and "already been used" in again.text
    assert c.get(f"/access/action?t={tok}").status_code == 400
    assert len(sends) == 1
    with pytest.raises(ValueError):
        owner_mail.decide(rid, True, by="dashboard button")      # the page button is refused too
    assert len(sends) == 1


def test_owner_mail_link_survives_a_mail_server_failure_and_then_works_once(owner):
    app_module, owner_mail, approvals, sends, state = owner
    rid = _request(owner_mail, key="dump:SAPLZ:2026-10-28")
    tok = _token(approvals[-1])
    c = TestClient(app_module.app)
    state["ok"] = False
    r = c.post("/access/action", data={"t": tok})
    assert r.status_code == 400 and "not sent" in r.text and "550" in r.text
    assert owner_mail.row(rid)["status"] == "failed"
    state["ok"] = True
    assert c.post("/access/action", data={"t": tok}).status_code == 200    # same link, retried
    assert owner_mail.row(rid)["status"] == "sent" and len(sends) == 2
    assert c.post("/access/action", data={"t": tok}).status_code == 400
    assert len(sends) == 2


def test_tampered_or_foreign_owner_mail_link_is_refused(owner):
    app_module, owner_mail, approvals, sends, _state = owner
    _request(owner_mail, key="k-tamper")
    tok = _token(approvals[-1])
    raw, sig = tok.split(".", 1)
    import base64
    body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    body["r"] = 9999                                             # point the link at another request
    forged = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()) \
        .decode().rstrip("=") + "." + sig
    c = TestClient(app_module.app)
    r = c.post("/access/action", data={"t": forged})
    assert r.status_code == 400 and "not issued" in r.text
    assert sends == []


def test_owner_mail_dashboard_button_is_scoped_by_project_and_role(team, monkeypatch):
    app_module, users, _started = team
    from core import owner_mail
    from notifications import mailer
    sends = []
    monkeypatch.setattr(owner_mail, "_ask_basis", lambda rid, basis: None)
    monkeypatch.setattr(owner_mail, "_smtp", lambda system: {})
    monkeypatch.setattr(mailer, "send_html_now",
                        lambda subject, html, to, smtp, cc=None, text_fallback="": sends.append(list(to)) or True)
    ps4 = owner_mail.request("PS4", "job_owner", "k-ps4", ["o@metro.example"], [], "s", "h", "", "w", ["b@x"])
    ceq = owner_mail.request("CEQ", "dump_owner", "k-ceq", ["t@centor.example"], [], "s", "h", "", "w", ["b@x"])

    viewer = _login(app_module, *users["viewer"][:2])
    assert viewer.post(f"/api/ops/owner-mail-requests/{ps4}/approve").status_code == 403
    plain = _login(app_module, *users["consultant"][:2])                 # no alerts grant
    assert plain.post(f"/api/ops/owner-mail-requests/{ps4}/approve").status_code == 403
    lead = _login(app_module, *users["team_lead"][:2])
    listed = lead.get("/api/ops/owner-mail-requests").json()["requests"]
    assert [r["id"] for r in listed] == [ps4]                           # CEQ is not shown
    assert listed[0]["to"] == ["1 address(es)"]                         # addresses: admins only
    assert lead.post(f"/api/ops/owner-mail-requests/{ceq}/approve").status_code == 404
    assert sends == []
    r = lead.post(f"/api/ops/owner-mail-requests/{ps4}/approve")
    assert r.status_code == 200 and "Done" in r.json()["message"]
    assert sends == [["o@metro.example"]]
    assert lead.post(f"/api/ops/owner-mail-requests/{ps4}/approve").status_code == 400
    granted = _login(app_module, *users["granted"][:2])                 # alerts granted on PS4
    assert granted.post(f"/api/ops/owner-mail-requests/{ceq}/reject").status_code == 404
    assert owner_mail.row(ceq)["status"] == "waiting" and len(sends) == 1


def test_scheduler_page_schedule_runs_once_and_a_parked_system_is_skipped(sched, monkeypatch):
    app, profiles, sch, cfg, started = sched
    from core import alert_control, run_history
    monkeypatch.setattr(profiles, "load_profiles", lambda: [])          # Scheduler-page schedules only
    notices = []
    monkeypatch.setattr(app, "_schedule_notice", lambda *a, **k: notices.append((a, k)))
    hourly = {"enabled": True, "mode": "hours", "every": 1, "at": "00:00", "days": list(sch.WEEKDAYS)}
    slot = sch.previous_slot(sch._normalise(hourly))
    for name in ("PS4", "CEQ"):
        sch.set_schedule(name, dict(hourly))
        sch.mark_run(name, slot - timedelta(hours=1))
    alert_control.park("CEQ", "SAP user locked")
    for _ in range(4):
        app._scheduler_pass()
    assert started == [("PS4", app.SCHEDULE_RUN_LABEL, slot)]
    parked = [r for r in run_history.query(system="CEQ") if r["result"] == "parked"]
    assert len(parked) == 1                                             # recorded once, not every pass
    assert len([n for n in notices if n[1].get("kind") == "parked"]) == 1
    assert app._scheduler_state.get("loop_error") is None


def test_grant_narrowed_to_one_system_of_a_project_hides_the_others(team, monkeypatch):
    """con2's grant names PS4 only; PS5 (same project Metro) is neither visible nor writable."""
    app_module, users, started = team
    from core import auth, config_loader
    three = SYSTEMS + [{"name": "PS5", "customer": "Metro"}]
    monkeypatch.setattr(app_module, "get_systems", lambda: auth.filter_systems([dict(s) for s in three]))
    monkeypatch.setattr(config_loader, "get_all_systems", lambda *a, **k: [dict(s) for s in three])
    monkeypatch.setattr(config_loader, "get_systems", lambda *a, **k: auth.filter_systems([dict(s) for s in three]))
    c = _login(app_module, *users["granted"][:2])
    assert _norm(_probe(c, "PS4")) == {"read": OK, "run": OK, "alerts": OK, "settings": 403}
    app_module._release_manual_claim()
    assert _norm(_probe(c, "PS5")) == INVISIBLE
    assert "PS5" not in started
    lead = _login(app_module, *users["team_lead"][:2])          # the lead sees the whole project
    app_module._release_manual_claim()
    assert _norm(_probe(lead, "PS5")) == ALL_OK
