"""
Review fix 3 (+ 8a, 8b): while the main account is suspended after a quota
answer, critical mail and the report are still tried (secondary, then the
main account anyway), critical kinds get a WhatsApp copy, a report that
still fails is re-queued by the outbox, send_message() strips client
addresses, and a delivery marked "timeout" is updated when it finishes.
"""
import os
import smtplib
import sys
import threading
import time
from email.mime.text import MIMEText

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_fix_alerts import fx  # noqa: E402,F401
from test_fix_alerts_smtp import FakeServer, QUOTA  # noqa: E402


@pytest.fixture
def smtp(fx, monkeypatch):
    FakeServer.instances, FakeServer.behaviour = [], []
    monkeypatch.setattr(smtplib, "SMTP", FakeServer)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return {"host": "smtp.test.invalid", "port": 587, "username": "u", "password": "p",
            "from_email": "ibops@infrabeat.com", "account": "primary"}


def _msg(to="a@infrabeat.com", cc=None):
    m = MIMEText("x")
    m["Subject"] = "t"
    m["From"] = "ibops@infrabeat.com"
    m["To"] = to
    if cc:
        m["Cc"] = cc
    return m


def _suspend(smtp):
    from notifications import mailer
    mailer.suspend(smtp, "550 5.4.5 Daily user sending limit exceeded")
    assert mailer.mail_suspended(smtp)


@pytest.mark.parametrize("kind,severity", [("system_down", ""), ("critical", "CRITICAL"),
                                           ("credentials", ""), ("vpn_down", ""),
                                           ("report", "")])
def test_suspended_main_account_is_tried_anyway_and_success_lifts_it(smtp, kind, severity):
    from notifications import mailer
    _suspend(smtp)
    ok, reason = mailer.transmit(_msg(), smtp, ["a@infrabeat.com"], kind=kind, severity=severity)
    assert ok, reason
    assert [s.host for s in FakeServer.instances] == ["smtp.test.invalid"]
    assert not mailer.mail_suspended(smtp)


def test_secondary_first_then_main(smtp, monkeypatch):
    from notifications import mailer
    second = {"host": "smtp.company.invalid", "port": 25, "username": "", "password": "",
              "from_email": "alt@infrabeat.com", "account": "company", "use_tls": False}
    monkeypatch.setattr(mailer, "secondary_smtp", lambda system=None: second)
    _suspend(smtp)
    FakeServer.behaviour = [smtplib.SMTPDataError(554, b"5.7.1 rejected"), None]
    ok, reason = mailer.transmit(_msg(), smtp, ["a@infrabeat.com"], kind="system_down")
    assert ok, reason
    assert [s.host for s in FakeServer.instances] == ["smtp.company.invalid", "smtp.test.invalid"]


def test_still_failing_report_reports_every_step(smtp):
    from notifications import mailer
    _suspend(smtp)
    FakeServer.behaviour = [QUOTA]
    ok, reason = mailer.transmit(_msg(), smtp, ["a@infrabeat.com"], kind="report")
    assert not ok
    assert "suppressed-mail-outage" in reason and "no secondary account" in reason
    assert "main account" in reason
    assert mailer.mail_suspended(smtp)                 # renewed


def test_non_critical_is_still_suppressed(smtp):
    from notifications import mailer
    _suspend(smtp)
    ok, reason = mailer.transmit(_msg(), smtp, ["a@infrabeat.com"], kind="tcode_failure")
    assert not ok and reason.startswith("suppressed-mail-outage")
    assert FakeServer.instances == []


def test_whatsapp_copy_for_critical_during_outage(fx, monkeypatch):
    from core import alert_ledger as L
    from core.config_loader import get_smtp_config
    from notifications import mailer
    copies = []
    monkeypatch.setattr(L, "_whatsapp_copy", lambda *a, **k: copies.append(a[:2]))
    mailer.suspend(get_smtp_config(None, require_recipients=False), "550 5.4.5 limit")
    ok, _ = L.should_send("PS4", "system_down", "PS4")
    assert ok and copies == [("PS4", "system_down")]
    ok, _ = L.should_send("PS4", "tcode_failure", "x")
    assert not ok and len(copies) == 1                 # non-critical: no WhatsApp


def test_send_message_whatsapp_copy_for_critical_during_outage(smtp, monkeypatch):
    from core import alert_ledger as L
    from notifications import mailer
    copies = []
    monkeypatch.setattr(L, "_whatsapp_copy", lambda *a, **k: copies.append(a[:2]))
    _suspend(smtp)
    mailer.send_message(_msg(), smtp, ["a@infrabeat.com"], system="PS4", kind="diagnostic",
                        severity="CRITICAL")
    assert copies == [("PS4", "diagnostic")]


# ------------------------------------------------------------------ 8a

