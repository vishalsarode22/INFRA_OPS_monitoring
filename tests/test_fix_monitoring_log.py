"""
Monitoring log page: which monitoring ran, the mails sent for it (to whom /
why not), when it started and ended, and when the next run is due.
"""
from __future__ import annotations

import csv
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from core import monitoring_log as ml, run_history  # noqa: E402

PAGE = os.path.join(ROOT, "dashboard", "static", "monitoring_log.html")
SHELL = os.path.join(ROOT, "dashboard", "static", "shell.js")
NODE = shutil.which("node")
SYSTEMS = [{"name": "PS4", "customer": "Metro"}, {"name": "CEQ", "customer": "Centor"}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_DB_PATH", str(tmp_path / "state.sqlite"))
    monkeypatch.delenv("IBO_AUTH", raising=False)
    from core import store, ops_db, auth, outbox, timeline, config_loader
    if hasattr(store._local, "conn"):
        store._local.conn.close()
        del store._local.conn
    importlib.reload(store)
    ops_db.reset_for_tests()
    auth._failures.clear()
    auth._secret_cache.clear()
    monkeypatch.setattr(run_history, "DB_PATH", str(tmp_path / "rh.sqlite"))
    monkeypatch.setattr(run_history, "LEGACY_JSON", str(tmp_path / "run_history.json"))
    monkeypatch.setattr(outbox, "OUTBOX_DIR", str(tmp_path / "outbox"))
    os.makedirs(tmp_path / "outbox", exist_ok=True)
    monkeypatch.setattr(timeline, "add", lambda *a, **k: None)
    import dashboard.app as app_module
    monkeypatch.setattr(app_module, "get_systems", lambda: auth.filter_systems([dict(s) for s in SYSTEMS]))
    monkeypatch.setattr(config_loader, "get_all_systems", lambda *a, **k: [dict(s) for s in SYSTEMS])
    monkeypatch.setattr(app_module, "get_run_history_next", lambda: {
        "paused": False, "snoozed": False, "manual_hold": False, "items": [
            {"kind": "profile", "id": "ps4-full", "system": "PS4", "schedule": "every 2 h",
             "enabled": True, "next_run_at": "2026-10-23 14:00", "held": None, "last_run": None},
            {"kind": "schedule", "id": "Scheduler page", "system": "CEQ", "schedule": "daily at 07:00",
             "enabled": True, "next_run_at": "2026-10-24 07:00", "held": "parked until cleared",
             "last_run": None}]})
    yield app_module, auth, ops_db
    if hasattr(store._local, "conn"):
        store._local.conn.close()
        del store._local.conn
    monkeypatch.undo()
    importlib.reload(store)
    ops_db.reset_for_tests()


def _send(ops_db, at, system, metric, kind, recipients, ok, note, label="", value="", channel="email"):
    ops_db.execute(
        "INSERT INTO alert_sends(at, system, metric, instance, label, level, value, limits, kind, channel, "
        "recipients, ok, note, batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (at.strftime("%Y-%m-%d %H:%M:%S"), system, metric, "", label, "INFO", value, "", kind, channel,
         recipients, 1 if ok else 0, note, ""))


def _seed(ops_db):
    from core import outbox
    t0 = datetime.now().replace(microsecond=0) - timedelta(hours=1)
    run_history.record(run_id="run-ps4", system="PS4", profile_id="ps4-full", trigger="schedule",
                       planned_at=t0, started_at=t0, ended_at=t0 + timedelta(minutes=6), result="success",
                       screenshots_captured=12, next_run_at=t0 + timedelta(hours=2),
                       next_run_reason="schedule")
    run_history.record(run_id="run-ceq", system="CEQ", trigger="manual-system", started_at=t0,
                       ended_at=t0 + timedelta(minutes=2), result="failed", failed_stage="logon",
                       failure_reason="RFC logon refused")
    # the report of run-ps4 (written by mailer.send_message), a suppressed failure notice,
    # a live alert long after the run, and a CEQ mail
    _send(ops_db, t0 + timedelta(minutes=7), "PS4", "report", "sent",
          "client@client-c.example, ops@client-c.example | cc: basis@infrabeat.com", True,
          "InfraBeatOps Report - PS4", value="InfraBeatOps Report - PS4")
    _send(ops_db, t0 + timedelta(minutes=3), "PS4", "sweep_failure", "suppressed-policy", "", False,
          "suppressed-policy: sweep_failure mails are switched off (IBO_DISABLED_MAIL_KINDS)")
    _send(ops_db, t0 + timedelta(minutes=50), "PS4", "cpu", "alert", "basis@infrabeat.com", True,
          "", label="CPU", value="95")
    _send(ops_db, t0 + timedelta(minutes=1), "CEQ", "system_down", "sent", "basis@infrabeat.com", True,
          "[DOWN] CEQ")
    outbox._write({"id": "20261023-x1", "kind": "final_report", "system": "PS4", "run_id": "run-ps4",
                   "status": "sent", "queued_at": t0.isoformat(), "finished_at": None})
    outbox._write({"id": "20261023-x2", "kind": "whatsapp", "system": "PS4", "run_id": "run-ps4",
                   "status": "timeout", "error": "no answer within 300s", "queued_at": t0.isoformat()})
    return t0


