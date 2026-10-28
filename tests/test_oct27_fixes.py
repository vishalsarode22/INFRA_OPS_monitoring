"""27 Oct fixes: scheduled run not lost behind an RCA, RCA log, quieter Errors page,
mail exclusion + DOWN reminders slowing down, address finder, Windows VPN keeper."""
import logging
import os
from datetime import datetime, timedelta

import pytest

from test_fix_monitoring_log import env  # noqa: F401  (PS4=Metro, CEQ=Centor)


# ---------------------------------------------------------------- schedules

def test_busy_time_does_not_count_against_the_grace_window(monkeypatch):
    from core import schedules
    monkeypatch.setenv("IBO_SCHEDULE_GRACE_MINUTES", "60")
    cfg = {"enabled": True, "mode": "daily", "every": 1, "at": "09:00", "days": list(schedules.WEEKDAYS)}
    now = datetime(2026, 10, 27, 10, 40)                       # 100 min after the slot
    handled = datetime(2026, 10, 26, 9, 0)
    assert schedules.slot_decision(cfg, handled, now)[0] == "missed"
    busy_since = datetime(2026, 10, 27, 8, 55)                 # an RCA started before 09:00
    win = schedules.busy_window(busy_since, now)
    state, slot = schedules.slot_decision(cfg, handled, now, win)
    assert state == "due" and slot == datetime(2026, 10, 27, 9, 0)
    monkeypatch.setenv("IBO_SCHEDULE_BUSY_MAX_MINUTES", "90")
    assert schedules.busy_window(datetime(2026, 10, 26, 0, 0), now) == timedelta(minutes=90)


def test_automatic_rca_waits_and_never_cancels_a_running_sweep(env, monkeypatch):  # noqa: F811
    app_module, _auth, _ops = env
    from core import rca_log
    monkeypatch.setenv("IBO_RCA_WAIT_FOR_SWEEP_MIN", "0.001")

    class _Cfg:
        preempt_running_sweep = True
    monkeypatch.setattr(app_module, "_rca_trigger", type("T", (), {"cfg": _Cfg()})())
    monkeypatch.setattr(app_module, "get_systems", lambda: [{"name": "PS4", "customer": "Metro"}])
    app_module._run_state.update(running=True, current_system="CEQ", cancel_requested=False)
    app_module._rca_cancel["on"] = False
    try:
        app_module._run_rca_background("PS4", "instance PS4_00 response 2400 ms")
        assert app_module._run_state["cancel_requested"] is False          # the sweep was left alone
    finally:
        app_module._run_state.update(running=False, current_system=None)
    rows = rca_log.query(1, "PS4")
    assert rows and rows[0]["result"] == "not-started" and "monitoring has priority" in rows[0]["error"]


# ------------------------------------------------------------------ RCA log

def test_rca_log_records_cause_culprits_and_mail(env, monkeypatch, tmp_path):  # noqa: F811
    from core import rca_log
    from evaluation import rca_pipeline as rp
    import reporting.rca_report as rr
    monkeypatch.setattr(rr, "build_rca_pdf", lambda *a, **k: None)
    monkeypatch.setattr(rp, "report_paths", lambda s, w: (tmp_path / "r.pdf", tmp_path / "r.json"))
    monkeypatch.setattr(rp, "rfc_context", lambda c: {})
    monkeypatch.setattr(rp, "analyze", lambda *a, **k: {
        "severity": "CRITICAL", "headline": "Lock wait on MARA held by GRRSRC",
        "root_cause": "Batch job ZMM299 keeps an enqueue on MARA for 30+ min",
        "culprits": [{"type": "user", "name": "GRRSRC", "evidence": "SM12: 4379 locks", "impact": "dialog waits"}],
        "source": "rules"})

    def fake_dispatch(cfg, rcfg, pdf, analysis, reason):
        rp._last_dispatch.update(to=["staff.d@infrabeat.com"], why="sent")
        return True
    monkeypatch.setattr(rp, "dispatch", fake_dispatch)
    out = rp.run_rca({"name": "PS4"}, "instance PS4_00 response 2400 ms", cfg={}, gui_results=[])
    row = rca_log.query(1, "PS4")[0]
    assert out["rca_log_id"] == row["id"] and row["result"] == "done" and row["trigger"] == "automatic"
    assert row["culprits"][0]["name"] == "GRRSRC" and "MARA" in row["headline"]
    assert row["mailed_to"] == "staff.d@infrabeat.com" and row["mail_result"] == "sent"


