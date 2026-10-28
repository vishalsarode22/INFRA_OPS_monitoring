"""
Finding 2: one delivery gate for every alert / notification -- dedup,
cooldown on disk, per-system and total caps, recipients by grade from
alert_teams.yaml, the client hard-excluded from internal kinds, and every
decision recorded in Alert history with its reason.
Finding 4: the two-copy value mail shares one gate decision; each copy is
recorded.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_fix_alerts import fx  # noqa: E402,F401


def _history(fx, system="PS4"):
    return fx.E.history(system)


def test_policy_switches_off_pipeline_failed_and_report_held(fx):
    from core import alert_ledger as L
    for kind in ("sweep_failure", "report_held"):
        ok, why = L.should_send("PS4", kind, "1")
        assert not ok and why.startswith("suppressed-policy")
    notes = [r["note"] for r in _history(fx)]
    assert sum("suppressed-policy" in n for n in notes) == 2


def test_failure_notice_is_not_mailed(fx, monkeypatch):
    from notifications import email_report, mailer
    calls = []
    monkeypatch.setattr(mailer, "_smtp_once", lambda *a, **k: calls.append(a))
    assert not email_report.send_failure_alert("PS4", "boom", 1, 2, {"to_emails": ["basis@infrabeat.com"]})
    assert calls == []
    assert any(r["metric"] == "sweep_failure" and "suppressed-policy" in r["note"] for r in _history(fx))


def test_dedup_and_caps_with_reasons(fx, monkeypatch):
    monkeypatch.setenv("IBO_MAIL_CAPS", "1")          # limits switched on for this test
    from core import alert_ledger as L
    ok, _ = L.should_send("PS4", "vpn_down", "net", min_interval_s=1800)
    assert ok
    L.record("PS4", "vpn_down", "net", "WARNING", "s", ["basis@infrabeat.com"])
    ok, why = L.should_send("PS4", "vpn_down", "net", min_interval_s=1800)
    assert not ok and why.startswith("suppressed-dedup")
    monkeypatch.setenv("IBO_MAIL_CAP_PER_SYSTEM", "2")
    assert L.should_send("PS4", "credentials", "a", daily_cap=0)[0]
    L.record("PS4", "credentials", "a", "WARNING", "s", ["b@infrabeat.com"])
    ok, why = L.should_send("PS4", "not_reachable", "b", daily_cap=0)
    assert not ok and why.startswith("suppressed-cap") and "IBO_MAIL_CAP_PER_SYSTEM" in why
    monkeypatch.setenv("IBO_MAIL_CAP_PER_SYSTEM", "0")
    monkeypatch.setenv("IBO_MAIL_CAP_TOTAL", "2")
    ok, why = L.should_send("CEAT S4P", "tcode_failure", "c", daily_cap=0)
    assert not ok and "IBO_MAIL_CAP_TOTAL" in why
    # CRITICAL kinds (credentials, VPN, DOWN, CRITICAL values) are not held by
    # the everyday caps -- only by the IBO_MAIL_CAP_CRITICAL flood guard.
    assert L.should_send("CEAT S4P", "credentials", "c", daily_cap=0)[0]
    notes = " ".join(r["note"] for r in _history(fx, None) if not r["ok"])
    assert "suppressed-dedup" in notes and "suppressed-cap" in notes
    # the sent one is in Alert history too
    assert any(r["ok"] and r["metric"] == "vpn_down" for r in _history(fx))


def test_default_caps_are_well_below_gmail(fx):
    from core import alert_ledger as L
    assert L.cap_per_system() == 40 and L.cap_total() == 300


def test_mail_outage_suppresses_non_critical_only(fx):
    from core import alert_ledger as L
    from core.config_loader import get_smtp_config
    from notifications import mailer
    mailer.suspend(get_smtp_config(None, require_recipients=False), "550 5.4.5 Daily user sending limit")
    ok, why = L.should_send("PS4", "tcode_failure", "x")
    assert not ok and why.startswith("suppressed-mail-outage")
    ok, _ = L.should_send("PS4", "system_down", "PS4")
    assert ok                                      # CRITICAL goes on (secondary / WhatsApp)
    ok, _ = L.should_send("PS4", "credentials", "x")
    assert ok                                      # credentials / VPN alerts are critical too
    assert any("suppressed-mail-outage" in r["note"] for r in _history(fx))


def test_recipients_by_grade_and_never_the_client(fx):
    E = fx.E
    cfg = {"name": "PS4", "alert_receivers": "client@customer.com"}
    y = E.mail_recipients(cfg, {"grade": "YELLOW", "kind": "alert"})
    assert y == {"to": ["basis@infrabeat.com"], "cc": []}
    o = E.mail_recipients(cfg, {"grade": "ORANGE", "kind": "repeat"})
    assert o == {"to": ["basis@infrabeat.com"], "cc": ["manager@infrabeat.com"]}   # Basis acts, manager in copy
    # a team list that (wrongly) contains the client's address loses it
    (fx.tmp / "alert_teams.yaml").write_text(
        "standard:\n  Basis: [basis@infrabeat.com]\n  ABAP: [abap@infrabeat.com, basis1@client-a.example]\n",
        encoding="utf-8")
    cfg2 = dict(cfg, alert_receivers="client@customer.com,basis1@client-a.example")
    t = E.mail_recipients(cfg2, {"grade": "YELLOW", "kind": "alert", "teams": ["ABAP"]})
    assert "basis1@client-a.example" not in t["to"] + t["cc"] and "abap@infrabeat.com" in t["to"]


def test_transport_strips_client_from_internal_kinds_only(fx):
    from notifications import mailer
    to, cc, removed = mailer.without_client(["client@customer.com", "basis@infrabeat.com"],
                                            ["boss@customer.com"], "PS4", "system_down")
    assert to == ["basis@infrabeat.com"] and cc == [] and set(removed) == {"client@customer.com",
                                                                          "boss@customer.com"}
    to, cc, removed = mailer.without_client(["client@customer.com"], [], "PS4", "report")
    assert to == ["client@customer.com"] and removed == []


def test_explicit_client_list_without_domains(fx, monkeypatch):
    from core import recipients
    monkeypatch.delenv("IBO_INTERNAL_DOMAINS", raising=False)
    monkeypatch.setenv("IBO_CLIENT_EMAILS", "it@customer.com")
    kept, removed = recipients.strip_client(["it@customer.com", "basis@infrabeat.com"], {"name": "PS4"})
    assert kept == ["basis@infrabeat.com"] and removed == ["it@customer.com"]
    # report receivers that are not in any internal list count as the client
    assert "client@customer.com" in recipients.client({"name": "PS4"})


def test_value_mail_goes_through_the_gate_and_cap_is_recorded(fx, monkeypatch):
    monkeypatch.setenv("IBO_MAIL_CAPS", "1")          # limits switched on for this test
    from notifications import mailer
    sent = []
    monkeypatch.setattr(mailer, "send_html_now", lambda s, h, to, smtp, cc=None, **k: sent.append(to) or True)
    monkeypatch.setattr(mailer, "email_enabled", lambda: True)
    monkeypatch.setenv("IBO_MAIL_CAP_PER_SYSTEM", "1")
    job = {"system": "PS4", "metric": "cpu", "instance": "", "label": "CPU", "level": "WARNING",
           "kind": "alert", "value": "85", "limits": "warning ≥ 80", "channels": ["email"], "skey": "cpu"}
    fx.E._send_all({"name": "PS4"}, dict(job))
    fx.E._send_all({"name": "PS4"}, dict(job, metric="memory", skey="memory", label="Memory"))
    assert len(sent) == 1
    rows = _history(fx)
    held = [r for r in rows if r["metric"] == "memory"]
    assert held and not held[0]["ok"] and "suppressed-cap" in held[0]["note"]


def test_two_copies_one_decision_each_recorded(fx, monkeypatch):
    from notifications import mailer
    monkeypatch.setenv("IBO_PUBLIC_URL", "http://ibops.test")
    monkeypatch.setenv("IBO_INTERNAL_ADDRESSES", "user.d.personal.example@gmail.com")
    calls = []

    def fake(subject, html, to, smtp, cc=None, **k):
        calls.append(list(to) + list(cc or []))
        if "user.d.personal.example@gmail.com" in to:
            mailer._set_reason("permanent SMTPRecipientsRefused: 550 5.1.1 no such user")
            return False
        return True
    monkeypatch.setattr(mailer, "send_html_now", fake)
    monkeypatch.setattr(mailer, "email_enabled", lambda: True)
    gate_calls = []
    from core import alert_ledger
    real = alert_ledger.should_send
    monkeypatch.setattr(alert_ledger, "should_send",
                        lambda *a, **k: gate_calls.append(a) or real(*a, **k))
    job = {"system": "PS4", "metric": "cpu", "instance": "", "label": "CPU", "level": "WARNING",
           "kind": "alert", "value": "85", "limits": "warning ≥ 80", "channels": ["email"], "skey": "cpu"}
    # A private mailbox named in the team list is "ours" by address but not by
    # domain: our copy has buttons, the other copy goes without.
    (fx.tmp / "alert_teams.yaml").write_text(
        "standard:\n  Basis: [basis@infrabeat.com, user.d.personal.example@gmail.com]\n", encoding="utf-8")
    monkeypatch.setenv("IBO_INTERNAL_ADDRESSES", "")
    fx.E._send_all({"name": "PS4"}, job)
    assert len(gate_calls) == 1                     # one gate decision for both copies
    assert len(calls) == 2
    rows = [r for r in _history(fx) if r["metric"] == "cpu"]
    notes = sorted(r["note"] for r in rows)
    assert any(n.startswith("our copy: sent") for n in notes)
    assert any(n.startswith("copy without buttons: NOT sent") and "550 5.1.1" in n for n in notes)
