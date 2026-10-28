"""
Round 2: real-time exception handling, extra SAP checks, time escalation
(MSG91), change tracking, noise / availability, incident report, ask,
weekly digest, business calendar, synthetic transaction, cross-system
correlation, new one-click actions and approval policy, standby failover,
WhatsApp alerts, Entra ID sign-in, configuration export / import.

No network, no SAP: sessions, HTTP and HANA are faked.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import socket
import ssl
import subprocess
import threading
import time
import types
import zipfile
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_DB_PATH", str(tmp_path / "r2.sqlite"))
    monkeypatch.setenv("IBO_STATE_BACKEND", "sqlite")
    monkeypatch.setenv("IBO_AUTH", "0")
    monkeypatch.setenv("IBO_MAIL_ASYNC", "0")
    monkeypatch.setenv("IBO_ERROR_MAIL", "0")
    monkeypatch.delenv("IBO_PUBLIC_URL", raising=False)
    from core import store, ops_db, errors
    importlib.reload(store)
    ops_db.reset_for_tests()
    errors.reset_for_tests()
    from core import alert_ledger
    importlib.reload(alert_ledger)
    from collectors import extra_checks
    extra_checks.forget()
    from core import config_loader
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path / "config"))
    os.makedirs(tmp_path / "config", exist_ok=True)
    from core import change_tracking, metric_history
    monkeypatch.setattr(change_tracking, "_path", lambda system: str(tmp_path / "changes" / f"{system}.json"))
    monkeypatch.setattr(metric_history, "HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setenv("IBO_CHECK_PROCESSES", "0")
    monkeypatch.setenv("IBO_SETTINGS_SYNC", "0")
    return tmp_path


def _wait_for(cond, seconds: float = 5.0):
    """Background threads (mail, WhatsApp) under a loaded machine: wait, do not guess a sleep."""
    end = time.time() + seconds
    while time.time() < end and not cond():
        time.sleep(0.05)


def _systems(monkeypatch, cfgs):
    from core import config_loader
    monkeypatch.setattr(config_loader, "get_all_systems", lambda: [dict(c) for c in cfgs])
    monkeypatch.setattr(config_loader, "get_systems", lambda: [dict(c) for c in cfgs])
    return cfgs


def _capture_mail(monkeypatch):
    sent = []
    import notifications.mailer as mailer

    def fake(**kw):
        from core import alert_ledger
        sys_, kind = kw.get("system", ""), kw.get("kind", "")
        if sys_ and kind and (kw.get("min_interval_s") or kw.get("daily_cap") is not None):
            ok, _ = alert_ledger.should_send(sys_, kind, kw.get("key"), kw.get("min_interval_s") or 0, kw.get("daily_cap"))
            if not ok:
                return False
        sent.append(kw)
        if sys_ and kind:
            alert_ledger.record(sys_, kind, kw.get("key"), kw.get("severity", ""), kw["subject"], kw["to"])
        return True
    monkeypatch.setattr(mailer, "send_html", fake)
    monkeypatch.setattr(mailer, "send_html_now", fake)
    monkeypatch.setattr("core.config_loader.get_smtp_config", lambda *a, **k: {})
    monkeypatch.setenv("IBO_BASIS_EMAILS", "basis@infrabeat.com")
    monkeypatch.setenv("ALERT_MANAGER_EMAILS", "mgr@infrabeat.com")
    return sent


# ================================================================= errors

def test_error_capture_groups_and_reopens(fresh):
    from core import errors
    for host in ("10.1.0.5", "10.1.0.9"):
        try:
            raise ConnectionError(f"connect to {host}:3300 refused after 3 tries")
        except ConnectionError as exc:
            errors.capture(exc, "sweep", context="PS4")
    rows = errors.listing()
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["source"] == "sweep"
    sig = rows[0]["signature"]
    assert errors.resolve(sig, "om")
    assert errors.listing() == []
    try:
        raise ConnectionError("connect to 10.9.9.9:3300 refused after 7 tries")
    except ConnectionError as exc:
        errors.capture(exc, "sweep")
    back = errors.get(sig)
    assert back["resolved_at"] is None and back["count"] == 3
    from core import timeline
    titles = [e["title"] for e in timeline.query("IBOPS", 1, ["error"])]
    assert any(t.startswith("New error") for t in titles) and any(t.startswith("Error back") for t in titles)


def test_error_mail_once_and_no_recursion(fresh, monkeypatch):
    monkeypatch.setenv("IBO_ERROR_MAIL", "1")
    from core import errors
    calls = []
    import notifications.mailer as mailer

    def boom(**kw):
        calls.append(kw["subject"])
        from utils.logger import get_logger
        get_logger("notifications.mailer").error("mail failed: SMTP down")   # would recurse without the guard
        raise RuntimeError("smtp down")
    monkeypatch.setattr(mailer, "send_html", boom)
    monkeypatch.setattr("core.config_loader.get_smtp_config", lambda *a, **k: {})
    monkeypatch.setenv("IBO_BASIS_EMAILS", "basis@infrabeat.com")
    try:
        raise KeyError("missing_key")
    except KeyError as exc:
        errors.capture(exc, "web")
        errors.capture(exc, "web")
    _wait_for(lambda: calls)
    time.sleep(0.3)                             # and no second mail follows
    assert len(calls) == 1 and calls[0].startswith("[IBOPS ERROR] KeyError")
    assert len(errors.listing()) == 1          # the mailer's own ERROR line was not captured


def test_error_log_handler_and_thread_hook(fresh, monkeypatch):
    import logging
    from core import errors
    _systems(monkeypatch, [{"name": "PS4"}, {"name": "PRD"}])
    h = errors._ErrorLogHandler(level=logging.ERROR)
    lg = logging.getLogger("tests.r2.mod")
    lg.propagate = False
    lg.addHandler(h)
    lg.error("could not read TBTCO on PS4: 42 rows")
    lg.error("could not read TBTCO on PRD: 7 rows")
    lg.warning("only a warning")
    rows = [r for r in errors.listing() if r["source"] == "log"]
    assert len(rows) == 1 and rows[0]["count"] == 2
    prev = threading.excepthook
    errors._installed = False
    try:
        errors.install()
        t = threading.Thread(target=lambda: 1 / 0, name="r2-thread")
        t.start()
        t.join()
    finally:
        threading.excepthook = prev
        import sys
        sys.excepthook = sys.__excepthook__
        logging.getLogger().removeHandler(next((x for x in logging.getLogger().handlers
                                                if isinstance(x, errors._ErrorLogHandler)), h))
        from utils import logger as ulog
        ulog.EXTRA_HANDLERS.clear()
    assert any(r["source"] == "thread" and r["etype"] == "ZeroDivisionError" for r in errors.listing())


def test_supervisor_restarts_crashed_loop(fresh, monkeypatch):
    from core import supervisor, errors
    monkeypatch.setattr(supervisor, "_DELAYS", (0.05, 0.05))
    runs = {"n": 0}

    def loop():
        runs["n"] += 1
        if runs["n"] < 3:
            raise RuntimeError("crash")
    supervisor.start("r2-test", loop)
    deadline = time.time() + 10
    while time.time() < deadline and "returned" not in supervisor.status()["r2-test"]["state"]:
        time.sleep(0.05)
    st = supervisor.status()["r2-test"]
    assert runs["n"] == 3 and st["restarts"] == 2 and st["state"].startswith("stopped")
    assert any(r["source"] == "loop" for r in errors.listing())


# ============================================================ extra checks

class _XS:
    """Fake SapSession for extra checks: tables and functions from dicts."""

    def __init__(self, tables=None, fms=None, ifaces=None, system="PS4"):
        self.system, self.ok, self.error, self.last_error = system, True, None, ""
        self.tables = tables or {}
        self.fms = fms or {}
        ifaces = ifaces or {}
        self.reads = []

        class _C:
            def get_function_description(_self, fm):
                if fm not in ifaces:
                    raise RuntimeError("FU_NOT_FOUND")
                return types.SimpleNamespace(parameters=[
                    {"name": n, "direction": d, "optional": False} for n, d in ifaces[fm]])
        self.conn = _C()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read_table(self, table, fields, where="", rows=500):
        self.reads.append((table, where))
        fn = self.tables.get(table)
        if fn is None:
            self.last_error = f"{table} not readable"
            return None
        return [[r.get(f, "") for f in fields] for r in fn(where)][:rows]

    def call(self, name, **kw):
        fn = self.fms.get(name)
        if fn is None:
            self.last_error = f"{name}: FU_NOT_FOUND"
            return None
        return fn(**kw)


def test_expected_jobs_and_housekeeping(fresh, monkeypatch):
    from collectors import extra_checks as xc
    now = datetime.now().replace(hour=10, minute=0)
    monkeypatch.setattr(xc, "_sap_now", lambda s: now)
    today = now.strftime("%Y%m%d")

    def tbtco(where):
        if "Z_BILL" in where:
            return [{"JOBNAME": "Z_BILL", "STATUS": "F", "ENDDATE": today}]
        if "Z_STOCK" in where:
            return [{"JOBNAME": "Z_STOCK", "STATUS": "A", "ENDDATE": today}]
        if "SAP_REORG_JOBS" in where:
            return [{"JOBNAME": "SAP_REORG_JOBS"}]
        return []
    s = _XS(tables={"TBTCO": tbtco})
    cfg = {"checks": {"expected_jobs": [
        {"job": "Z_BILL", "by": "06:00"}, {"job": "Z_STOCK", "by": "07:00", "severity": "critical"},
        {"job": "Z_LATE", "by": "23:00"}, {"job": "Z_SUNDAY", "by": "01:00", "days": "sun" if now.weekday() != 6 else "mon"},
        {"job": "bad name!", "by": "01:00"}]}}
    m = xc.check_expected_jobs(s, "PS4", cfg)
    assert m.value == 1 and m.status.value == "CRITICAL" and "Z_STOCK (cancelled" in m.detail and "invalid" in m.detail
    hk = xc.check_housekeeping(s, "PS4")
    assert hk.value == 5 and "SAP_REORG_SPOOL" in hk.detail and "SAP_REORG_JOBS" not in hk.detail
    assert xc._due_today("mon-fri", datetime(2026, 10, 5)) and not xc._due_today("mon-fri", datetime(2026, 10, 4))
    assert xc._due_today("fri-mon", datetime(2026, 10, 4)) and xc._due_today("monthly:1,15", datetime(2026, 10, 15))


def test_job_runtime_prediction(fresh, monkeypatch):
    from collectors import extra_checks as xc
    now = datetime(2026, 10, 7, 12, 0)
    monkeypatch.setattr(xc, "_sap_now", lambda s: now)
    xc._hist_cache.clear()
    start = now - timedelta(minutes=95)

    def tbtco(where):
        if "STATUS = 'R'" in where:
            return [{"JOBNAME": "Z_LONG", "JOBCOUNT": "1", "STRTDATE": start.strftime("%Y%m%d"),
                     "STRTTIME": start.strftime("%H%M%S")},
                    {"JOBNAME": "Z_NEW", "JOBCOUNT": "2", "STRTDATE": start.strftime("%Y%m%d"),
                     "STRTTIME": start.strftime("%H%M%S")}]
        if "Z_LONG" in where:
            return [{"STRTDATE": "20261001", "STRTTIME": "100000", "ENDDATE": "20261001", "ENDTIME": f"10{m:02d}00"}
                    for m in (20, 22, 25, 30)]
        return []
    m = xc.check_job_runtimes(_XS(tables={"TBTCO": tbtco}), "PS4")
    assert m.value == 1 and "Z_LONG running 95 min" in m.detail and m.status.value == "WARNING"
    m2 = xc.check_job_runtimes(_XS(tables={"TBTCO": tbtco}), "PS4", {"checks": {"runtime_ignore": ["z_long*"]}})
    assert m2.value == 0 and m2.status.value == "NORMAL"


def test_users_destinations_spool_transports(fresh, monkeypatch):
    from collectors import extra_checks as xc
    soon = (datetime.now() + timedelta(days=5)).strftime("%Y%m%d")

    def usr02(where):
        if "'ALEREMOTE'" in where:
            return [{"BNAME": "ALEREMOTE", "UFLAG": "128", "GLTGB": "00000000"}]
        if "'WF-BATCH'" in where:
            return [{"BNAME": "WF-BATCH", "UFLAG": "0", "GLTGB": soon}]
        if "'MONUSER'" in where:
            return [{"BNAME": "MONUSER", "UFLAG": "0", "GLTGB": "00000000"}]
        return []
    tables = {"USR02": usr02,
              "ARFCSSTATE": lambda w: [{"ARFCDEST": "BWPCLNT100", "ARFCSTATE": "SYSFAIL"}] * 3,
              "TPALOG": lambda w: [{"TRKORR": "K900111", "RETCODE": "0008", "TRTIME": "20261007080000"},
                                   {"TRKORR": "K900112", "RETCODE": "0000", "TRTIME": "20261007081000"}]}
    fms = {"EM_GET_NUMBER_OF_ENTRIES": lambda IT_TABLES: {"IT_TABLES": [{"TABNAME": "TSP01", "TABROWS": 29000}]},
           "DDIF_FIELDINFO_GET": lambda TABNAME: {"DFIES_TAB": [{"FIELDNAME": f} for f in
                                                                 ("TRKORR", "RETCODE", "TRTIME", "TRSTEP")]},
           "CAT_CHECK_RFC_DESTINATION": lambda RFCDESTINATION: {"RFC_SUBRC": 1 if RFCDESTINATION == "CRM" else 0,
                                                                "MSGV1": "partner not reached"}}
    ifaces = {"EM_GET_NUMBER_OF_ENTRIES": [("IT_TABLES", "RFC_TABLES")],
              "CAT_CHECK_RFC_DESTINATION": [("RFCDESTINATION", "RFC_IMPORT")]}
    s = _XS(tables, fms, ifaces)
    cfg = {"rfc": {"username": "monuser"}, "checks": {"watch_users": ["ALEREMOTE", "wf-batch"],
                                                       "rfc_destinations": ["CRM", "BW"], "spool_max": 32000}}
    u = xc.check_users(s, "PS4", cfg)
    assert u.status.value == "CRITICAL" and u.value == 2 and "after failed logons" in u.detail and "5 d" in u.detail
    d = xc.check_destinations(s, "PS4", cfg)
    assert d.value == 2 and "BWPCLNT100: 3 failed" in d.detail and "CRM test failed" in d.detail
    sp = xc.check_spool(s, cfg)
    assert sp.value == 29000 and sp.status.value == "CRITICAL" and "90.6%" in sp.detail
    t = xc.check_transports(s, "PS4")
    assert t.value == 1 and t.status.value == "WARNING" and "K900111 RC 8" in t.detail
    # with a date field, only recent failures count (an old SM58 backlog is not "failing now")
    fields_with_date = {"ARFCSSTATE": ["ARFCDEST", "ARFCSTATE", "ARFCDATUM"]}
    seen_where = []
    s2 = _XS({"ARFCSSTATE": lambda w: seen_where.append(w) or []},
             {"DDIF_FIELDINFO_GET": lambda TABNAME: {"DFIES_TAB": [{"FIELDNAME": f} for f in
                                                                   fields_with_date.get(TABNAME, [])]}}, system="QAS")
    d2 = xc.check_destinations(s2, "QAS", {"checks": {"sm58_days": 3}})
    assert "ARFCDATUM >=" in seen_where[0] and d2.status.value == "NORMAL" and "in 3 d" in d2.detail
    # a system without the row-count function says so instead of guessing
    assert xc.check_spool(_XS(), {}).status.value == "UNKNOWN"


def test_licence_unknown_and_found(fresh):
    from collectors import extra_checks as xc
    m = xc.check_licence(_XS())
    assert m.status.value == "UNKNOWN" and "not readable over RFC" in m.detail
    xc.forget("PS4")                      # "does not exist" is remembered per system
    exp = (datetime.now() + timedelta(days=20)).strftime("%Y%m%d")
    s = _XS(fms={"SLIC_GET_LICENCE_KEYS": lambda: {"LICENSES": [{"PRODUCTID": "X", "EXPIRATION_DATE": exp},
                                                                {"PRODUCTID": "Y", "EXPIRATION_DATE": "99991231"}]}},
            ifaces={"SLIC_GET_LICENCE_KEYS": [("LICENSES", "RFC_TABLES")]})
    m = xc.check_licence(s)
    assert m.status.value == "WARNING" and m.value == 20, (m.value, m.detail)


def _tls_server(tmp_path, days: int):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                    "-days", str(days), "-subj", "/CN=sapprd.test"], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    def serve():
        for _ in range(4):
            try:
                c, _ = srv.accept()
                with ctx.wrap_socket(c, server_side=True) as tc:
                    tc.recv(1)
            except Exception:
                pass
    threading.Thread(target=serve, daemon=True).start()
    return port


def test_certificate_expiry_over_tls(fresh, tmp_path):
    from collectors import extra_checks as xc
    port = _tls_server(tmp_path, 5)
    m = xc.check_certificates({"checks": {"certificates": [f"127.0.0.1:{port}", "127.0.0.1:1"]}})
    assert m.status.value == "CRITICAL" and m.value in (4, 5) and "sapprd.test" in m.detail
    assert "127.0.0.1:1 does not answer" in m.detail


def test_hana_checks_with_fake_driver(fresh, monkeypatch):
    from collectors import extra_checks as xc
    answers = {
        "M_LICENSE": [(datetime.now() + timedelta(days=3), "FALSE", "TRUE")],
        "M_HOST_RESOURCE_UTILIZATION": [("hana1", 90 * 1024 ** 3, 100 * 1024 ** 3)],
        "STATISTICS_CURRENT_ALERTS": [(5, "Log volume full")],
        "M_SERVICE_REPLICATION": [("hana2", "ERROR")],
        "M_BACKUP_CATALOG": [(datetime.utcnow() - timedelta(hours=30),)],
    }

    class Cur:
        def execute(self, sql):
            self.rows = next(v for k, v in answers.items() if k in sql)

        def fetchall(self):
            return self.rows

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass
    fake = types.ModuleType("hdbcli")
    fake.dbapi = types.SimpleNamespace(connect=lambda **kw: Conn())
    monkeypatch.setitem(__import__("sys").modules, "hdbcli", fake)
    ms = {m.name: m for m in xc.check_hana("PS4", {"hana": {"host": "h", "user": "u", "password": "p"}})}
    assert ms["hana.license_days_left"].status.value == "CRITICAL"
    assert ms["hana.memory_used_pct"].value == 90.0 and ms["hana.memory_used_pct"].status.value == "WARNING"
    assert ms["hana.alerts_high"].status.value == "CRITICAL"
    assert ms["hana.replication"].status.value == "CRITICAL"
    assert ms["hana.last_backup_hours"].status.value == "WARNING"


def test_graded_checks_keep_status_on_the_wall(fresh):
    from collectors import extra_checks as xc
    from core.live_metrics import metrics_from_live
    from core.models import Status
    rows = xc.to_rows([xc._metric("sap.strust.cert_days_left", 5, Status.CRITICAL, "x", display="5 days"),
                       xc._unknown("sap.slicense.days_left", "n/a")])
    ms = {m.name: m for m in metrics_from_live({"connected": True, "checks": rows}, {})}
    assert ms["sap.strust.cert_days_left"].status == Status.CRITICAL and ms["sap.strust.cert_days_left"].value == 5.0
    assert ms["sap.slicense.days_left"].status == Status.UNKNOWN and ms["sap.slicense.days_left"].value is None


def test_check_alerts_mail_once_with_stable_key(fresh, monkeypatch):
    from collectors import extra_checks as xc
    from core import check_alerts
    from core.models import Status
    sent = _capture_mail(monkeypatch)
    cfg = {"name": "PS4", "rfc": {"ashost": "1.2.3.4"}}
    m1 = xc._metric("sap.sm37.overdue_jobs", 1, Status.WARNING, "Z_LONG running 95 min")
    m2 = xc._metric("sap.sm37.overdue_jobs", 1, Status.WARNING, "Z_LONG running 125 min")
    assert check_alerts._mail(cfg, m1) and not check_alerts._mail(cfg, m2)
    assert len(sent) == 1 and "job running long" in sent[0]["subject"]


# ============================================================== escalation

def test_time_escalation_ladder(fresh, monkeypatch, tmp_path):
    from core import escalation_time, reachability, alert_control
    sent = _capture_mail(monkeypatch)
    (tmp_path / "config" / "escalation.yaml").write_text(
        "enabled: true\nsteps:\n  - {after_minutes: 15, action: manager_mail}\n"
        "  - {after_minutes: 30, action: sms, to: [manager]}\nmanager_phones: ['9876543210']\n", encoding="utf-8")
    since = (datetime.now() - timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(reachability, "current_failures",
                        lambda: {"PS4": {"state": "SYSTEM DOWN", "since": since, "message": "PS4 is DOWN"}})
    _systems(monkeypatch, [{"name": "PS4"}])
    from notifications import sms
    smsed = []
    monkeypatch.setattr(sms, "send_sms", lambda phones, fields: (smsed.append((phones, fields)) or (True, "ok")))
    # SMS not configured -> noted once, not retried
    assert escalation_time.run() == 1
    assert len(sent) == 1 and sent[0]["subject"].startswith("[ESCALATION]") and not smsed
    monkeypatch.setenv("IBO_MSG91_AUTHKEY", "k")
    monkeypatch.setenv("IBO_MSG91_FLOW_ID", "f")
    from core import ops_db
    ops_db.execute("DELETE FROM alerts WHERE kind LIKE 'escalation_2%'")
    escalation_time.run()
    escalation_time.run()
    assert len(smsed) == 1 and smsed[0][0] == ["9876543210"] and smsed[0][1]["minutes"] >= 35
    assert len(sent) == 1                                   # manager mail only once per outage
    # acknowledged -> the ladder stops
    ops_db.execute("DELETE FROM alerts")
    ops_db.execute("DELETE FROM send_attempts")
    alert_control.silence("PS4", "*", "ack", by="om")
    escalation_time.run()
    assert len(sent) == 1 and len(smsed) == 1


def test_escalation_failed_send_backs_off_and_standby_is_skipped(fresh, monkeypatch, tmp_path):
    from core import escalation_time, reachability, ops_db, attempts, timeline
    _capture_mail(monkeypatch)
    (tmp_path / "config" / "escalation.yaml").write_text(
        "enabled: true\nsteps:\n  - {after_minutes: 1, action: sms, to: [manager]}\nmanager_phones: ['9876543210']\n",
        encoding="utf-8")
    since = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(reachability, "current_failures",
                        lambda: {"PS4": {"state": "SYSTEM DOWN", "since": since, "message": "down"}})
    _systems(monkeypatch, [{"name": "PS4"}])
    monkeypatch.setenv("IBO_MSG91_AUTHKEY", "k")
    monkeypatch.setenv("IBO_MSG91_FLOW_ID", "f")
    from notifications import sms
    tries = []
    monkeypatch.setattr(sms, "send_sms", lambda phones, fields: (tries.append(1) or (False, "template rejected")))
    for _ in range(5):                       # five ops ticks in a row
        escalation_time.run()
    assert len(tries) == 1                   # not once a minute
    scope = f"esc:PS4:escalation_1:system_down:{since}"
    ops_db.execute("UPDATE send_attempts SET last_at=? WHERE scope=?",
                   ((datetime.now() - timedelta(minutes=6)).strftime("%Y-%m-%d %H:%M:%S"), scope))
    escalation_time.run()
    assert len(tries) == 2                   # second try after 5 min
    ops_db.execute("UPDATE send_attempts SET tries=4 WHERE scope=?", (scope,))
    escalation_time.run()
    escalation_time.run()
    assert len(tries) == 2 and attempts.gave_up(scope)
    assert any("given up" in e["title"] for e in timeline.query("PS4", 1))
    # a system the peer monitors (standby) does not escalate from here
    ops_db.execute("DELETE FROM send_attempts")
    ops_db.execute("DELETE FROM alerts")
    _systems(monkeypatch, [{"name": "PS4", "standby_for": "WINVM"}])
    from core import standby
    monkeypatch.setattr(standby, "peer_state", lambda peer: "up")
    escalation_time.run()
    assert len(tries) == 2


def test_standby_handback_forgets_stale_state(fresh, monkeypatch, tmp_path):
    from core import reachability, standby
    monkeypatch.setattr(reachability, "_STATE_PATH", str(tmp_path / "reach.json"))
    _systems(monkeypatch, [{"name": "PS4", "standby_for": "WINVM"}])
    _capture_mail(monkeypatch)
    state = {"peer": "silent"}
    monkeypatch.setattr(standby, "peer_state", lambda peer: state["peer"])
    standby._state.clear()
    cfg = {"name": "PS4", "standby_for": "WINVM"}
    assert standby.active(cfg)
    reachability.report("PS4", reachability.tag(reachability.SYSTEM_DOWN, "PS4 is down"), cfg)
    assert "PS4" in reachability.current_failures()
    state["peer"] = "up"
    assert not standby.active(cfg)
    assert "PS4" not in reachability.current_failures()
    assert standby.only_active({"PS4": 1, "OTHER": 2}) == {"OTHER": 2}


def test_msg91_payload(fresh, monkeypatch):
    from notifications import sms
    monkeypatch.setenv("IBO_MSG91_AUTHKEY", "AUTH")
    monkeypatch.setenv("IBO_MSG91_FLOW_ID", "FLOW1")
    monkeypatch.setenv("IBO_MSG91_SENDER", "IBOPSX")
    seen = {}

    class R:
        status_code = 200
        text = '{"message":"abc","type":"success"}'

        def json(self):
            return {"message": "abc", "type": "success"}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, headers=headers)
        return R()
    monkeypatch.setitem(__import__("sys").modules, "requests", types.SimpleNamespace(post=post))
    ok, _ = sms.send_sms(["+91 98765 43210", "bad"], {"system": "PS4", "state": "SYSTEM DOWN", "minutes": 31})
    assert ok and seen["url"] == "https://api.msg91.com/api/v5/flow/" and seen["headers"]["authkey"] == "AUTH"
    assert seen["body"]["recipients"] == [{"mobiles": "919876543210", "VAR1": "PS4", "VAR2": "SYSTEM DOWN", "VAR3": "31"}]
    assert seen["body"]["flow_id"] == "FLOW1" and seen["body"]["sender"] == "IBOPSX"
    assert sms.normalise("09876543210") == "919876543210" and sms.normalise("0091 98765 43210") == "919876543210"
    assert sms.normalise("+44 20 7946 0958") == "442079460958" and sms.normalise("12345") is None
    assert sms.call(["9876543210"], {})[0] is False      # voice not configured


# ============================================================ change tracking

def test_change_tracking_diff_and_timeline(fresh, monkeypatch):
    from core import change_tracking as ct, timeline
    from collectors import extra_checks as xc
    state = {"kern": "753", "sp": "0012", "pahi": [], "imports": []}
    fields = {"PAHI": ["PARDATE", "PARNAME", "PARVALUE"], "TPALOG": ["TRKORR", "RETCODE", "TRTIME"]}
    s = _XS(tables={"CVERS": lambda w: [{"COMPONENT": "SAP_BASIS", "RELEASE": "750", "EXTRELEASE": state["sp"]}],
                    "PAHI": lambda w: state["pahi"], "TPALOG": lambda w: state["imports"]},
            fms={"RFC_SYSTEM_INFO": lambda: {"RFCSI_EXPORT": {"RFCKERNRL": state["kern"], "RFCDBSYS": "HDB"}},
                 "DDIF_FIELDINFO_GET": lambda TABNAME: {"DFIES_TAB": [{"FIELDNAME": f} for f in fields.get(TABNAME, [])]}})
    assert ct.maybe_snapshot(s, "PS4") == []          # first snapshot: nothing to compare
    state.update(kern="777", sp="0014",
                 pahi=[{"PARDATE": datetime.now().strftime("%Y%m%d"), "PARNAME": "rdisp/wp_no_dia", "PARVALUE": "20"}],
                 imports=[{"TRKORR": "K900200", "RETCODE": "0012", "TRTIME": datetime.now().strftime("%Y%m%d%H%M%S")}])
    xc.forget()
    changes = ct.maybe_snapshot(s, "PS4", force=True)
    titles = " | ".join(t for t, _ in changes)
    assert "kernel release changed: 753 -> 777" in titles and "SAP_BASIS: 750 SP 0012 -> 750 SP 0014" in titles
    assert "rdisp/wp_no_dia = 20" in titles and "K900200 imported (RC 12)" in titles
    assert len(timeline.query("PS4", 1, ["change"])) == 4
    blk = ct.mail_block("PS4")
    assert "Recent changes on PS4" in blk and "with errors (worst return code 12)" in blk


# ================================================== insights, report, ask, digest

def _outage(system, minutes_ago_start, minutes_ago_end):
    from core import timeline
    timeline.add(system, "state", "[SYSTEM DOWN]", "down", at=datetime.now() - timedelta(minutes=minutes_ago_start))
    if minutes_ago_end is not None:
        timeline.add(system, "state", "Reachable again", "", at=datetime.now() - timedelta(minutes=minutes_ago_end))


def test_availability_noise_report_ask_digest(fresh, monkeypatch):
    _systems(monkeypatch, [{"name": "PS4"}, {"name": "PRD"}])
    from core import insights, incident_report, ask, digest, alert_ledger, alert_control, timeline
    _outage("PS4", 120, 60)
    timeline.add("PS4", "change", "transport K900300 imported (RC 0)", at=datetime.now() - timedelta(minutes=200))
    timeline.add("PS4", "silence", "Acknowledged (all alerts)", actor="om", at=datetime.now() - timedelta(minutes=110))
    av = {a["system"]: a for a in insights.availability(7)}
    assert av["PS4"]["outages"] == 1 and 59 <= av["PS4"]["down_minutes"] <= 61 and av["PRD"]["down_minutes"] == 0
    for i in range(12):
        alert_ledger.record("PS4", "critical", f"k{i}", "CRITICAL", "s", ["a@b.c"])
    for i in range(4):
        alert_control.silence("PS4", "critical", "snooze", minutes=10, by="om")
    n = insights.noise(30)
    top = n[0]
    assert top["system"] == "PS4" and top["kind"] == "critical" and top["sent"] == 12 and top["snoozed"] == 4 and top["tip"]
    r = incident_report.build("PS4")
    assert 59 <= r["duration_minutes"] <= 61 and r["minutes_to_ack"] in (9, 10, 11)
    assert any("K900300" in c["title"] for c in r["changes"])
    html = incident_report.to_html(r)
    assert "Incident report" in html and "K900300" in html
    cust = incident_report.build("PS4", audience="customer")
    assert all(e["category"] != "silence" for e in cust["events"]) and cust["first_ack"] is None
    a = ask.answer("what happened on PS4 today?", ["PS4", "PRD"])
    assert a["ai"] is False and a["systems"] == ["PS4"] and "SYSTEM DOWN" in a["answer"], a
    with pytest.raises(ValueError):
        ask.answer("  ", ["PS4"])
    d = digest.build(7)
    assert {s["system"] for s in d["systems"]} == {"PS4", "PRD"}
    assert "weekly digest" in digest.to_html(d)
    assert digest.due(datetime(2026, 10, 5, 9)) and not digest.due(datetime(2026, 10, 5, 7)) \
        and not digest.due(datetime(2026, 10, 6, 9))


def test_incident_report_pdf_if_reportlab(fresh, monkeypatch, tmp_path):
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return
    _systems(monkeypatch, [{"name": "PS4"}])
    _outage("PS4", 30, None)
    from core import incident_report
    path = incident_report.to_pdf(incident_report.build("PS4"), str(tmp_path / "r.pdf"))
    assert os.path.getsize(path) > 1000


# ======================================================= business calendar

def test_business_calendar_and_special_day_band(fresh, tmp_path, monkeypatch):
    from core import business_calendar as bc
    (tmp_path / "config" / "business_calendar.yaml").write_text(
        "holidays: ['2026-10-20']\nmonth_end_working_days: 2\nspecial_days: ['2026-10-14']\n", encoding="utf-8")
    bc._checked["at"] = -1e9
    assert bc.day_class(datetime(2026, 10, 20)) == "holiday"
    assert bc.day_class(datetime(2026, 10, 14)) == "special"
    assert bc.day_class(datetime(2026, 10, 30)) == "special" and bc.day_class(datetime(2026, 10, 29)) == "special"
    assert bc.day_class(datetime(2026, 10, 28)) == "workday" and bc.day_class(datetime(2026, 10, 31)) == "weekend"
    from core import learned_baseline as lb
    series = []
    for months_back in (1, 2, 3):
        d = datetime(2026, 10 - months_back, 1, 10)
        while bc.day_class(d) != "special":
            d += timedelta(days=1)
        series.append((d, 900.0))
    day = datetime(2026, 9, 1, 10)
    for i in range(40):
        t = day + timedelta(days=i)
        if bc.day_class(t) == "workday":
            series.append((t, 100.0))
    a = lb.assess("PS4", "sap.sm12.lock_count", 880, datetime(2026, 10, 30, 10, 5), series)
    assert a["state"] == "normal" and "month-end" in a["basis"]
    b = lb.assess("PS4", "sap.sm12.lock_count", 880, datetime(2026, 10, 27, 10, 5), series)
    assert b["state"] == "high"


# ============================================================== synthetic

def test_synthetic_slow_alert_after_two_runs(fresh, monkeypatch):
    from core import synthetic
    sent = _capture_mail(monkeypatch)
    synthetic._slow_runs.clear()
    cfg = {"name": "PS4"}
    synthetic._record("PS4", {"ok": True, "logon_ms": 900, "total_ms": 4000}, cfg)
    assert not sent
    synthetic._record("PS4", {"ok": True, "logon_ms": 900, "total_ms": 4200}, cfg)
    assert len(sent) == 1 and sent[0]["subject"].startswith("[SLOW]")
    synthetic._record("PS4", {"ok": True, "logon_ms": 100, "total_ms": 300}, cfg)
    assert synthetic._slow_runs["PS4"] == 0
    assert synthetic.wanted({"synthetic": True}) and not synthetic.wanted({})


# ============================================================ correlation

def test_cross_system_correlation(fresh, monkeypatch):
    _systems(monkeypatch, [{"name": "PS4", "network": {"name": "Infrabeat VPN"}, "rfc": {"ashost": "198.51.100.31"}},
                           {"name": "CAQ", "network": {"name": "Infrabeat VPN"}, "rfc": {"ashost": "198.51.100.213"}},
                           {"name": "PRD", "rfc": {"ashost": "10.1.0.193"}}])
    sent = _capture_mail(monkeypatch)
    from core import correlation_x, reachability
    monkeypatch.setattr(reachability, "current_failures",
                        lambda: {"PS4": {"state": "SYSTEM DOWN"}, "CAQ": {"state": "SYSTEM DOWN"},
                                 "PRD": {"state": "VPN NOT CONNECTED"}})
    assert correlation_x.run() == 1
    assert len(sent) == 1 and "CAQ, PS4" in sent[0]["subject"]
    assert correlation_x.run() == 1 and len(sent) == 1        # held for 2 h


# ================================================ actions: new + policy

def test_new_actions_and_policy(fresh, monkeypatch, tmp_path):
    from core import sap_actions as sa
    cfgs = _systems(monkeypatch, [{"name": "PS4", "rfc": {"ashost": "1.2.3.4"},
                                   "actions": ["unlock_user", "cancel_job"]},
                                  {"name": "QAS", "rfc": {"ashost": "1.2.3.5"}, "actions": ["unlock_user"]}])
    monkeypatch.setenv("IBO_ACTIONS_ENABLED", "1")
    (tmp_path / "config" / "action_policy.yaml").write_text(
        "default: confirm\nrules:\n  - {systems: [PS4], actions: [unlock_user, cancel_job], approval: two_person}\n",
        encoding="utf-8")
    calls = []
    ifaces = {"BAPI_USER_UNLOCK": [("USERNAME", "RFC_IMPORT")],
              "BAPI_XMI_LOGON": [(n, "RFC_IMPORT") for n in ("EXTCOMPANY", "EXTPRODUCT", "INTERFACE", "VERSION")],
              "BAPI_XBP_JOB_ABORT": [(n, "RFC_IMPORT") for n in ("JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME")]}
    fms = {"BAPI_USER_UNLOCK": lambda USERNAME: calls.append(("unlock", USERNAME)) or {"RETURN": []},
           "BAPI_XMI_LOGON": lambda **k: {"RETURN": {"TYPE": "S"}},
           "BAPI_XMI_LOGOFF": lambda **k: {},
           "BAPI_XBP_JOB_ABORT": lambda **k: calls.append(("abort", k["JOBNAME"])) or {"RETURN": {"TYPE": "S"}}}
    tables = {"USR02": lambda w: [{"BNAME": "JDOE", "UFLAG": "128"}] if "JDOE" in w else
              [{"BNAME": "OK1", "UFLAG": "0"}] if "OK1" in w else [],
              "TBTCO": lambda w: [{"JOBNAME": "Z_RUN", "JOBCOUNT": "1", "STATUS": "R", "SDLUNAME": "OM",
                                   "STRTDATE": "20261007", "STRTTIME": "010000"}]}
    from collectors import extra_checks
    extra_checks.forget()
    monkeypatch.setattr(sa, "_session", lambda system, cfg: _XS(tables, fms, ifaces, system=system))
    assert sa.mode_for("PS4", "unlock_user") == "two_person" and sa.mode_for("QAS", "unlock_user") == "confirm"
    assert "after too many failed logons" in sa.preview("PS4", "unlock_user", {"user": "jdoe"})["text"]
    with pytest.raises(ValueError, match="not locked"):
        sa.preview("PS4", "unlock_user", {"user": "ok1"})
    with pytest.raises(ValueError, match="protected"):
        sa.preview("PS4", "unlock_user", {"user": "DDIC"})
    a = sa.request("PS4", "unlock_user", {"user": "JDOE"}, "user called", by="om")
    with pytest.raises(ValueError, match="second person"):
        sa.approve(a["id"], by="om")
    sa.add_standing_approval("PS4", "*", 60, "month-end", by="alex")
    done = sa.approve(a["id"], by="om", wait=True)
    assert done["status"] == "done" and "standing approval" in done["approved_by"] and ("unlock", "JDOE") in calls
    c = sa.request("PS4", "cancel_job", {"job": "Z_RUN", "job_count": "1"}, "runaway", by="om")
    assert sa.approve(c["id"], by="vik", wait=True)["status"] == "done" and ("abort", "Z_RUN") in calls
    # a function missing on the system -> refused in the preview, nothing run
    monkeypatch.setattr(sa, "_session", lambda system, cfg: _XS(tables, fms, {}, system=system))
    extra_checks.forget()
    with pytest.raises(ValueError, match="does not exist"):
        sa.preview("QAS", "unlock_user", {"user": "JDOE"})


# ================================================================ standby

def test_standby_follows_peer(fresh, monkeypatch):
    from core import standby, peer_watch, store, reachability, alert_control
    cfgs = _systems(monkeypatch, [{"name": "PS4", "standby_for": "WINVM", "rfc": {"ashost": "1.2.3.4"}}])
    sent = _capture_mail(monkeypatch)
    assert standby.peer_state("WINVM") == "never" and not standby.active(cfgs[0])
    peer_watch.receive("WINVM", {})
    assert not standby.active(cfgs[0]) and "standby" in alert_control.sweep_block_reason("PS4")
    reachability.report("PS4", reachability.tag(reachability.SYSTEM_DOWN, "PS4 down"), cfgs[0])
    assert "PS4" not in reachability.current_failures()
    store.conn().execute("UPDATE heartbeats SET beat_at=? WHERE service='peer:WINVM'",
                         ((datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),))
    assert standby.active(cfgs[0]) and alert_control.sweep_block_reason("PS4") is None
    assert any("[FAILOVER]" in m["subject"] for m in sent)


# =============================================================== whatsapp

def test_whatsapp_alert(fresh, monkeypatch):
    from notifications import whatsapp
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "PN")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "TOK")
    monkeypatch.setenv("WHATSAPP_ALERT_TEMPLATE", "ibops_alert_text")
    monkeypatch.setenv("WHATSAPP_ALERT_RECIPIENTS", "98765 43210")
    monkeypatch.setenv("IBO_PUBLIC_URL", "https://ibops.example")
    posted = []

    class R:
        status_code = 200
        text = "{}"
    monkeypatch.setattr(whatsapp.requests, "post", lambda url, **kw: posted.append((url, kw)) or R())
    assert whatsapp.wants_alert("system_down") and not whatsapp.wants_alert("capacity")
    res = whatsapp.send_alert("PS4", "system_down", "[SYSTEM DOWN] PS4", "line one\nline\ttwo")
    assert res["sent"] and posted[0][0].endswith("/PN/messages")
    body = posted[0][1]["json"]
    params = [p["text"] for p in body["template"]["components"][0]["parameters"]]
    assert body["to"] == "919876543210" and params[1] == "line one · line two"
    assert params[2].startswith("https://ibops.example/access/action?t=")
    # through the ledger: one WhatsApp per alert per 30 min, even while the mail keeps failing
    from core import alert_ledger
    started = []
    monkeypatch.setattr(whatsapp, "send_alert", lambda *a, **k: started.append(a))
    for _ in range(4):
        alert_ledger.record("PS4", "system_down", "PS4", "CRITICAL", "[SYSTEM DOWN] PS4", ["a@b.c"],
                            delivered=False, reason="SMTP down")
    _wait_for(lambda: started)
    time.sleep(0.3)
    assert len(started) == 1


# ==================================================================== SSO

def test_entra_sign_in(fresh, monkeypatch, tmp_path):
    from core import sso, auth
    monkeypatch.setenv("IBO_ENTRA_TENANT_ID", "tid-1")
    monkeypatch.setenv("IBO_ENTRA_CLIENT_ID", "cid-1")
    monkeypatch.setenv("IBO_ENTRA_CLIENT_SECRET", "sec")
    monkeypatch.setenv("IBO_PUBLIC_URL", "https://ibops.example")
    (tmp_path / "config" / "sso.yaml").write_text(
        "allowed_domains: [infrabeat.com]\nroles:\n  admin: [staff.a@infrabeat.com]\n"
        "  operator: ['*@infrabeat.com']\n", encoding="utf-8")
    url, cookie = sso.start("/wall")
    assert url.startswith("https://login.microsoftonline.com/tid-1/oauth2/v2.0/authorize?")
    assert "code_challenge_method=S256" in url and "redirect_uri=https%3A%2F%2Fibops.example%2Fauth%2Fcallback" in url
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(url).query)
    state, nonce = q["state"][0], q["nonce"][0]

    def token_for(claims):
        import base64
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
        return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"
    good = {"aud": "cid-1", "iss": "https://login.microsoftonline.com/tid-1/v2.0", "tid": "tid-1",
            "exp": time.time() + 600, "nbf": time.time() - 5, "nonce": nonce, "preferred_username": "staff.g@infrabeat.com"}
    reply = {"claims": good}

    class R:
        status_code = 200

        def json(self):
            return {"id_token": token_for(reply["claims"])}
    monkeypatch.setitem(__import__("sys").modules, "requests",
                        types.SimpleNamespace(post=lambda url, **kw: R()))
    token, user, nxt = sso.finish("code", state, cookie)
    assert user == "staff.g@infrabeat.com" and nxt == "/wall"
    assert auth.principal_for_token(token)["role"] == "operator"
    with pytest.raises(ValueError, match="state"):
        sso.finish("code", "wrong", cookie)
    reply["claims"] = dict(good, aud="other")
    with pytest.raises(ValueError, match="another application"):
        sso.finish("code", state, cookie)
    reply["claims"] = dict(good, nonce="x")
    with pytest.raises(ValueError, match="nonce"):
        sso.finish("code", state, cookie)
    reply["claims"] = dict(good, preferred_username="someone@gmail.com")
    with pytest.raises(ValueError, match="not allowed"):
        sso.finish("code", state, cookie)
    assert sso.role_for("staff.a@infrabeat.com", [])[0] == "admin"
    # another tenant's token is refused even though its issuer matches its own tid
    reply["claims"] = dict(good, tid="tid-2", iss="https://login.microsoftonline.com/tid-2/v2.0")
    with pytest.raises(ValueError, match="another Microsoft tenant"):
        sso.finish("code", state, cookie)
    # a LOCAL account keeps its password and role when its owner signs in with Microsoft
    auth.add_user("staff.b@infrabeat.com", "local-Pass-123", "admin", "*")
    reply["claims"] = dict(good, preferred_username="staff.b@infrabeat.com")
    token2, user2, _ = sso.finish("code", state, cookie)
    assert auth.principal_for_token(token2)["role"] == "admin"
    assert auth.login("staff.b@infrabeat.com", "local-Pass-123", "") is not None
    # an account Microsoft created follows sso.yaml at the next sign-in
    (tmp_path / "config" / "sso.yaml").write_text(
        "allowed_domains: [infrabeat.com]\nroles:\n  viewer: ['*@infrabeat.com']\n", encoding="utf-8")
    reply["claims"] = good
    token3, _, _ = sso.finish("code", state, cookie)
    assert auth.principal_for_token(token3)["role"] == "viewer"
    # "common" / "organizations" are refused as tenant
    monkeypatch.setenv("IBO_ENTRA_TENANT_ID", "organizations")
    assert not sso.configured() and "not allowed" in sso.problem()
    with pytest.raises(ValueError):
        sso._verify(cookie[:-3] + "AAA")


# ======================================================== config bundle

def test_config_export_and_import(fresh, tmp_path, monkeypatch):
    from core import config_bundle as cb
    cfgdir = tmp_path / "config"
    (cfgdir / "systems.yaml").write_text("systems:\n- name: PS4\n  rfc:\n    password: ${PS4_RFC_PASSWORD}\n"
                                         "    username: mon  # monitor user\n", encoding="utf-8")
    (cfgdir / "remote_sources.yaml").write_text("sources:\n- name: ubuntu\n  token: abc123secret\n", encoding="utf-8")
    (cfgdir / "thresholds.yaml").write_text("cpu:\n  warning: 80\n", encoding="utf-8")
    monkeypatch.setenv("PS4_RFC_PASSWORD", "x")
    data, summary = cb.export_zip()
    z = zipfile.ZipFile(io.BytesIO(data))
    assert summary["secrets_removed"] == 1
    assert "abc123secret" not in z.read("config/remote_sources.yaml").decode()
    assert "${PS4_RFC_PASSWORD}" in z.read("config/systems.yaml").decode()
    assert "PS4_RFC_PASSWORD\tset" in z.read("env_keys.txt").decode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for n in z.namelist():
            if n != "config/thresholds.yaml":
                out.writestr(n, z.read(n))
        out.writestr("config/thresholds.yaml", "cpu:\n  warning: 85\n")
        out.writestr("config/evil.py", "print(1)")
        out.writestr("../etc/passwd", "x")
    check = cb.check_import(buf.getvalue())
    actions = {f["name"]: f["action"] for f in check["files"]}
    assert actions["thresholds.yaml"] == "replace" and actions["remote_sources.yaml"] == "skip"
    assert actions["systems.yaml"] == "same" and len(check["errors"]) == 2
    res = cb.apply_import(buf.getvalue())
    assert res["applied"] == ["thresholds.yaml"]
    assert "85" in (cfgdir / "thresholds.yaml").read_text() and "abc123secret" in (cfgdir / "remote_sources.yaml").read_text()
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as out:
        out.writestr("config/thresholds.yaml", "cpu: [unclosed\n")
    with pytest.raises(ValueError):
        cb.apply_import(bad.getvalue())
    # a file of the wrong shape is refused before it can break every page
    for text in ("systems:\n  PS4: {}\n", "systems:\n", "systems:\n- name: A\n- name: A\n", "- a\n"):
        z2 = io.BytesIO()
        with zipfile.ZipFile(z2, "w") as out:
            out.writestr("config/systems.yaml", text)
        assert cb.check_import(z2.getvalue())["ok"] is False, text
        with pytest.raises(ValueError):
            cb.apply_import(z2.getvalue())
    assert "name: PS4" in (cfgdir / "systems.yaml").read_text()
    # the same file twice in one zip is refused
    z3 = io.BytesIO()
    with zipfile.ZipFile(z3, "w") as out:
        out.writestr("config/thresholds.yaml", "cpu:\n  warning: 70\n")
        out.writestr("thresholds.yaml", "cpu:\n  warning: 60\n")
    assert cb.check_import(z3.getvalue())["ok"] is False
    # secrets that are not on a line of their own are still removed
    for text in ("hana: {host: h, user: M, password: S3cret}\n", "- {name: x, token: abc}\n",
                 "smtp:\n  password: |\n    hunter2\n  user: a\n", "x:\n  passphrase: open sesame\n"):
        red, n = cb.redact(text)
        assert n >= 1 and not any(w in red for w in ("S3cret", "abc", "hunter2", "sesame")) and cb.MARK in red, red
    assert cb.redact("x:\n  password: ${PW}\n") == ("x:\n  password: ${PW}\n", 0)
    for text in ("tokens:\n  - abc123\n", "url: https://user:S3cret@host/x\n"):
        red, n = cb.redact(text)
        assert n >= 1 and "abc123" not in red and "S3cret" not in red and cb.MARK in red, red


# ================================================================= routes

def test_round2_routes(fresh, monkeypatch):
    cfgs = _systems(monkeypatch, [{"name": "PS4"}])
    monkeypatch.setenv("IBO_API_TOKEN", "tok")
    from fastapi.testclient import TestClient
    import dashboard.app as app_module
    monkeypatch.setattr(app_module, "get_systems", lambda: [dict(c) for c in cfgs])
    client = TestClient(app_module.app, raise_server_exceptions=False)
    h = {"X-IBO-Token": "tok"}

    @app_module.app.get("/__boom")
    def _boom():
        raise RuntimeError("route exploded 123")
    r = client.get("/__boom")
    assert r.status_code == 500 and r.json()["error_id"] and "exploded" not in r.text
    errs = client.get("/api/errors").json()["errors"]
    assert errs and errs[0]["source"] == "web"
    # Browser pages send the session's CSRF token with this POST (shell.js reportError).
    _csrf = client.get("/api/me").json().get("csrf_token") or ""
    assert client.post("/api/errors/client", json={"kind": "JavaScriptError", "message": "x is undefined",
                                                    "page": "/wall"},
                       headers={"X-CSRF-Token": _csrf}).json()["recorded"]
    for page in ("/errors", "/ops", "/timeline", "/health", "/systems-page"):
        assert client.get(page).status_code == 200, page
    assert client.get("/report/incident?system=PS4").status_code == 200
    assert client.get("/report/incident?system=NOPE").status_code == 404
    assert client.post("/api/ops/ask", json={"question": "what happened on PS4 today"}, headers=h).status_code == 200
    assert client.get("/api/ops/noise").status_code == 200 and client.get("/api/ops/availability").status_code == 200
    assert client.get("/api/ops/escalation").json()["sms"] is False
    r = client.get("/api/ops/config/export", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert client.get("/login/sso").status_code in (303, 307, 400)


# ============================================================ review fixes

def test_two_person_env_is_a_floor(fresh, monkeypatch, tmp_path):
    from core import sap_actions as sa
    (tmp_path / "config" / "action_policy.yaml").write_text(
        "default: confirm\nrules:\n  - {systems: ['*'], actions: ['*'], approval: confirm}\n", encoding="utf-8")
    assert sa.mode_for("PS4", "restart_job") == "confirm"
    monkeypatch.setenv("IBO_ACTIONS_TWO_PERSON", "1")
    assert sa.mode_for("PS4", "restart_job") == "two_person"


def test_check_alert_key_keeps_names_with_digits(fresh, monkeypatch):
    from core import check_alerts
    from core.models import MetricResult, Status
    sent = _capture_mail(monkeypatch)
    cfg = {"name": "PS4"}

    def m(detail):
        return MetricResult(name="sap.stms.failed_imports", value=1, display_value="1", status=Status.WARNING,
                            detail=detail, source="rfc")
    assert check_alerts._mail(cfg, m("K900111 RC 8 at 08:00"))
    assert not check_alerts._mail(cfg, m("K900111 RC 8 at 09:15"))     # same problem, time moved
    assert check_alerts._mail(cfg, m("K900456 RC 8 at 09:20"))         # another transport: at once
    assert len(sent) == 2


def test_expected_job_times_and_transport_steps(fresh, monkeypatch):
    from collectors import extra_checks as xc
    import yaml
    assert xc._clock("06:00") == "06:00" and xc._clock(yaml.safe_load("by: 23:59")["by"]) == "23:59"
    assert xc._clock("24:00") is None and xc._clock("7:65") is None and xc._clock(None) == "23:59"
    rows = [{"TRKORR": "K900111", "RETCODE": "0000", "TRTIME": "20261007080000", "TRSTEP": "H"},
            {"TRKORR": "K900111", "RETCODE": "0008", "TRTIME": "20261007080500", "TRSTEP": "A"},
            {"TRKORR": "K900111", "RETCODE": "0004", "TRTIME": "20261007081000", "TRSTEP": "G"}]
    fields = ("TRKORR", "RETCODE", "TRTIME", "TRSTEP")
    s = _XS(tables={"TPALOG": lambda w: rows},
            fms={"DDIF_FIELDINFO_GET": lambda TABNAME: {"DFIES_TAB": [{"FIELDNAME": f} for f in fields]}})
    imports, _ = xc.recent_imports(s, "PS4", days=1)
    assert imports == [{"trkorr": "K900111", "rc": 8, "at": "20261007081000", "step": "A"}]
    # imported again cleanly 3 hours later: the transport check is green again
    rows.append({"TRKORR": "K900111", "RETCODE": "0000", "TRTIME": "20261007111000", "TRSTEP": "A"})
    xc.forget()
    t = xc.check_transports(s, "PS4")
    assert t.status.value == "NORMAL" and t.value == 0, t.detail


def test_sap_clock_moves_on(fresh, monkeypatch):
    from collectors import extra_checks as xc, rfc_perf as rp
    learned = datetime(2026, 10, 7, 9, 0)
    with rp._sap_clock_lock:
        rp._sap_clock["PS4"] = learned
        rp._sap_clock_learned_at["PS4"] = datetime.now() - timedelta(hours=2)
    try:
        now = xc._sap_now("PS4")
        assert abs((now - (learned + timedelta(hours=2))).total_seconds()) < 5
    finally:
        with rp._sap_clock_lock:
            rp._sap_clock.pop("PS4", None)
            rp._sap_clock_learned_at.pop("PS4", None)


def test_ask_without_platform_events_for_scoped_users(fresh, monkeypatch):
    _systems(monkeypatch, [{"name": "PS4"}])
    from core import ask, timeline
    timeline.add("IBOPS", "error", "KeyError in core/secret_module.py")
    timeline.add("PS4", "state", "[SYSTEM DOWN]")
    assert "KeyError" in ask.answer("what happened today", ["PS4"])["answer"]
    a = ask.answer("what happened today", ["PS4"], platform=False)
    assert "KeyError" not in a["answer"] and "SYSTEM DOWN" in a["answer"]


def test_exception_counted_once_when_captured_and_logged(fresh):
    import logging
    from core import errors
    h = errors._ErrorLogHandler(level=logging.ERROR)
    lg = logging.getLogger("tests.r2.once")
    lg.propagate = False
    lg.addHandler(h)
    try:
        raise ValueError("boom 1")
    except ValueError as exc:
        errors.capture(exc, "sweep")
        lg.error("sweep failed", exc_info=True)
    rows = errors.listing()
    assert len(rows) == 1 and rows[0]["count"] == 1


def test_extra_checks_shared_between_processes(fresh, monkeypatch):
    from collectors import extra_checks as xc
    from core.models import Status
    runs = []

    def fake_run(session, system, cfg):
        runs.append(system)
        return [xc._metric("sap.sp01.spool_requests", 10, Status.NORMAL, "10 spool requests")]
    monkeypatch.setattr(xc, "run", fake_run)
    ok = types.SimpleNamespace(ok=True)
    m, age = xc.cached(ok, "PS4", {})
    assert len(runs) == 1 and m[0].value == 10 and age == 0.0
    with xc._cache_lock:
        xc._cache.clear()                       # another process: empty memory, same database
    m2, age2 = xc.cached(ok, "PS4", {})
    assert len(runs) == 1 and m2[0].display_value == "10" and m2[0].status == Status.NORMAL
    assert xc.stored("PS4")[0][0].detail == "10 spool requests"
    # a run in progress elsewhere: the stored result is served, nothing runs twice
    assert xc._claim("PS4") and not xc._claim("PS4")
    xc._release("PS4")
    # without RFC: run (certificates / HANA) but never stored over the full set
    m3, _ = xc.cached(types.SimpleNamespace(ok=False), "QAS", {})
    assert runs[-1] == "QAS" and xc.stored("QAS") == ([], None)
