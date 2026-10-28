"""Own people for back-to-normal / system-down / VPN-logon mails, and per-system alert timing."""
import pytest

SYS = {"name": "BW_PRD", "basis_emails": "staff.d@infrabeat.com",
       "manager_receivers": "staff.a@infrabeat.com",
       "recovery_emails": "shift.lead@infrabeat.com", "down_emails": "noc@infrabeat.com, staff.b@infrabeat.com",
       "access_emails": "staff.e@infrabeat.com",
       "alert_timing": {"repeat_critical_min": 30, "repeat_warning_min": 240, "escalate_window_min": 90,
                        "raise_after": 2, "recovery_min_minutes": 15, "down_after": 3, "down_repeat_min": 10,
                        "access_repeat_min": 45, "rca_daily_cap": 2}}
PLAIN = {"name": "PLAIN", "basis_emails": "staff.d@infrabeat.com",
         "manager_receivers": "staff.a@infrabeat.com"}


@pytest.fixture(autouse=True)
def systems(monkeypatch):
    from core import config_loader
    for k in ("IBO_RECOVERY_EMAILS", "IBO_DOWN_EMAILS", "IBO_ACCESS_EMAILS", "IBO_DOWN_REPEAT_MIN",
              "IBO_ACCESS_REPEAT_MIN", "IBO_ALERT_REPEAT_CRITICAL_MIN", "IBO_RCA_DAILY_CAP",
              "IBO_DOWN_CONSECUTIVE_FAILURES"):
        monkeypatch.delenv(k, raising=False)
    from core import alert_timing
    for _label, env, _d, _m in alert_timing.KEYS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("IBO_INTERNAL_DOMAINS", "infrabeat.com")
    monkeypatch.setattr(config_loader, "get_all_systems", lambda *a, **k: [dict(SYS), dict(PLAIN)])


def test_special_lists_and_empty_means_normal():
    from core import recipients
    assert recipients.special(SYS, "down") == ["noc@infrabeat.com", "staff.b@infrabeat.com"]
    assert recipients.special(PLAIN, "down") == []


def test_reachability_uses_own_people_and_own_interval():
    from core import reachability as r
    assert r._who_gets(SYS, r.SYSTEM_DOWN, "manager") == (["noc@infrabeat.com", "staff.b@infrabeat.com"], [])
    assert r._who_gets(SYS, r.VPN_DOWN, "basis") == (["staff.e@infrabeat.com"], [])
    to, cc = r._who_gets(PLAIN, r.SYSTEM_DOWN, "manager")
    assert to == ["staff.a@infrabeat.com"] and cc == ["staff.d@infrabeat.com"]
    assert r._repeat_seconds(SYS, r.SYSTEM_DOWN, 300) == 600
    assert r._repeat_seconds(SYS, r.CREDENTIALS, 3600) == 45 * 60
    assert r._repeat_seconds(PLAIN, r.CREDENTIALS, 3600) == 3600          # unchanged default
    assert r.down_after("BW_PRD") == 3 and r.down_after("PLAIN") == 2


def test_value_recovery_goes_to_its_own_people():
    from core import alert_engine
    got = alert_engine.mail_recipients(SYS, {"kind": "recovery", "grade": "YELLOW"})
    assert got == {"to": ["shift.lead@infrabeat.com"], "cc": []}


def test_value_alert_timing_per_system():
    from core import alert_engine as ae
    assert ae.repeat_minutes("CRITICAL", "BW_PRD") == 30 and ae.repeat_minutes("WARNING", "BW_PRD") == 240
    assert ae.repeat_minutes("CRITICAL", "PLAIN") == 60
    assert ae.escalate_window_minutes("BW_PRD") == 90 and ae.escalate_window_minutes("PLAIN") == 120
    h = ae.hysteresis("BW_PRD", "sap.st06.cpu")
    assert h["raise_after"] == 2 and h["recovery_min_minutes"] == 15.0


def test_rca_cap_per_system():
    from core import rca_trigger
    assert rca_trigger.daily_cap("BW_PRD") == 2 and rca_trigger.daily_cap("PLAIN") == 4


def test_preview_shows_own_people_and_timing():
    from core import mail_explain
    out = mail_explain.preview(PLAIN, {"down_emails": "noc@infrabeat.com",
                                       "alert_timing": {"repeat_critical_min": "20", "down_repeat_min": "15"}})
    rules = {r["when"]: r for r in out["rules"]}
    assert rules["System NOT REACHABLE / DOWN"]["to"] == ["System down list"]
    assert "every 15 min" in rules["System NOT REACHABLE / DOWN"]["how_often"]
    assert rules["The same value is still bad (reminder)"]["how_often"].startswith("every 20 min")
    assert rules["A value is back to normal"]["to"] == ["same people as the alert"]
    assert out["down"]["addresses"] == ["noc@infrabeat.com"]
    assert {t["key"] for t in out["timing"]} >= {"down_after", "rca_daily_cap"}