def test_rca_log_api_hides_addresses_from_read_only_roles(env, monkeypatch):  # noqa: F811
    app_module, auth, _ops = env
    from fastapi.testclient import TestClient
    from core import rca_log
    rid = rca_log.start("PS4", "manual: Performance monitoring button")
    rca_log.finish(rid, "done", {"headline": "x"}, mailed_to=["a@infrabeat.com", "b@infrabeat.com"])
    monkeypatch.setenv("IBO_AUTH", "1")
    auth.add_user("viewer1", "viewer-pass-12", "viewer", "Metro")
    c = TestClient(app_module.app)
    c.post("/login", data={"username": "viewer1", "password": "viewer-pass-12"},
           headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    runs = c.get("/api/rca/log").json()["runs"]
    assert runs[0]["trigger"] == "manual" and runs[0]["mailed_to"] == "2 person(s)"


# ------------------------------------------------------------------ errors

def test_client_disconnects_are_not_errors():
    from core import errors
    rec = logging.LogRecord("asyncio", logging.ERROR, __file__, 1, "Exception in callback "
                            "_ProactorBasePipeTransport._call_connection_lost()", (), None)
    try:
        raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
    except ConnectionResetError:
        import sys
        rec.exc_info = sys.exc_info()
    assert errors.benign_network(rec)
    real = logging.LogRecord("main", logging.ERROR, __file__, 1, "Pipeline for PRD hung", (), None)
    assert not errors.benign_network(real)


def test_ai_quota_is_a_warning_not_an_error(monkeypatch, caplog):
    from evaluation import rca_pipeline as rp
    monkeypatch.setenv("IBO_ENABLE_AI", "1")
    import evaluation.ai_analyzer as aa

    class P:
        def generate(self, prompt):
            raise RuntimeError("AI chain: all providers failed -- gemini#1: Gemini daily quota exhausted (HTTP 429)")
    monkeypatch.setattr(aa, "get_provider", lambda: P())
    monkeypatch.setattr(rp, "build_prompt", lambda *a: "p")
    monkeypatch.setattr(rp, "rules_based_analysis", lambda e, r: {"severity": "WARNING"})
    with caplog.at_level(logging.WARNING):
        out = rp.analyze("PS4", "x", [], {}, {})
    assert out["severity"] == "WARNING"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# -------------------------------------------------------------------- mail

def test_mail_exclude_removes_an_address_everywhere(monkeypatch):
    from notifications import mailer
    monkeypatch.setenv("IBO_MAIL_EXCLUDE", "staff.c@infrabeat.com")
    to, cc, _ = mailer.without_client(["staff.c@infrabeat.com", "staff.b@infrabeat.com"],
                                      ["staff.c@infrabeat.com"], "CEAT S4P", "")
    assert to == ["staff.b@infrabeat.com"] and cc == []


def test_down_reminders_slow_to_hourly_after_the_daily_kind_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_MAIL_CAPS", "1")          # limits switched on for this test
    from core import alert_ledger as al, store
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "l.sqlite"))
    for name in ("_mail_outage",):
        monkeypatch.setattr(al, name, lambda s: "")
    monkeypatch.setattr(al, "_gate", lambda *a: None)
    monkeypatch.setattr(al, "history", lambda *a, **k: None)
    monkeypatch.setattr(al, "_whatsapp_copy", lambda *a, **k: None)
    monkeypatch.setenv("IBO_ALERT_DAILY_CAP", "2")
    for i in range(2):
        assert al.should_send("CAR PRD", "not_reachable", "CAR PRD", severity="CRITICAL")[0]
        al.record("CAR PRD", "not_reachable", "CAR PRD", "CRITICAL", "s", ["a@infrabeat.com"],
                  mirror=False, hooks=False)
    ok, why = al.should_send("CAR PRD", "not_reachable", "CAR PRD", severity="CRITICAL")
    assert not ok and "one an hour" in why
    # an hour later it goes again
    with al._connect() as c:
        c.execute("UPDATE alerts SET sent_at=? WHERE system='CAR PRD'",
                  ((datetime.now() - timedelta(minutes=61)).strftime("%Y-%m-%d %H:%M:%S"),))
    assert al.should_send("CAR PRD", "not_reachable", "CAR PRD", severity="CRITICAL")[0]


