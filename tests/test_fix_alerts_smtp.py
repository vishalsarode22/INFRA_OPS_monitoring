"""
Finding 3: SMTP retried permanent 5xx answers (Gmail 550 5.4.5 daily limit)
and blocked callers (60 s timeout x 2 + sleeps). Now: no retry on 5xx, a
persisted "mail suspended" flag with a probe after 1 h, CRITICAL via a
secondary account, 20 s timeout, and a non-blocking deliver() / enqueue_mail().
No network: smtplib.SMTP is replaced.
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


class FakeServer:
    instances = []
    behaviour = []                      # one entry per connection: None = ok, or an exception

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        FakeServer.instances.append(self)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, frm, to, body):
        exc = FakeServer.behaviour.pop(0) if FakeServer.behaviour else None
        if exc is not None:
            raise exc
        self.sent.append((frm, list(to)))


@pytest.fixture
def smtp(fx, monkeypatch):
    FakeServer.instances, FakeServer.behaviour = [], []
    monkeypatch.setattr(smtplib, "SMTP", FakeServer)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return {"host": "smtp.test.invalid", "port": 587, "username": "u", "password": "p",
            "from_email": "ibops@infrabeat.com", "account": "primary"}


def _msg():
    m = MIMEText("x")
    m["Subject"] = "t"
    m["From"] = "ibops@infrabeat.com"
    return m


QUOTA = smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded.")


def test_permanent_5xx_is_not_retried(smtp):
    from notifications import mailer
    FakeServer.behaviour = [smtplib.SMTPDataError(554, b"5.7.1 rejected")]
    ok, reason = mailer.transmit(_msg(), smtp, ["a@infrabeat.com"])
    assert not ok and reason.startswith("permanent") and len(FakeServer.instances) == 1


def test_transient_is_retried_once_and_timeout_is_short(smtp):
    from notifications import mailer
    FakeServer.behaviour = [smtplib.SMTPServerDisconnected("dropped"), None]
    ok, _ = mailer.transmit(_msg(), smtp, ["a@infrabeat.com"])
    assert ok and len(FakeServer.instances) == 2
    assert FakeServer.instances[0].timeout == 20


def test_quota_suspends_persists_and_probes_once_after_the_hour(fx, smtp, monkeypatch):
    from notifications import mailer
    from datetime import datetime, timedelta
    FakeServer.behaviour = [QUOTA]
    ok, reason = mailer.transmit(_msg(), smtp, ["a@infrabeat.com"], kind="tcode_failure")
    assert not ok and len(FakeServer.instances) == 1
    assert mailer.mail_suspended(smtp)
    # persisted: survives a restart (the in-memory copy is dropped)
    mailer._mem_suspend.clear()
    assert mailer.mail_suspended(smtp)
    ok, reason = mailer.transmit(_msg(), smtp, ["a@infrabeat.com"], kind="tcode_failure")
    assert not ok and reason.startswith("suppressed-mail-outage") and len(FakeServer.instances) == 1
    # one hour later: one probe goes through and lifts the suspension
    rec = mailer.suspension(smtp)
    past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    mailer._meta_set(mailer._skey(smtp), dict({k: v for k, v in rec.items()
                                               if k not in ("until_dt", "active")}, until=past))
    ok, _ = mailer.transmit(_msg(), smtp, ["a@infrabeat.com"], kind="tcode_failure")
    assert ok and not mailer.mail_suspended(smtp)


def test_critical_goes_through_the_secondary_account(fx, smtp, monkeypatch):
    from notifications import mailer
    second = {"host": "smtp.company.invalid", "port": 25, "username": "", "password": "",
              "from_email": "ibops@infrabeat.com", "account": "company", "use_tls": False}
    monkeypatch.setattr(mailer, "secondary_smtp", lambda system=None: second)
    FakeServer.behaviour = [QUOTA, None]
    ok, _ = mailer.transmit(_msg(), smtp, ["mgr@infrabeat.com"], kind="system_down", severity="CRITICAL")
    assert ok and [s.host for s in FakeServer.instances] == ["smtp.test.invalid", "smtp.company.invalid"]
    # while suspended: critical skips the primary, non-critical is not sent at all
    ok, _ = mailer.transmit(_msg(), smtp, ["mgr@infrabeat.com"], kind="system_down")
    assert ok and FakeServer.instances[-1].host == "smtp.company.invalid"
    n = len(FakeServer.instances)
    ok, reason = mailer.transmit(_msg(), smtp, ["b@infrabeat.com"], kind="tcode_failure")
    assert not ok and "suppressed-mail-outage" in reason and len(FakeServer.instances) == n


def test_secondary_account_comes_from_delivery_accounts(fx, monkeypatch):
    from notifications import mailer
    (fx.tmp / "delivery_accounts.yaml").write_text(
        "email:\n- id: company\n  host: mail.company.local\n  port: 25\n  username: ''\n"
        "  from_email: ibops@infrabeat.com\nwhatsapp: []\n", encoding="utf-8")
    from core import delivery_accounts
    monkeypatch.setattr(delivery_accounts, "ACCOUNTS_PATH", str(fx.tmp / "delivery_accounts.yaml"))
    assert mailer.secondary_smtp() is None
    monkeypatch.setenv("IBO_SECONDARY_SMTP_ACCOUNT", "company")
    cfg = mailer.secondary_smtp()
    assert cfg["host"] == "mail.company.local" and cfg["account"] == "company"


def test_deliver_returns_at_once_and_sends_in_the_background(fx, smtp, monkeypatch):
    from notifications import mailer
    gate = threading.Event()

    class SlowServer(FakeServer):
        def sendmail(self, frm, to, body):
            gate.wait(5)
            super().sendmail(frm, to, body)
    monkeypatch.setattr(smtplib, "SMTP", SlowServer)
    t0 = time.monotonic()
    assert mailer.deliver("subj", "<p>x</p>", ["basis@infrabeat.com"], system="PS4", kind="credentials",
                          smtp_config=smtp)
    assert time.monotonic() - t0 < 1.0
    assert mailer.enqueue_mail is mailer.deliver
    gate.set()
    assert mailer.wait_idle(5)
    assert FakeServer.instances and FakeServer.instances[-1].sent


def test_send_html_now_records_the_reason(fx, smtp):
    from notifications import mailer
    FakeServer.behaviour = [smtplib.SMTPDataError(552, b"message too big")]
    assert not mailer.send_html_now("s", "<p>x</p>", ["a@infrabeat.com"], smtp)
    assert "552" in mailer.last_reason()


def test_jobs_runner_does_not_retry_a_permanent_failure(fx, monkeypatch):
    pytest.importorskip("services.common")
    from services import jobs
    from notifications import mailer
    from core import config_loader
    monkeypatch.setattr(config_loader, "get_systems", lambda *a, **k: [])
    monkeypatch.setattr(config_loader, "get_smtp_config", lambda *a, **k: {"host": "h"})
    monkeypatch.setattr(mailer, "send_html_now",
                        lambda **k: mailer._set_reason("permanent 550 x") or False)
    jobs._job_mail({"subject": "s", "html": "h", "to": ["a@infrabeat.com"], "system": "PS4"})  # no raise
    monkeypatch.setattr(mailer, "send_html_now",
                        lambda **k: mailer._set_reason("transient timeout") or False)
    with pytest.raises(RuntimeError):
        jobs._job_mail({"subject": "s", "html": "h", "to": ["a@infrabeat.com"], "system": "PS4"})
