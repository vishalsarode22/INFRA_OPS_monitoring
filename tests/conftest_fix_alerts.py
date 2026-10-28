"""
Shared fixture for tests/test_fix_alerts_*.py: a private config folder and
database, a controllable clock, and an environment that does not depend on
the developer's .env (which python-dotenv finds from a worktree too).
No network: SMTP / WhatsApp / Gemini are replaced in each test.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SYSTEMS = ("PS4", "CEAT S4P")

CLEAN_ENV = ("IBO_ALERT_RAISE_AFTER", "IBO_ALERT_RAISE_MINUTES", "IBO_ALERT_CLEAR_AFTER",
             "IBO_ALERT_CLEAR_PCT", "IBO_ALERT_RECOVERY_MIN_MINUTES", "IBO_ALERT_ESCALATE_WINDOW_MIN",
             "IBO_ALERT_REPEAT_CRITICAL_MIN", "IBO_ALERT_REPEAT_WARNING_MIN",
             "IBO_DISABLED_MAIL_KINDS", "IBO_MAIL_CAP_PER_SYSTEM", "IBO_MAIL_CAP_TOTAL",
             "IBO_ALERT_DAILY_CAP", "IBO_CLIENT_EMAILS", "IBO_INTERNAL_ADDRESSES",
             "IBO_SECONDARY_SMTP_ACCOUNT", "IBO_MAIL_SUSPEND_MINUTES", "IBO_SMTP_RETRIES",
             "IBO_SMTP_TIMEOUT", "IBO_MAIL_ASYNC", "IBO_DOWN_CONSECUTIVE_FAILURES",
             "ALERT_CC_EMAILS", "IBO_PUBLIC_URL", "IBO_MAIL_BUTTONS_ALWAYS", "IBO_ROLE",
             "IBO_LEGACY_VALUE_MAILS", "WHATSAPP_ENABLED", "IBO_WHATSAPP_ALERT_KINDS")


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self):
        return self.now

    def tick(self, minutes: float):
        self.now += timedelta(minutes=minutes)


@pytest.fixture
def fx(tmp_path, monkeypatch):
    from core import config_loader, thresholds as T, timeline, store, ops_db, alert_engine as E
    for k in CLEAN_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("IBO_ENABLE_EMAIL", "1")
    monkeypatch.setenv("IBO_INTERNAL_DOMAINS", "infrabeat.com")
    monkeypatch.setenv("IBO_BASIS_EMAILS", "basis@infrabeat.com")
    monkeypatch.setenv("ALERT_MANAGER_EMAILS", "manager@infrabeat.com")
    monkeypatch.setenv("ALERT_TO_EMAILS", "client@customer.com,basis@infrabeat.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
    monkeypatch.setenv("SMTP_USERNAME", "ibops@infrabeat.com")
    monkeypatch.setenv("SMTP_PASSWORD", "x")
    monkeypatch.setenv("ALERT_FROM_EMAIL", "ibops@infrabeat.com")
    (tmp_path / "thresholds.yaml").write_text(
        "cpu:\n  warning: 80\n  critical: 90\n"
        "sap.sm58.entries_today:\n  warning: 100\n  critical: 250\n", encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_loader, "get_all_systems",
                        lambda *a, **k: [{"name": s} for s in SYSTEMS])
    monkeypatch.setattr(T, "_known_system", lambda n: n in SYSTEMS)
    monkeypatch.setattr(timeline, "add", lambda *a, **k: None)
    try:
        from core import oncall
        monkeypatch.setattr(oncall, "basis_emails", lambda *a, **k: [])
    except Exception:
        pass
    if hasattr(store._local, "conn"):
        store._local.conn.close()
        del store._local.conn
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t.sqlite"))
    store._initialised = False
    ops_db.reset_for_tests()
    T._cache.clear()
    clock = Clock(datetime(2026, 10, 9, 10, 0))
    monkeypatch.setattr(E, "_now", clock)
    E._live_seen.clear()
    from notifications import mailer
    mailer._mem_suspend.clear()

    class Box:
        pass
    box = Box()
    box.tmp, box.clock, box.E, box.T = tmp_path, clock, E, T
    box.sent = []
    box.cfg = {"name": "PS4"}

    def feed(status, value, key="cpu", source="live", **extra):
        it = {"key": key, "label": key, "value": value, "status": status,
              "warning": extra.pop("warning", 80), "critical": extra.pop("critical", 90),
              "direction": "high", "unit": ""}
        it.update(extra)
        return E.observe(box.cfg, [it], source=source, sender=lambda cfg, job: box.sent.append(job))
    box.feed = feed
    yield box
    if hasattr(store._local, "conn"):
        store._local.conn.close()
        del store._local.conn
    store._initialised = False
    ops_db.reset_for_tests()
    T._cache.clear()