def test_send_message_strips_client_addresses_for_internal_kinds(smtp):
    from notifications import mailer
    msg = _msg(to="basis@infrabeat.com, client@customer.com", cc="boss@customer.com")
    ok = mailer.send_message(msg, smtp, ["basis@infrabeat.com", "client@customer.com",
                                         "boss@customer.com"], system="PS4", kind="diagnostic")
    assert ok
    server = FakeServer.instances[-1]
    assert server.sent == [("ibops@infrabeat.com", ["basis@infrabeat.com"])]
    assert "customer.com" not in str(msg.get("To")) and msg.get("Cc") is None


def test_send_message_keeps_client_on_the_report(smtp):
    from notifications import mailer
    msg = _msg(to="client@customer.com")
    assert mailer.send_message(msg, smtp, ["client@customer.com"], system="PS4", kind="report")
    assert FakeServer.instances[-1].sent == [("ibops@infrabeat.com", ["client@customer.com"])]


def test_send_message_with_only_client_addresses_is_not_sent(smtp):
    from notifications import mailer
    assert not mailer.send_message(_msg(to="client@customer.com"), smtp, ["client@customer.com"],
                                   system="PS4", kind="rca")
    assert FakeServer.instances == [] and "no recipients" in mailer.last_reason()


# ------------------------------------------------------------------ outbox

@pytest.fixture
def box(tmp_path, monkeypatch):
    from core import outbox
    monkeypatch.setattr(outbox, "OUTBOX_DIR", str(tmp_path / "outbox"))
    return outbox


def _entry(outbox, entry_id):
    import json
    with open(outbox._path(entry_id), encoding="utf-8") as fh:
        return json.load(fh)


def test_report_failing_in_outage_is_requeued_and_sent_later(box, monkeypatch):
    calls = []
    monkeypatch.setattr(box, "_outage_delay", lambda reason: 0.05)

    def send():
        calls.append(1)
        return len(calls) >= 2                       # fails during the outage, then works
    e = box.enqueue("final_report", "PS4", send, run_id="r1")
    assert box.drain(timeout=5)
    deadline = time.time() + 5
    while _entry(box, e["id"])["status"] != "sent" and time.time() < deadline:
        time.sleep(0.02)
        box.drain(timeout=1)
    rec = _entry(box, e["id"])
    assert rec["status"] == "sent" and rec["retries"] == 1 and len(calls) == 2


def test_report_outage_retries_are_bounded(box, monkeypatch):
    monkeypatch.setenv("IBO_REPORT_OUTAGE_RETRIES", "2")
    monkeypatch.setattr(box, "_outage_delay", lambda reason: 0.02)
    calls = []
    e = box.enqueue("final_report", "PS4", lambda: (calls.append(1), False)[1])
    deadline = time.time() + 5
    while time.time() < deadline:
        box.drain(timeout=1)
        rec = _entry(box, e["id"])
        if rec["status"] == "failed":
            break
        time.sleep(0.02)
    assert rec["status"] == "failed" and rec["retries"] == 2 and len(calls) == 3


def test_non_report_failure_is_not_requeued(box, monkeypatch):
    monkeypatch.setattr(box, "_outage_delay", lambda reason: 0.01)
    e = box.enqueue("critical_alert", "PS4", lambda: False)
    assert box.drain(timeout=5)
    assert _entry(box, e["id"])["status"] == "failed"


def test_report_failing_for_another_reason_is_not_requeued(box, monkeypatch):
    monkeypatch.setattr(box, "_outage_delay", lambda reason: None)
    e = box.enqueue("final_report", "PS4", lambda: False)
    assert box.drain(timeout=5)
    assert _entry(box, e["id"])["status"] == "failed"


def test_outage_delay_follows_the_suspension(fx):
    from core import outbox
    from core.config_loader import get_smtp_config
    from notifications import mailer
    assert outbox._outage_delay("permanent 554 rejected") is None
    mailer.suspend(get_smtp_config(None, require_recipients=False), "550 5.4.5 limit")
    d = outbox._outage_delay("suppressed-mail-outage: x")
    assert 60 * 60 <= d <= 62 * 60


def test_timeout_entry_is_updated_when_the_send_finishes_late(box, monkeypatch):
    monkeypatch.setattr(box, "timeout_seconds", lambda: 0.1)
    release = threading.Event()
    e = box.enqueue("final_report", "PS4", lambda: release.wait(5) or True)
    assert box.drain(timeout=5)
    assert _entry(box, e["id"])["status"] == "timeout"
    release.set()
    deadline = time.time() + 5
    while _entry(box, e["id"])["status"] == "timeout" and time.time() < deadline:
        time.sleep(0.02)
    rec = _entry(box, e["id"])
    assert rec["status"] == "sent" and rec["late"] is True
