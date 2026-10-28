"""First breach -> consultants (yellow); repeat on the next sweep -> + manager (red)."""

import smtplib
from email import message_from_string
from email.header import decode_header, make_header

import core.escalation as esc
from core.models import MetricResult, MonitoringResult, Status
from notifications import email_alert


def _m(name, status=Status.CRITICAL):
    return MetricResult(name=name, value=1, display_value="1", status=status)


def test_yellow_then_red_then_reset(tmp_path):
    path = str(tmp_path / "state.json")
    a = esc.assess("QAS", ["cpu"], state_path=path)
    assert a.level == "yellow" and a.first_time == ["cpu"]
    a = esc.assess("QAS", ["cpu", "dumps"], state_path=path)
    assert a.level == "red" and a.repeated == ["cpu"] and a.first_time == ["dumps"]
    # cpu recovered, dumps still critical -> dumps now repeated
    a = esc.assess("QAS", ["dumps"], state_path=path)
    assert a.repeated == ["dumps"] and "cpu" not in a.counts
    # all clear, then a fresh breach starts at yellow again
    esc.assess("QAS", [], state_path=path)
    assert esc.assess("QAS", ["cpu"], state_path=path).level == "yellow"


def test_state_survives_restart_and_corrupt_file(tmp_path):
    path = str(tmp_path / "state.json")
    esc.assess("PRD", ["cpu"], state_path=path)
    assert esc.assess("PRD", ["cpu"], state_path=path).level == "red"   # re-read from disk
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    assert esc.assess("PRD", ["cpu"], state_path=path).level == "yellow"  # degrades, no crash


def test_email_adds_manager_only_on_repeat(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_LEGACY_VALUE_MAILS", "1")      # the old combined mail, switched back on
    # Internal alerts never reach the client (Oct 2026): the addresses below
    # must be declared ours, or the transport removes them as the client's.
    monkeypatch.setenv("IBO_INTERNAL_DOMAINS", "x.com")
    monkeypatch.delenv("IBO_CLIENT_EMAILS", raising=False)
    monkeypatch.setattr(esc, "STATE_PATH", str(tmp_path / "s.json"))
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, frm, to, body):
            subject = str(make_header(decode_header(message_from_string(body)["Subject"])))
            sent.append((list(to), subject))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    smtp = {"host": "h", "port": 587, "username": "u", "password": "p",
            "from_email": "ibops@x.com", "to_emails": ["basis@x.com"],
            "cc_emails": [], "manager_emails": ["manager@x.com"]}
    result = MonitoringResult(system="QAS", client="100",
                              metrics=[_m("cpu")], overall_status=Status.CRITICAL)

    assert email_alert.send_critical_alert(result, smtp)
    assert sent[0][0] == ["basis@x.com"]
    assert "first occurrence" in sent[0][1]

    assert email_alert.send_critical_alert(result, smtp)
    assert sent[1][0] == ["basis@x.com", "manager@x.com"]
    assert "escalated to manager" in sent[1][1]