def test_runs_have_their_emails_linked_and_next_runs_listed(env):
    app_module, auth, ops_db = env
    _seed(ops_db)
    c = TestClient(app_module.app)
    d = c.get("/api/monitoring-log", params={"days": 7}).json()
    assert d["recipients_hidden"] is False                         # loopback = local admin
    runs = {r["run_id"]: r for r in d["runs"]}
    ps4 = runs["run-ps4"]
    assert ps4["trigger_label"] == "Scheduled" and ps4["screenshots_captured"] == 12
    assert ps4["started_at"] and ps4["ended_at"] and ps4["duration_s"] == 360.0
    kinds = [(m["kind_label"], m["status"]) for m in ps4["emails"]]
    assert ("Report", "sent") in kinds and ("Pipeline failed", "suppressed") in kinds
    assert ("WhatsApp screenshots", "failed") in kinds              # outbox entry, by run_id
    assert all(m["kind"] != "alert" for m in ps4["emails"])         # live alert 50 min later: not this run
    rep = next(m for m in ps4["emails"] if m["kind_label"] == "Report")
    assert rep["to"] == ["client@client-c.example", "ops@client-c.example"] and rep["cc"] == ["basis@infrabeat.com"]
    assert rep["subject"] == "InfraBeatOps Report - PS4" and rep["outbox_status"] == "sent"
    sup = next(m for m in ps4["emails"] if m["status"] == "suppressed")
    assert "switched off" in sup["status_text"]
    assert ps4["emails_sent"] == 1
    assert runs["run-ceq"]["failed_stage"] == "logon"
    assert [m["kind_label"] for m in runs["run-ceq"]["emails"]] == ["SYSTEM DOWN"]
    # every decision in the period, newest first, including the live alert
    assert len(d["emails"]) == 4 and d["emails"][0]["kind"] == "alert"
    assert d["emails"][0]["channel_label"] == "Email"
    up = {u["system"]: u for u in d["upcoming"]}
    assert up["PS4"]["next_run_at"] == "2026-10-23 14:00" and up["PS4"]["schedule"] == "every 2 h"
    assert up["PS4"]["profile"] == "ps4-full" and up["PS4"]["state"] == "scheduled"
    assert up["CEQ"]["state"] == "parked"
    only = c.get("/api/monitoring-log", params={"system": "CEQ"}).json()
    assert {r["system"] for r in only["runs"]} == {"CEQ"} and {u["system"] for u in only["upcoming"]} == {"CEQ"}


