"""
Finding 5: notifications/router.py (deliver_alert / deliver_profile_result,
wrong send_final_report signature) was dead code -- removed.
Finding 8: SAP / AI text entered mail HTML unescaped.
Finding 10: the job-owner key held the job count (new every run).
Finding 11: "today" keys use the SAP server's date (injectable clock).
"""
import importlib
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_fix_alerts import fx  # noqa: E402,F401

EVIL = "<img src=x onerror=alert(1)>"


def test_router_dead_code_is_gone():
    with pytest.raises(ImportError):
        importlib.import_module("notifications.router")
    from notifications import email_report
    import inspect
    assert list(inspect.signature(email_report.send_final_report).parameters)[:4] == \
        ["result", "smtp_config", "pdf_path", "excel_path"]


def test_value_alert_html_is_escaped(fx):
    job = {"system": "PS4", "metric": "cpu", "instance": "", "label": EVIL, "level": "WARNING",
           "kind": "alert", "value": EVIL, "limits": "", "detail": EVIL, "grade": "YELLOW"}
    _subject, html, _text = fx.E.render_email(job)
    assert "<img" not in html and "&lt;img" in html


def test_job_owner_body_escaped_and_key_stable_across_runs():
    from notifications import job_owner
    job = {"job": f"Z_JOB{EVIL}", "scheduled_by": EVIL, "user": "U1", "started_at": "09:00",
           "ended_at": "09:05", "duration_text": "5 min", "job_count": "09001234"}
    subject, html = job_owner._body("PS4", job, "cancelled")
    assert "<img" not in html and "&lt;img" in html
    assert EVIL in subject                       # plain-text subject is not escaped
    k1 = job_owner.notify_key("PS4", dict(job, job_count="09001234"), "cancelled", "2026-10-23")
    k2 = job_owner.notify_key("PS4", dict(job, job_count="10003456"), "cancelled", "2026-10-23")
    assert k1 == k2 and "2026-10-23" in k1
    assert job_owner.notify_key("PS4", job, "cancelled", "2026-10-24") != k1


def test_job_owner_cc_is_basis_never_the_client(fx, monkeypatch):
    from notifications import job_owner, mailer
    monkeypatch.setenv("IBO_OWNER_MAIL_APPROVAL", "0")          # old path: send at once
    monkeypatch.setenv("NOTIFY_JOB_OWNERS", "true")
    monkeypatch.setattr(job_owner, "_state_path", lambda: str(fx.tmp / "jo.json"))
    got = []
    monkeypatch.setattr(mailer, "deliver", lambda subject, html, to, **k: got.append((to, k.get("cc"))) or True)
    detail = {"cancelled": [{"job": "ZJ", "owner": "MEERA", "owner_email": "meera@customer.com",
                             "job_count": "1"}]}
    out = job_owner.notify_job_owners("PS4", detail, {"to_emails": ["client@customer.com"]})
    assert out["sent"] == 1
    to, cc = got[0]
    assert to == ["meera@customer.com"] and cc == ["basis@infrabeat.com"]
    # the same job failing again (new job count) the same SAP day: no second mail
    detail["cancelled"][0]["job_count"] = "2"
    assert job_owner.notify_job_owners("PS4", detail, {})["sent"] == 0


def test_dump_owner_and_report_html_escaped():
    from notifications import dump_owner, email_report
    from core.models import MonitoringResult, AIAnalysis, Status
    v = {"owner_team": "ABAP", "verdict": "single_hog", "culprit": EVIL, "memory_dumps": "5",
         "programs": "1", "_evidence": ["hdr", f"  {EVIL} took the memory"]}
    _s, html = dump_owner._body("PRD", v, SimpleNamespace(title=EVIL, first_seen=None, incident_id="I1"))
    assert "<img" not in html
    r = MonitoringResult(system="PS4", client=EVIL, overall_status=Status.WARNING)
    r.ai_analysis = AIAnalysis(severity="WARNING", likely_root_cause=EVIL, confidence="LOW", raw_response="x")
    html = email_report._build_report_html(r)
    assert "<img" not in html and "&lt;img" in html


def test_rca_and_approval_html_escaped(fx):
    from notifications import rca_email
    html = rca_email.build_rca_html({"severity": EVIL, "headline": EVIL, "root_cause": EVIL,
                                     "culprits": [{"type": "user", "name": EVIL, "evidence": EVIL}]}, EVIL)
    assert "<img" not in html
    from core import job_user_mail, ops_db
    rid = ops_db.execute("INSERT INTO job_user_requests(system, user, email, jobs, created_at) "
                         "VALUES (?,?,?,?,?)", ("PS4", EVIL, "", "[]", ops_db.now()))
    assert "<img" not in job_user_mail.button_html(rid, ["basis@infrabeat.com"])


def test_sap_clock_is_injectable_and_used_for_today(monkeypatch):
    from core import sap_clock
    sap_clock.set_provider(lambda system: datetime(2026, 10, 24, 0, 30) if system == "PS4" else None)
    try:
        assert sap_clock.sap_today("PS4") == "2026-10-24"
        from notifications import dump_owner
        assert dump_owner._today("PS4:ABAP:ZX") == "2026-10-24"
    finally:
        sap_clock.set_provider(None)