def test_back_to_normal_is_not_held_by_the_everyday_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_MAIL_CAPS", "1")          # limits switched on for this test
    from core import alert_ledger as al, store
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "l.sqlite"))
    monkeypatch.setattr(al, "_mail_outage", lambda s: "")
    monkeypatch.setattr(al, "_gate", lambda *a: None)
    monkeypatch.setattr(al, "history", lambda *a, **k: None)
    monkeypatch.setenv("IBO_MAIL_CAP_PER_SYSTEM", "1")
    al.record("CEAT S4P", "metric", "a", "WARNING", "s", ["a@infrabeat.com"], mirror=False, hooks=False)
    assert not al.should_send("CEAT S4P", "metric", "b", 0, 0, severity="WARNING")[0]
    assert al.should_send("CEAT S4P", "metric", "a", 0, 0, severity="NORMAL", closing=True)[0]


def test_find_address_lists_every_place(tmp_path, monkeypatch):
    from core import find_address
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "alert_teams.yaml").write_text(
        "standard:\n  basis: [staff.c@infrabeat.com]\n", encoding="utf-8")
    (tmp_path / "config" / "systems.yaml").write_text(
        "systems:\n- name: CEAT S4P\n  basis_emails: staff.b@infrabeat.com\n", encoding="utf-8")
    (tmp_path / ".env").write_text("IBO_BASIS_EMAILS=staff.c@infrabeat.com\nSMTP_PASSWORD=secret\n",
                                   encoding="utf-8")
    hits = find_address.find("staff.c@infrabeat.com", root=str(tmp_path))
    where = {h["where"].replace(os.sep, "/") for h in hits}
    assert "config/alert_teams.yaml" in where and ".env" in where
    assert all("secret" not in h["text"] for h in hits)
    assert any("replaces" in h["edit"] for h in hits)


# ------------------------------------------------------------------ VPN keeper

def test_vpn_keeper_reconnects_then_backs_off_then_stops(monkeypatch):
    from core import vpn_keeper as vk
    vk._state.clear()
    vpn = {"name": "AG", "gateway": "203.0.113.74:10443", "username": "u", "password": "p",
           "systems": ["PS4"], "probe": "10.0.0.1:3300", "enabled": True}
    up = {"v": False}
    monkeypatch.setattr(vk, "reachable", lambda t, timeout=3.0: up["v"])
    tries = []

    def fake_connect(v, exe):
        tries.append(1)
        return False, "the VPN did not come up within 75 s"
    monkeypatch.setattr(vk, "_connect", fake_connect)
    monkeypatch.setattr(vk, "_notify_stopped", lambda v, r: tries.append("mail"))
    t = 1000.0
    assert vk.check_once(vpn, "x.exe", t)["state"] == "unknown"          # 1st failure: wait
    st = vk.check_once(vpn, "x.exe", t + 60)
    assert st["state"] == "retrying" and len(tries) == 1
    for delay in (120, 300, 900):
        t = st["next_try"] + 1
        st = vk.check_once(vpn, "x.exe", t)
    assert st["state"] == "stopped" and "mail" in tries
    up["v"] = True                                                       # fixed by hand
    assert vk.check_once(vpn, "x.exe", t + 60)["state"] == "up"


def test_vpn_keeper_does_not_fight_an_account_in_use(monkeypatch):
    from core import vpn_keeper as vk
    vk._state.clear()
    vpn = {"name": "AG", "gateway": "g:1", "username": "u", "password": "p", "systems": [],
           "probe": "10.0.0.1:3300", "enabled": True}
    seq = iter([True, False, False, True, False, False, False])
    monkeypatch.setattr(vk, "reachable", lambda t, timeout=3.0: next(seq))
    monkeypatch.setattr(vk, "_connect", lambda v, e: (True, "connected"))
    t = 0.0
    for _ in range(6):
        st = vk.check_once(vpn, "x.exe", t)
        t += 60
    st = vk.check_once(vpn, "x.exe", t)
    assert st["state"] == "in_use" and "not pushing them off" in st["reason"]