def test_csv_has_an_emails_column(env):
    app_module, _auth, ops_db = env
    _seed(ops_db)
    r = TestClient(app_module.app).get("/api/monitoring-log.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    ps4 = next(x for x in rows if x["run_id"] == "run-ps4")
    assert "Report → client@client-c.example, ops@client-c.example cc basis@infrabeat.com (sent)" in ps4["emails"]


def test_viewer_allowed_customer_scope_enforced_recipients_admin_only(env, monkeypatch):
    app_module, auth, ops_db = env
    monkeypatch.setenv("IBO_AUTH", "1")
    _seed(ops_db)
    auth.add_user("metro", "metro-password-1", "viewer", "Metro")
    auth.add_user("ada", "admin-password!", "admin")
    v = TestClient(app_module.app)
    assert v.get("/api/monitoring-log").status_code == 401           # login required
    v.post("/login", data={"username": "metro", "password": "metro-password-1", "next": "/"},
           headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    r = v.get("/api/monitoring-log")
    assert r.status_code == 200
    d = r.json()
    assert {x["system"] for x in d["runs"]} == {"PS4"}
    assert {x["system"] for x in d["emails"]} == {"PS4"}
    assert {x["system"] for x in d["upcoming"]} == {"PS4"} and d["systems"] == ["PS4"]
    assert d["recipients_hidden"] is True
    assert all(not m["to"] and not m["cc"] for m in d["emails"])
    assert v.get("/api/monitoring-log", params={"system": "CEQ"}).status_code == 404
    assert "CEQ" not in v.get("/api/monitoring-log.csv").text
    assert v.get("/monitoring-log").status_code == 200
    a = TestClient(app_module.app)
    a.post("/login", data={"username": "ada", "password": "admin-password!", "next": "/"},
           headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    d = a.get("/api/monitoring-log").json()
    assert d["recipients_hidden"] is False and {x["system"] for x in d["runs"]} == {"PS4", "CEQ"}


def test_report_mail_is_recorded_with_to_cc_subject_and_status(env, monkeypatch):
    _app, _auth, _ops = env
    from notifications import mailer
    from core import alert_engine
    monkeypatch.setenv("IBO_ENABLE_EMAIL", "1")
    monkeypatch.setattr(mailer, "transmit", lambda *a, **k: (True, ""))
    msg = MIMEMultipart("mixed")
    msg["Subject"] = "InfraBeatOps Report - PS4"
    msg["From"] = "ibops@infrabeat.com"
    msg["To"] = "client@client-c.example, ops@client-c.example"
    msg["Cc"] = "basis@infrabeat.com"
    msg.attach(MIMEText("x", "html"))
    assert mailer.send_message(msg, {"from_email": "ibops@infrabeat.com"},
                               ["client@client-c.example", "ops@client-c.example", "basis@infrabeat.com"],
                               system="PS4", kind="report")
    row = alert_engine.history("PS4", 1)[0]
    assert row["metric"] == "report" and row["kind"] == "sent" and row["ok"] == 1
    assert row["recipients"] == "client@client-c.example, ops@client-c.example | cc: basis@infrabeat.com"
    e = ml.email_from_send(row)
    assert e["kind_label"] == "Report" and e["subject"] == "InfraBeatOps Report - PS4"
    assert e["cc"] == ["basis@infrabeat.com"] and e["status"] == "sent"
    # a failed report is recorded too, with the reason
    monkeypatch.setattr(mailer, "transmit", lambda *a, **k: (False, "550 refused"))
    assert not mailer.send_message(msg, {"from_email": "x"}, ["client@client-c.example"], system="PS4",
                                   kind="report")
    e = ml.email_from_send(alert_engine.history("PS4", 1)[0])
    assert e["status"] == "failed" and "550 refused" in e["status_text"]
    assert e["subject"] == "InfraBeatOps Report - PS4"
    # mail switched off: recorded as suppressed
    monkeypatch.setenv("IBO_ENABLE_EMAIL", "0")
    assert not mailer.send_message(msg, {"from_email": "x"}, ["client@client-c.example"], system="PS4",
                                   kind="report")
    e = ml.email_from_send(alert_engine.history("PS4", 1)[0])
    assert e["status"] == "suppressed" and "switched off" in e["status_text"]
    # kinds whose callers record themselves are not written twice
    n = len(alert_engine.history("PS4", 1))
    monkeypatch.setenv("IBO_ENABLE_EMAIL", "1")
    monkeypatch.setattr(mailer, "transmit", lambda *a, **k: (True, ""))
    mailer.send_message(msg, {"from_email": "x"}, ["basis@infrabeat.com"], system="PS4", kind="rca")
    assert len(alert_engine.history("PS4", 1)) == n


def test_recording_never_raises(env, monkeypatch):
    from notifications import mailer
    from core import alert_ledger
    monkeypatch.setenv("IBO_ENABLE_EMAIL", "1")
    monkeypatch.setattr(mailer, "transmit", lambda *a, **k: (True, ""))

    def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(alert_ledger, "history", boom)
    msg = MIMEText("x")
    msg["Subject"], msg["To"] = "s", "a@b.c"
    assert mailer.send_message(msg, {"from_email": "x"}, ["a@b.c"], system="PS4", kind="report") is True


def test_page_served_and_linked_in_nav(env):
    app_module, _auth, _ops = env
    r = TestClient(app_module.app).get("/monitoring-log")
    assert r.status_code == 200 and "Monitoring log" in r.text and "/api/monitoring-log" in r.text
    shell = open(SHELL, encoding="utf-8").read()
    assert re.search(r'label:\s*"Monitoring log".*href:\s*"/monitoring-log"', shell)


# ------------------------------------------------------------- escaping (node)

HARNESS = r"""
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, innerHTML: "", textContent: "", value: "", href: "", style: {}, dataset: {},
    options: [], add(o) { this.options.push(o); }, addEventListener() {}, onclick: null, onchange: null,
    classList: { toggle() {} } };
  return els[id];
}
global.document = { getElementById: el, documentElement: { setAttribute() {} }, hidden: false,
                    addEventListener() {} };
global.window = { addEventListener() {} };
global.Option = function (t, v) { this.text = t; this.value = v; };
global.localStorage = { getItem() { return null; } };
const IB = { esc(s) { return String(s ?? "").replace(/[&<>"'`]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;" }[c])); },
  mountShell() {}, themeToggle() {}, poll() { return { stop() {} }; },
  get: async () => DATA_IN };
global.IB = IB;
const DATA_IN = __DATA__;
__SCRIPT__
setTimeout(() => {
  console.log(JSON.stringify({ runs: el("runs").innerHTML, mails: el("mails").innerHTML,
                               next: el("next").innerHTML }));
}, 50);
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_page_escapes_server_text():
    evil = "<script>alert(1)</script>"
    mail = {"id": 1, "at": "2026-10-23 10:07:00", "system": "PS4", "kind": "report", "kind_label": "Report",
            "subject": evil, "detail": None, "to": [f"x{evil}@a.b"], "cc": [evil], "channel": "email",
            "channel_label": "Email", "status": "sent", "status_text": evil}
    data = {"recipients_hidden": False, "systems": ["PS4"], "emails": [mail],
            "upcoming": [{"system": evil, "profile": evil, "source": "Profile", "schedule": evil,
                          "next_run_at": "2026-10-23 14:00", "reason": evil, "state": "held"}],
            "runs": [{"id": 1, "run_id": evil, "system": evil, "profile_id": evil, "trigger_label": evil,
                      "started_at": "2026-10-23T10:00:00+05:30", "ended_at": "2026-10-23T10:06:00+05:30",
                      "duration_s": 360, "result": evil, "screenshots_captured": 3, "failed_stage": evil,
                      "failure_reason": evil, "next_run_reason": evil, "emails": [mail]}]}
    html = open(PAGE, encoding="utf-8").read()
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    # no raw innerHTML assignment of a server field without esc()
    assert "esc(" in script
    js = HARNESS.replace("__DATA__", json.dumps(data)).replace("__SCRIPT__", script)
    res = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    for part in ("runs", "mails", "next"):
        assert "<script>" not in out[part], part
    assert "&lt;script&gt;" in out["mails"] and "&lt;script&gt;" in out["next"]
    assert "&lt;script&gt;" in out["runs"]


def test_final_report_goes_to_cc_as_well(tmp_path, monkeypatch):
    """The profile's/system's Cc list was configured but the report only went to To."""
    from notifications import email_report, mailer
    seen = {}

    def fake_send(msg, smtp_config, envelope, system="", kind="", **kw):
        seen.update(to=msg["To"], cc=msg["Cc"], envelope=list(envelope), kind=kind)
        return True

    monkeypatch.setattr(mailer, "send_message", fake_send)
    monkeypatch.setattr(email_report, "_build_report_html", lambda r: "<p>r</p>")

    class R:
        system = "PS4"

    ok = email_report.send_final_report(
        R(), {"from_email": "ibops@infrabeat.com", "to_emails": ["client@client-c.example"],
              "cc_emails": ["staff.a@infrabeat.com", "client@client-c.example"]}, "", "")
    assert ok and seen["kind"] == "report"
    assert seen["to"] == "client@client-c.example" and seen["cc"] == "staff.a@infrabeat.com"
    assert seen["envelope"] == ["client@client-c.example", "staff.a@infrabeat.com"]
