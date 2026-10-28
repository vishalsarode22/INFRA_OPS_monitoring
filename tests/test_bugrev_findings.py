"""Proofs for the bug review of 28.10.2026. Each test FAILS on the current code."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import schedules as sch  # noqa: E402

DAY = datetime(2026, 10, 9)


def at(hh, mm=0, ss=0, days=0):
    return DAY + timedelta(days=days, hours=hh, minutes=mm, seconds=ss)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_STATE_BACKEND", "files")
    monkeypatch.delenv("IBO_SCHEDULE_GRACE_MINUTES", raising=False)
    monkeypatch.setattr(sch, "_PATH", str(tmp_path / "schedules.json"))
    monkeypatch.setattr(sch, "STATE_PATH", str(tmp_path / "schedule_state.json"))
    sch._missed_reported.clear()
    sch._last_good.clear()
    from core import profiles, run_history
    monkeypatch.setattr(profiles, "STATE_PATH", str(tmp_path / "profile_state.json"))
    profiles._last_good.clear()
    monkeypatch.setattr(profiles, "resolve_tcodes", lambda p: ["SM51"])
    monkeypatch.setattr(run_history, "DB_PATH", str(tmp_path / "rh.sqlite"))
    return tmp_path


def _profile(pid, system, at_="09:00"):
    s = sch._normalise({"enabled": True, "mode": "daily", "at": at_})
    return {"id": pid, "system": system, "deliver": ["email"], "label": pid, "schedule": s,
            "tcodes": "all", "email_to": [], "email_cc": []}


# 1. The scheduler's OWN sweep does not extend the grace window (busy_since is
#    only set by a pass that sees _run_state["running"], and no pass happens
#    while the scheduler thread itself runs the sweep).
def test_second_profile_is_not_missed_behind_a_long_scheduled_sweep(env, monkeypatch):
    import dashboard.app as app
    from core import profiles
    monkeypatch.setattr(profiles, "load_profiles",
                        lambda: [_profile("prd-full", "PRD"), _profile("qas-daily", "QAS")])
    profiles.set_handled("prd-full", at(8))
    profiles.set_handled("qas-daily", at(8))
    clock = {"now": at(9, 0, 15)}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]
    for mod in (profiles, sch, app):
        monkeypatch.setattr(mod, "datetime", _Clock)
    started = []

    def fake_run(system, **kw):           # a 70-minute PRD sweep
        started.append(system)
        clock["now"] += timedelta(minutes=70)
        return True
    monkeypatch.setattr(app, "_run_one_system_background", fake_run)
    monkeypatch.setattr(app, "_sweep_block_reason", lambda s: None)
    monkeypatch.setattr(app, "_stop_hold_reason", lambda s: None)
    monkeypatch.setattr(app, "_run_due_report_retry", lambda: False)
    monkeypatch.setattr(app, "_run_due_system_schedule", lambda w=None: False)
    app._scheduler_state.update(enabled=True, snooze_until=None, manual_pause=False, busy_since=None)
    app._run_state["running"] = False
    app._scheduler_pass()                 # PRD runs 09:00 -> 10:10
    clock["now"] += timedelta(seconds=20)
    app._scheduler_pass()                 # QAS 09:00 waited behind it
    assert started == ["PRD", "QAS"], "QAS 09:00 recorded MISSED instead of running after PRD"


# 2. "every N hours/minutes through `at`" only stays on the grid when N divides 24 h.
def test_interval_grid_keeps_the_at_time_every_day():
    cfg = {"enabled": True, "mode": "hours", "every": 5, "at": "08:00"}
    slots = {sch.next_slot(cfg, datetime(2026, 10, d, 7, 59)).strftime("%H:%M") for d in (27, 28, 29)}
    assert slots == {"08:00"}, f"slots drift day to day: {slots}"


# 3. A report RETRY runs with profile_id "Retry: report was incomplete": the
#    original profile's delivery (none / WhatsApp only / own email_to) is lost
#    and the report is mailed to the system's default client list.
def test_report_retry_honours_the_profile_that_was_held(monkeypatch, tmp_path):
    """Fixed: the retry chain stores the held run's profile and the retry runs AS that profile."""
    import main
    from core import profiles, report_gate, store, ops_db
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "r.sqlite"))
    if hasattr(store._local, "conn"):
        del store._local.conn
    ops_db.reset_for_tests() if hasattr(ops_db, "reset_for_tests") else None
    monkeypatch.setenv("IBO_REPORT_RETRY_MAX", "2")
    report_gate.plan_retry("PRD", 1, "ST02 empty", profile_id="prd-quick")
    assert report_gate.retry_profile("PRD") == "prd-quick"
    report_gate.plan_retry("PRD", 2, "ST02 empty", profile_id="Retry: report was incomplete")
    assert report_gate.retry_profile("PRD") == "prd-quick"          # kept for the whole chain
    # The retry run carries the profile, so its delivery rules apply ("no destination" = nothing).
    monkeypatch.setattr(profiles, "load_profiles",
                        lambda: [dict(_profile("prd-quick", "PRD"), deliver=[])])
    monkeypatch.setenv("IBO_ENABLE_EMAIL", "1")
    out = main._report_deliveries({"name": "PRD", "_profile_id": "prd-quick"}, result=None, gui_results=[],
                                  pdf_path="", sheet_path="", gated=True, ctx={})
    assert [k for k, _f, _w in out] == []
    import inspect, dashboard.app as app
    src = inspect.getsource(app._run_due_report_retry)
    assert "retry_profile(name)" in src and "unmark_started" in src