def test_timing_clean_drops_blanks_and_floors():
    from core import alert_timing
    assert alert_timing.clean({"raise_after": "0", "down_repeat_min": "", "access_repeat_min": "1",
                               "bogus": 5}) == {"raise_after": 1, "access_repeat_min": 5}


def test_systems_form_saves_and_reads_back(monkeypatch, tmp_path):
    from core import config_loader, config_editor as ce
    monkeypatch.undo()                       # real get_all_systems for this one
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    (tmp_path / "systems.yaml").write_text("systems: []\n", encoding="utf-8")
    monkeypatch.setattr(config_loader, "_env_path", lambda: str(tmp_path / ".env"))
    config_loader.invalidate_systems_cache()
    base = {"name": "QAS", "client": "100", "rfc_ashost": "10.0.0.1", "rfc_username": "mon",
            "rfc_password": "s3cret", "has_gui_access": False}
    ce.save_system(dict(base, down_emails="noc@infrabeat.com", access_emails="",
                        alert_timing={"down_repeat_min": "10", "rca_daily_cap": ""}))
    ex = ce.system_extras("QAS")
    assert ex["down_emails"] == "noc@infrabeat.com" and ex["access_emails"] == ""
    assert ex["alert_timing"] == {"down_repeat_min": 10}
    ce.save_system(dict(base), editing=True)                     # form without them keeps them
    assert ce.system_extras("QAS")["alert_timing"] == {"down_repeat_min": 10}
    errors, _ = ce.validate_system(dict(base, name="Q2", access_emails="bad-address"))
    assert any("VPN / logon" in e for e in errors)


def test_critical_not_held_by_everyday_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_MAIL_CAPS", "1")          # limits switched on for this test
    from core import alert_ledger as al, store
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setattr(al, "_mail_outage", lambda s: "")
    monkeypatch.setattr(al, "_gate", lambda *a: None)
    monkeypatch.setattr(al, "history", lambda *a, **k: None)
    monkeypatch.setattr(al, "_whatsapp_copy", lambda *a, **k: None)
    monkeypatch.setenv("IBO_MAIL_CAP_PER_SYSTEM", "3")
    monkeypatch.setenv("IBO_MAIL_CAP_CRITICAL", "5")
    for i in range(3):
        assert al.should_send("PLAIN", "metric", f"w{i}", 0, 0, severity="WARNING")[0]
        al.record("PLAIN", "metric", f"w{i}", "WARNING", "x", "a@infrabeat.com", delivered=True,
                  mirror=False, hooks=False)
    ok, why = al.should_send("PLAIN", "metric", "w9", 0, 0, severity="WARNING")
    assert not ok and "suppressed-cap" in why
    ok, why = al.should_send("PLAIN", "metric", "c1", 0, 0, severity="CRITICAL")
    assert ok, why                                                        # CRITICAL still goes
    for i in range(3):
        al.record("PLAIN", "metric", f"c{i+2}", "CRITICAL", "x", "a@infrabeat.com", delivered=True,
                  mirror=False, hooks=False)
    ok, why = al.should_send("PLAIN", "metric", "c9", 0, 0, severity="CRITICAL")
    assert not ok and "IBO_MAIL_CAP_CRITICAL" in why                      # flood guard


def test_caps_rise_with_outlook_and_per_system(monkeypatch):
    from core import alert_ledger as al
    monkeypatch.delenv("IBO_MAIL_CAP_PER_SYSTEM", raising=False)
    monkeypatch.delenv("IBO_MAIL_CAP_TOTAL", raising=False)
    monkeypatch.delenv("IBO_MAIL_TRANSPORT", raising=False)
    assert al.cap_per_system() == 40 and al.cap_total() == 300
    monkeypatch.setenv("IBO_MAIL_TRANSPORT", "graph")
    assert al.cap_per_system() == 150 and al.cap_total() == 1500
    SYS["alert_timing"]["mail_cap_per_day"] = 80
    try:
        assert al.cap_per_system("BW_PRD") == 80 and al.cap_per_system("PLAIN") == 150
    finally:
        SYS["alert_timing"].pop("mail_cap_per_day")