def test_vpn_keeper_watch_only_without_the_client(monkeypatch):
    from core import vpn_keeper as vk
    vk._state.clear()
    monkeypatch.setattr(vk, "reachable", lambda t, timeout=3.0: False)
    vpn = {"name": "AG", "gateway": "g:1", "username": "u", "password": "p", "systems": [],
           "probe": "10.0.0.1:3300", "enabled": True}
    vk.check_once(vpn, None, 0)
    st = vk.check_once(vpn, None, 60)
    assert st["state"] == "no-client" and "FortiSSLVPNclient.exe" in st["reason"]


def test_one_rfc_worker_crash_is_a_warning_three_are_an_error(env, monkeypatch):  # noqa: F811
    app_module, _auth, _ops = env
    from core import errors, rfc_workers
    caught = []
    monkeypatch.setattr(errors, "capture", lambda exc, *a, **k: caught.append(k.get("context", "")))
    monkeypatch.setattr(rfc_workers, "enabled", lambda: True)

    def boom(*a, **k):
        raise RfcWorkerDied("RFC worker for PS4 died (exit code 3221225477)")
    monkeypatch.setattr(rfc_workers, "run", boom, raising=False)
    monkeypatch.setattr(rfc_workers, "call", boom, raising=False)
    app_module._rfc_crashes.clear()
    for _ in range(2):
        with pytest.raises(RfcWorkerDied):
            app_module._run_in_rfc_pool(lambda *a: None, "PS4")
    assert caught == []
    with pytest.raises(RfcWorkerDied):
        app_module._run_in_rfc_pool(lambda *a: None, "PS4")
    assert caught and "3 times in 6 h" in caught[0]


from core.rfc_workers import RfcWorkerDied  # noqa: E402


def test_systems_page_basis_box_beats_the_standard_alert_team(monkeypatch):
    from core import alert_teams, alert_engine
    data = {"standard": {"Basis": ["staff.c@infrabeat.com"]}, "systems": {}}
    monkeypatch.setattr(alert_teams, "load", lambda: data)
    cfg = {"name": "CEAT S4P", "basis_emails": "basis@client-a.example,staff.b@infrabeat.com"}
    assert alert_engine.recipients(cfg)["email"]["to"] == ["basis@client-a.example", "staff.b@infrabeat.com"]
    assert alert_teams.basis_for({"name": "PS4"})[0] == ["staff.c@infrabeat.com"]   # box empty
    data["systems"]["CEAT S4P"] = {"Basis": ["ceat.basis@infrabeat.com"]}                 # own team wins
    assert alert_engine.recipients(cfg)["email"]["to"] == ["ceat.basis@infrabeat.com"]


def test_find_address_replace_and_remove(tmp_path):
    import yaml
    from core import find_address as fa
    (tmp_path / "config").mkdir()
    p = tmp_path / "config" / "alert_teams.yaml"
    p.write_text("standard:\n  Basis:\n  - staff.c@infrabeat.com\n  - x@infrabeat.com\n"
                 "systems:\n  CEAT S4P:\n    Basis: [staff.c@infrabeat.com, y@infrabeat.com]\n", encoding="utf-8")
    s = tmp_path / "config" / "systems.yaml"
    s.write_text("systems:\n- name: A\n  basis_emails: a@infrabeat.com, staff.c@infrabeat.com\n", encoding="utf-8")
    out = fa.replace("staff.c@infrabeat.com", "staff.b@infrabeat.com", root=str(tmp_path))
    assert len(out) == 2
    d = yaml.safe_load(p.read_text())
    assert d["standard"]["Basis"] == ["staff.b@infrabeat.com", "x@infrabeat.com"]
    assert d["systems"]["CEAT S4P"]["Basis"] == ["staff.b@infrabeat.com", "y@infrabeat.com"]
    fa.replace("staff.b@infrabeat.com", "", root=str(tmp_path))
    d = yaml.safe_load(p.read_text())
    assert d["standard"]["Basis"] == ["x@infrabeat.com"] and d["systems"]["CEAT S4P"]["Basis"] == ["y@infrabeat.com"]
    assert yaml.safe_load(s.read_text())["systems"][0]["basis_emails"] == "a@infrabeat.com"


def test_no_mail_limits_by_default(monkeypatch):
    """28.10.2026: no limit on mails for any system (CEAT S4P was held at 200)."""
    monkeypatch.delenv("IBO_MAIL_CAPS", raising=False)
    from core import alert_ledger
    assert alert_ledger.caps_on() is False