# 4. ${VAR} in a LIST is never resolved, and an unresolved scalar silently
#    falls back to the global .env list (another customer's recipients).
def test_receivers_placeholders(monkeypatch):
    from core import config_loader as cl
    monkeypatch.setenv("CEAT_TO", "basis@ceat.example")
    sys_ = cl._resolve_system({"name": "S4P", "alert_receivers": ["${CEAT_TO}"]})
    assert sys_["alert_receivers"] == ["basis@ceat.example"], sys_["alert_receivers"]


def test_unresolved_receiver_does_not_fall_back_to_global(monkeypatch):
    from core import config_loader as cl
    monkeypatch.delenv("CEAT_TO", raising=False)
    monkeypatch.setenv("ALERT_TO_EMAILS", "someone@other-client.example")
    for k, v in {"SMTP_HOST": "h", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p",
                 "ALERT_FROM_EMAIL": "f@x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cl, "_secret_from_keyring", lambda n: None, raising=False)
    sys_ = cl._resolve_system({"name": "S4P", "alert_receivers": "${CEAT_TO}"})
    try:
        smtp = cl.get_smtp_config(sys_)
    except ValueError:
        return                                   # refusing is the correct behaviour
    assert smtp["to_emails"] != ["someone@other-client.example"], \
        "a missing per-system variable mails the global list"


# 5. The final "Monitoring sheet NOT sent" mail is kind report_held, which the
#    default IBO_DISABLED_MAIL_KINDS switches off: whenever the gate is applied
#    (IBO_MAIL_ASYNC / split deployment) the held report ends silently.
def test_final_not_sent_mail_is_not_policy_suppressed(tmp_path, monkeypatch):
    from core import alert_ledger, store
    monkeypatch.delenv("IBO_DISABLED_MAIL_KINDS", raising=False)
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "a.sqlite"))
    if hasattr(store._local, "conn"):
        del store._local.conn
    monkeypatch.setattr(alert_ledger, "history", lambda *a, **k: None)
    ok, why = alert_ledger.should_send("PS4", "report_not_sent", "202610281200")
    assert ok, why
    import inspect
    from core import report_gate
    assert '"report_held" if due else "report_not_sent"' in inspect.getsource(report_gate._tell_basis)
