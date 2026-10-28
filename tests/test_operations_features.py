"""
Operations round: maintenance windows, snooze / acknowledge / park, the
alert gate in the ledger, timeline, mail action links, on-call rotation,
knowledge base, learned baselines, capacity forecasts, runbooks, SAP
actions (fake session), tickets (fake provider), config validation,
peer watch and the ops API routes.

Each test gets its own database file.
"""
from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_DB_PATH", str(tmp_path / "ops.sqlite"))
    monkeypatch.setenv("IBO_STATE_BACKEND", "sqlite")
    monkeypatch.setenv("IBO_AUTH", "0")
    monkeypatch.setenv("IBO_MAIL_ASYNC", "0")
    monkeypatch.setenv("IBO_TICKETS", "off")
    monkeypatch.delenv("IBO_PUBLIC_URL", raising=False)
    from core import store, ops_db
    importlib.reload(store)
    ops_db.reset_for_tests()
    from core import alert_ledger
    importlib.reload(alert_ledger)
    import core.alert_control as ac
    return ac


def _systems(monkeypatch, names=("PS4", "CARFOUR QAS"), net="Infrabeat VPN"):
    cfgs = [{"name": n, "client": "100", "network": {"name": net, "vpn": True}} for n in names]
    from core import config_loader
    monkeypatch.setattr(config_loader, "get_all_systems", lambda: [dict(c) for c in cfgs])
    monkeypatch.setattr(config_loader, "get_systems", lambda: [dict(c) for c in cfgs])
    return cfgs


# ----------------------------------------------------------- maintenance

def test_maintenance_window_blocks_alerts_and_sweeps(fresh, monkeypatch):
    ac = fresh
    _systems(monkeypatch)
    from core import alert_ledger
    assert alert_ledger.should_send("PS4", "system_down", "PS4", 300)[0]
    w = ac.create_window("PS4", datetime.now() - timedelta(minutes=1), datetime.now() + timedelta(hours=1),
                         "kernel upgrade")
    assert w["state" if "state" in w else "system"]
    ok, why = alert_ledger.should_send("PS4", "system_down", "PS4", 300)
    assert not ok and "maintenance" in why
    # recovery is held too (nothing to recover from during planned work)
    assert not alert_ledger.should_send("PS4", "recovered", "PS4", 60)[0]
    assert "maintenance" in ac.sweep_block_reason("PS4")
    assert ac.sweep_block_reason("CARFOUR QAS") is None
    ac.cancel_window(w["id"])
    assert ac.sweep_block_reason("PS4") is None
    assert alert_ledger.should_send("PS4", "credentials", "x", 0)[0]


def test_maintenance_validation(fresh, monkeypatch):
    ac = fresh
    _systems(monkeypatch)
    with pytest.raises(ValueError):
        ac.create_window("PS4", None, datetime.now() - timedelta(hours=1), "x")
    with pytest.raises(ValueError):
        ac.create_window("PS4", None, datetime.now() + timedelta(hours=1), "")
    with pytest.raises(ValueError):
        ac.create_window("PS4", None, datetime.now() + timedelta(days=30), "too long")


def test_star_window_covers_every_system_but_not_platform(fresh, monkeypatch):
    ac = fresh
    _systems(monkeypatch)
    ac.create_window("*", None, datetime.now() + timedelta(hours=2), "DC power work")
    from core import alert_ledger
    assert not alert_ledger.should_send("CARFOUR QAS", "critical", "YELLOW,cpu", 0)[0]
    assert not alert_ledger.should_send("network:Infrabeat VPN", "vpn_down", "Infrabeat VPN", 0)[0]
    assert alert_ledger.should_send("IBOPS", "monitor_down", "jobs", 0)[0]


# --------------------------------------------------------------- silences

def test_snooze_ack_until_fixed(fresh, monkeypatch):
    ac = fresh
    _systems(monkeypatch)
    from core import alert_ledger
    ac.silence("PS4", "system_down", "snooze", minutes=60, by="vik")
    ok, why = alert_ledger.should_send("PS4", "system_down", "PS4", 0)
    assert not ok and "snoozed by vik" in why
    assert alert_ledger.should_send("PS4", "credentials", "PS4", 0)[0]      # other kind still goes
    assert alert_ledger.should_send("PS4", "recovered", "PS4", 0)[0]        # recovery never snoozed
    ac.silence("PS4", "credentials", "ack", by="om")
    ac.silence("PS4", "*", "ack", by="om")
    assert not alert_ledger.should_send("PS4", "credentials", "PS4", 0)[0]
    # credentials fixed -> that ack is cleared; the wall-level '*' ack and the snooze stay
    assert ac.clear_until_fixed("PS4", {"credentials"}) == 1
    assert len(ac.active_silences("PS4")) == 2
    assert not alert_ledger.should_send("PS4", "system_down", "PS4", 0)[0]
    # explicit full clear (ticket closed) drops the '*' ack too
    assert ac.clear_until_fixed("PS4", None) == 1
    assert alert_ledger.should_send("PS4", "credentials", "PS4", 0)[0]
    assert not alert_ledger.should_send("PS4", "system_down", "PS4", 0)[0]   # snooze still on
    # until_fixed parks sweeps
    ac.silence("CARFOUR QAS", "*", "until_fixed", by="om")
    assert ac.parked("CARFOUR QAS") and "parked" in ac.sweep_block_reason("CARFOUR QAS")
    assert ac.unpark("CARFOUR QAS") and ac.parked("CARFOUR QAS") is None


def test_network_silence_needs_every_member(fresh, monkeypatch):
    ac = fresh
    _systems(monkeypatch)
    from core import alert_ledger
    ac.silence("PS4", "vpn_down", "snooze", minutes=30)
    assert alert_ledger.should_send("network:Infrabeat VPN", "vpn_down", "Infrabeat VPN", 0)[0]
    ac.silence("CARFOUR QAS", "vpn_down", "snooze", minutes=30)
    assert not alert_ledger.should_send("network:Infrabeat VPN:in_use", "vpn_down", "Infrabeat VPN:in_use", 0)[0]


def test_auto_park_on_credentials_and_recovery_clears_ack(fresh, monkeypatch, tmp_path):
    ac = fresh
    cfgs = _systems(monkeypatch)
    monkeypatch.setenv("IBO_REACH_ALERTS", "0")
    from core import reachability
    monkeypatch.setattr(reachability, "_STATE_PATH", str(tmp_path / "reach.json"))
    reachability.report("PS4", reachability.credentials_message("PS4", "Name or password is incorrect"), cfgs[0])
    assert ac.parked("PS4") and ac.parked("PS4")["auto"] == 1
    ac.silence("PS4", "credentials", "ack", by="om")
    reachability.report_ok("PS4", cfgs[0])
    assert ac.active_silences("PS4") == []
    from core import timeline
    cats = [e["category"] for e in timeline.query("PS4", 1)]
    assert "state" in cats and "park" in cats


# ---------------------------------------------------------------- ledger

def test_ledger_records_timeline_after_send(fresh, monkeypatch):
    _systems(monkeypatch)
    from core import alert_ledger, timeline
    assert alert_ledger.should_send("PS4", "system_down", "PS4", 300)[0]
    alert_ledger.record("PS4", "system_down", "PS4", "CRITICAL", "[SYSTEM DOWN] PS4", ["a@b.c"])
    ev = timeline.query("PS4", 1, ["alert"])
    assert ev and ev[0]["title"] == "[SYSTEM DOWN] PS4" and ev[0]["severity"] == "critical"
    alert_ledger.record("network:Infrabeat VPN", "vpn_down", "Infrabeat VPN", "WARNING", "[VPN] x", ["a@b.c"])
    assert timeline.query("CARFOUR QAS", 1, ["alert"])        # fanned out to the members


# ---------------------------------------------------------- action links

def test_action_links_round_trip_once(fresh, monkeypatch):
    ac = fresh
    _systems(monkeypatch)
    monkeypatch.setenv("IBO_PUBLIC_URL", "https://ibops.example")
    from core import action_links
    tok = action_links.make("snooze", "PS4", "system_down", minutes=60)
    body = action_links.verify(tok)
    assert body["a"] == "snooze" and body["s"] == "PS4" and body["m"] == 60
    msg = action_links.perform(tok, actor="Vik")
    assert msg.startswith("Done")
    s = ac.silenced("PS4", "system_down")
    assert s and s["mode"] == "snooze" and "Vik" in s["created_by"]
    with pytest.raises(ValueError):
        action_links.perform(tok)                       # single use
    with pytest.raises(ValueError):
        action_links.verify(tok[:-3] + "abc")           # tampered
    html = action_links.footer_html("PS4", "system_down")
    assert "Snooze 1 h" in html and "/access/action?t=" in html and "/runbook?kind=system_down" in html
    monkeypatch.setenv("IBO_PUBLIC_URL", "")
    assert action_links.footer_html("PS4", "system_down") == ""


def test_footer_only_for_internal_recipients(fresh, monkeypatch):
    _systems(monkeypatch)
    monkeypatch.setenv("IBO_PUBLIC_URL", "https://ibops.example")
    monkeypatch.setenv("IBO_INTERNAL_DOMAINS", "infrabeat.com")
    from notifications.mailer import with_action_footer
    assert "ibo-actions" in with_action_footer("<p>x</p>", "PS4", "system_down", ["a@infrabeat.com"])
    assert "ibo-actions" not in with_action_footer("<p>x</p>", "PS4", "system_down", ["a@infrabeat.com", "c@customer.com"])
    out = with_action_footer("<html><body><p>x</p></body></html>", "PS4", "system_down", ["a@infrabeat.com"])
    assert out.index("ibo-actions") < out.index("</body>")


# ------------------------------------------------------------------ oncall

def test_oncall_rotation_and_override(fresh, monkeypatch, tmp_path):
    from core import config_loader, oncall
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    (tmp_path / "oncall.yaml").write_text(
        "enabled: true\nrotation:\n  start: 2026-10-05\n  handover: '09:00'\n  length_days: 7\n"
        "  members:\n    - {name: A, email: a@x.com}\n    - {name: B, email: b@x.com}\n"
        "always_copy: [team@x.com]\n", encoding="utf-8")
    oncall._cfg_cache["stamp"] = None
    r1 = oncall.rotation_at(datetime(2026, 10, 6, 10))
    r2 = oncall.rotation_at(datetime(2026, 10, 13, 10))
    r3 = oncall.rotation_at(datetime(2026, 10, 12, 8))      # before handover: still week 1
    assert r1["emails"] == ["a@x.com"] and r2["emails"] == ["b@x.com"] and r3["emails"] == ["a@x.com"]
    cur = oncall.current()
    assert cur["emails"] and cur["always_copy"] == ["team@x.com"]
    oncall.add_override(datetime.now() - timedelta(hours=1), datetime.now() + timedelta(hours=1), "c@x.com", "leave")
    assert oncall.current()["emails"] == ["c@x.com"] and oncall.current()["source"] == "override"
    from core import recipients
    assert recipients.basis({}) == ["c@x.com", "team@x.com"]
    assert recipients.basis({"basis_emails": "own@x.com"}) == ["own@x.com"]
    with pytest.raises(ValueError):
        oncall.add_override("2026-10-10 09:00", "2026-10-09 09:00", "c@x.com")
    with pytest.raises(ValueError):
        oncall.add_override("2026-10-10 09:00", "2026-10-11 09:00", "not-an-email")


def test_oncall_absent_falls_back_to_env(fresh, monkeypatch, tmp_path):
    from core import config_loader, oncall, recipients
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    oncall._cfg_cache["stamp"] = None
    monkeypatch.setenv("IBO_BASIS_EMAILS", "x@y.z")
    assert recipients.basis({}) == ["x@y.z"]


# ---------------------------------------------------------- knowledge base

def test_knowledge_base_suggests_past_fix(fresh, monkeypatch):
    _systems(monkeypatch)
    from core import knowledge
    knowledge.add("TSV_TNEW_PAGE_ALLOC_FAILED in ZSD_BILLING_REPORT", "Raised abap/heap_area_dia; told user to filter",
                  rule="sap.st22.dumps", system="PS4", by="om", root_cause="selection too wide")
    knowledge.add("Job Z_STOCK_UPD cancelled", "Variant deleted; recreated it", rule="sap.sm37.cancelled_jobs", system="PS4")
    hits = knowledge.suggest("PS4", "sap.st22.dumps", "12 dumps: TSV_TNEW_PAGE_ALLOC_FAILED ZSD_BILLING_REPORT user X")
    assert hits and "heap_area" in hits[0]["fix"]
    assert "Last time this was fixed" in knowledge.mail_block("PS4", "sap.st22.dumps", "TSV_TNEW_PAGE_ALLOC_FAILED")
    assert knowledge.suggest("PS4", "system_down", "PS4 is DOWN") == []
    assert knowledge.mail_block("PS4", "vpn_down") == ""


def test_knowledge_from_resolved_incident(fresh, monkeypatch):
    _systems(monkeypatch)
    from core import knowledge
    from core.incidents import Incident, IncidentStatus
    from core.resolutions import ResolutionRecord, ResolutionCategory
    inc = Incident(incident_id="i1", system="PS4", client="100", rule_id="dump_spike", title="Dump spike",
                   severity="CRITICAL", status=IncidentStatus.RESOLVED, first_seen=datetime.now(),
                   last_seen=datetime.now(), evidence=["LOAD_PROGRAM_NOT_FOUND ZXYZ"])
    inc.resolution = ResolutionRecord(resolved_at=datetime.now(), category=ResolutionCategory.APPLICATION_PROCESS,
                                      summary="Re-imported transport K900123", resolver="om")
    e = knowledge.from_incident("PS4", inc)
    assert e and e["source"] == "incident" and "LOAD_PROGRAM_NOT_FOUND" in e["signature"]
    assert knowledge.suggest("PS4", "dump_spike", "LOAD_PROGRAM_NOT_FOUND ZXYZ")[0]["id"] == e["id"]


# ------------------------------------------------------- learned baselines

def _series(days=28, base=None):
    out = []
    start = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(days=days)
    for i in range(days * 24):
        t = start + timedelta(hours=i)
        v = (base(t) if base else (600 if t.weekday() < 5 and 8 <= t.hour <= 18 else 50))
        out.append((t, float(v)))
    return out


def test_learned_baseline_bands(fresh, monkeypatch):
    monkeypatch.setenv("IBO_LEARNED_MODE", "smart")
    from core import learned_baseline as lb
    series = _series()
    monday_10 = datetime.now()
    while monday_10.weekday() != 0:
        monday_10 += timedelta(days=1)
    monday_10 = monday_10.replace(hour=10, minute=0)
    a = lb.assess("PS4", "sap.sm12.lock_count", 650, monday_10, series)
    assert a["state"] == "normal" and "Mon 10:00" in a["basis"]
    sunday_3 = monday_10 - timedelta(days=1, hours=7)
    b = lb.assess("PS4", "sap.sm12.lock_count", 600, sunday_3, series)
    assert b["state"] == "high"
    assert lb.assess("PS4", "x", 1, monday_10, series[:30])["state"] == "learning"


def test_learned_baseline_regrades_metrics(fresh, monkeypatch):
    monkeypatch.setenv("IBO_LEARNED_MODE", "smart")
    from core import learned_baseline as lb
    from core.models import MetricResult, Status
    series = _series()
    monkeypatch.setattr(lb, "_load_system", lambda s, days=56: {"sap.sm12.lock_count": series,
                                                                 "sap.sm13.failed_updates": series})
    monkeypatch.setattr(lb, "_store_flags", lambda *a, **k: None)
    when = datetime.now()
    while when.weekday() != 1:
        when += timedelta(days=1)
    when = when.replace(hour=11)
    m1 = MetricResult("sap.sm12.lock_count", 640, "640", Status.WARNING)
    m2 = MetricResult("sap.sm12.lock_count", 999, "999", Status.CRITICAL)
    m3 = MetricResult("sap.sm13.failed_updates", 640, "640", Status.WARNING)
    lb.apply_to_metrics("PS4", [m1, m2, m3], when)
    assert m1.status == Status.NORMAL and "normal for Tue 11:00" in m1.detail
    assert m2.status == Status.CRITICAL
    assert m3.status == Status.WARNING          # never_downgrade
    sunday = when + timedelta(days=5)
    m4 = MetricResult("sap.sm12.lock_count", 600, "600", Status.NORMAL)
    lb.apply_to_metrics("PS4", [m4], sunday.replace(hour=3))
    assert m4.status == Status.WARNING and "UNUSUAL" in m4.detail
    monkeypatch.setenv("IBO_LEARNED_MODE", "annotate")
    m5 = MetricResult("sap.sm12.lock_count", 600, "600", Status.NORMAL)
    lb.apply_to_metrics("PS4", [m5], sunday.replace(hour=3))
    assert m5.status == Status.NORMAL and "UNUSUAL" in m5.detail


# ---------------------------------------------------------------- capacity

def test_capacity_forecast(fresh):
    from core import capacity
    now = datetime.now()
    series = [(now - timedelta(days=30 - d, hours=h), 60 + d * 0.5 + (h % 3) * 0.1) for d in range(30) for h in (1, 13)]
    f = capacity.forecast_series("disk_/usr/sap", series, {"limit": 100, "unit": "%"}, now)
    # 60 % + 0.5 %/day over 30 days = 74.6 % now; 25.4 % left at 0.5 %/day = about 51 days
    assert f["state"] == "growing" and 45 <= f["days_left"] <= 58 and f["confidence"] == "high"
    flat = capacity.forecast_series("disk_/", [(t, 40.0) for t, _ in series], {"limit": 100}, now)
    assert flat["state"] == "stable"
    short = capacity.forecast_series("disk_/", series[-6:], {"limit": 100}, now)
    assert short["state"] == "learning"
    full = capacity.forecast_series("disk_/", [(t, 100.0) for t, _ in series], {"limit": 100}, now)
    assert full["state"] == "full"
    assert capacity.spec_for("disk_/hana") and capacity.spec_for("db.ase.memory_pool_max_used_pct")
    assert capacity.spec_for("cpu") is None


def test_filesystems_become_disk_metrics(fresh, monkeypatch):
    from core.live_metrics import metrics_from_live
    payload = {"connected": True, "cpu": 10, "memory": 50, "load_1m": 1,
               "filesystems": [{"host": "h1", "mount": "/usr/sap", "used_pct": 91.0},
                               {"host": "h2", "mount": "/usr/sap", "used_pct": 45.0}]}
    ms = {m.name: m for m in metrics_from_live(payload, {})}
    assert ms["disk_/usr/sap"].value == 91.0 and ms["disk_/usr/sap"].status.value == "CRITICAL"


# ---------------------------------------------------------------- runbooks

def test_runbooks_and_ticks(fresh, monkeypatch):
    _systems(monkeypatch)
    from core import runbooks
    assert runbooks.for_alert("system_down")["id"] == "system_down"
    assert runbooks.for_alert("critical", "sap.st22.dumps")["id"] == "critical_dumps"
    assert runbooks.for_alert("something_new")["id"] == "generic"
    v = runbooks.view("system_down", "PS4")
    assert v["done"] == 0 and v["total"] >= 5
    runbooks.tick(v["run_key"], 0, True, "on it", by="om")
    v = runbooks.view("system_down", "PS4")
    assert v["done"] == 1 and v["steps"][0]["by"] == "om"
    with pytest.raises(ValueError):
        runbooks.tick(v["run_key"], 99, True)


# ------------------------------------------------------------- sap actions

class _FakeConn:
    """pyrfc-like interface description for the functions the actions use."""
    PARAMS = {
        "BAPI_XMI_LOGON": ["EXTCOMPANY", "EXTPRODUCT", "INTERFACE", "VERSION"],
        "BAPI_XBP_JOB_COPY": ["SOURCE_JOBNAME", "SOURCE_JOBCOUNT", "TARGET_JOBNAME", "EXTERNAL_USER_NAME"],
        "BAPI_XBP_JOB_START_IMMEDIATELY": ["JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME"],
        "BAPI_XBP_JOB_ABORT": ["JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME"],
        "ENQUE_READ2": ["GCLIENT", "GUNAME"], "ENQUE_DELETE": ["ENQ"], "TRFC_QIN_RESTART": ["QNAME"],
        "BAPI_USER_UNLOCK": ["USERNAME"],
    }

    def get_function_description(self, fm):
        import types
        if fm not in self.PARAMS:
            raise RuntimeError("FU_NOT_FOUND")
        return types.SimpleNamespace(parameters=[{"name": n, "direction": "RFC_IMPORT", "optional": False}
                                                 for n in self.PARAMS[fm]])


class _FakeSession:
    def __init__(self, system, cfg):
        self.system, self.cfg, self.ok, self.error, self.last_error = system, cfg, True, None, ""
        self.calls = []
        self.conn = _FakeConn()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read_table(self, table, fields, where="", rows=500):
        self.calls.append(("read", table, where))
        if table == "TBTCO":
            return [["Z_JOB", "12345678", "A", "OM", "20261007", "031500"]] if "Z_JOB" in where else []
        if table == "VBHDR":
            return [] if "LOCKUSER" in where else [["BATCH01"]]
        if table == "TRFCQIN":
            return [["QIN1"], ["QIN1"]] if ("QIN1" in where or not where) else []
        return []

    def call(self, name, **kw):
        self.calls.append((name, kw))
        if name == "ENQUE_READ2":
            old = (datetime.now() - timedelta(hours=3)).strftime("%Y%m%d%H%M%S")
            return {"ENQ": [{"GUNAME": "LOCKUSER", "GNAME": "VBAK", "GARG": "1000000123", "GUSR": old + "123456000900x"},
                            {"GUNAME": "BATCH01", "GNAME": "MARA", "GARG": "M1", "GUSR": old + "000000000900x"}]}
        if name == "BAPI_XMI_LOGON":
            return {"RETURN": {"TYPE": "S"}}
        if name == "BAPI_XBP_JOB_COPY":
            return {"TARGET_JOBCOUNT": "87654321", "RETURN": {"TYPE": "S"}}
        if name == "BAPI_XBP_JOB_START_IMMEDIATELY":
            return {"RETURN": {"TYPE": "S"}}
        if name == "ENQUE_DELETE":
            return {"SUBRC": 0}
        if name == "TRFC_QIN_RESTART":
            return {}
        return {}


def test_sap_actions_flow(fresh, monkeypatch):
    from collectors import extra_checks
    extra_checks.forget()
    cfgs = _systems(monkeypatch)
    for c in cfgs:
        c["rfc"] = {"ashost": "1.2.3.4"}
        c["actions"] = ["restart_job", "release_lock", "restart_queue"]
    from core import sap_actions as sa
    monkeypatch.setattr(sa, "_session", lambda system, cfg: _FakeSession(system, cfg))
    monkeypatch.setattr(sa, "_has_param", lambda *a: False)
    monkeypatch.setattr(sa, "_param_fields", lambda *a: None)
    with pytest.raises(ValueError, match="switched off"):
        sa.preview("PS4", "restart_job", {"job": "Z_JOB", "job_count": "12345678"})
    monkeypatch.setenv("IBO_ACTIONS_ENABLED", "1")
    pv = sa.preview("PS4", "restart_job", {"job": "Z_JOB", "job_count": "12345678"})
    assert "Z_JOB" in pv["text"]
    with pytest.raises(ValueError, match="not found"):
        sa.preview("PS4", "restart_job", {"job": "OTHER", "job_count": "1"})
    with pytest.raises(ValueError, match="update request"):
        sa.preview("PS4", "release_lock", {"user": "BATCH01"})
    assert "1 lock entry" in sa.preview("PS4", "release_lock", {"user": "LOCKUSER"})["text"]
    with pytest.raises(ValueError, match="reason"):
        sa.request("PS4", "restart_job", {"job": "Z_JOB", "job_count": "12345678"}, "", by="om")
    a = sa.request("PS4", "restart_job", {"job": "Z_JOB", "job_count": "12345678"}, "owner asked", by="om")
    assert a["status"] == "pending"
    monkeypatch.setenv("IBO_ACTIONS_TWO_PERSON", "1")
    with pytest.raises(ValueError, match="second person"):
        sa.approve(a["id"], by="om")
    done = sa.approve(a["id"], by="vik", wait=True)
    assert done["status"] == "done" and "87654321" in done["result"]
    with pytest.raises(ValueError):
        sa.approve(a["id"], by="vik")
    q = sa.request("PS4", "restart_queue", {"queue": "QIN1"}, "fixed SYSFAIL", by="om")
    assert sa.approve(q["id"], by="vik", wait=True)["status"] == "done"
    lk = sa.request("PS4", "release_lock", {"user": "LOCKUSER"}, "user gone", by="om")
    assert sa.approve(lk["id"], by="vik", wait=True)["status"] == "done"
    from core import timeline
    assert len(timeline.query("PS4", 1, ["action"])) >= 6
    assert sa.candidates("PS4", "release_lock")[0]["user"] == "LOCKUSER"
    assert sa.candidates("PS4", "restart_queue")[0]["queue"] == "QIN1"


# ------------------------------------------------------------------ tickets

def test_tickets_open_once_and_close_clears_alert(fresh, monkeypatch):
    ac = fresh
    _systems(monkeypatch)
    monkeypatch.setenv("IBO_TICKETS", "webhook")
    monkeypatch.setenv("IBO_TICKET_WEBHOOK_URL", "http://ticket.local/hook")
    from core import tickets
    sent = []

    class _R:
        def __init__(self, body):
            self._b = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._b

    class _Req:
        @staticmethod
        def post(url, **kw):
            sent.append((url, kw))
            return _R({"id": "TK-7", "url": "http://ticket.local/TK-7"})

    monkeypatch.setattr(tickets, "_requests", lambda: _Req)
    monkeypatch.setattr(tickets, "_screenshots", lambda snap, limit=3: [])
    from core.status_snapshot import load_snapshot  # noqa: F401
    monkeypatch.setattr("core.status_snapshot.load_snapshot", lambda s: {})
    assert tickets.wants_ticket("PS4", "critical", "YELLOW,cpu", "CRITICAL") is False
    assert tickets.wants_ticket("PS4", "critical", "RED,cpu", "CRITICAL") is True
    assert tickets.wants_ticket("network:X", "vpn_down", "", "") is False
    tickets.on_alert_sent("PS4", "system_down", "PS4", "CRITICAL", "[SYSTEM DOWN] PS4")
    import time
    for _ in range(50):
        t = tickets.open_ticket("PS4", "system_down")
        if t and t["status"] == "open":
            break
        time.sleep(0.05)
    assert t["status"] == "open" and t["external_id"] == "TK-7"
    tickets.on_alert_sent("PS4", "system_down", "PS4", "CRITICAL", "repeat")
    assert len([r for r in tickets.listing() if r["kind"] == "system_down"]) == 1
    ac.silence("PS4", "*", "until_fixed", by="om")
    ac.silence("PS4", "credentials", "ack", by="om")       # unrelated kind: must survive the close
    ac.park("CARFOUR QAS", "manual: hardware swap", by="om")
    closed = tickets.close(t["id"], by="om", resolution="Restarted the dispatcher after disk full")
    assert closed["status"] == "closed" and ac.parked("PS4") is None
    assert [x["kind"] for x in ac.active_silences("PS4")] == ["credentials"]
    assert ac.parked("CARFOUR QAS")                          # manual park elsewhere untouched
    from core import knowledge
    assert knowledge.suggest("PS4", "system_down", "")[0]["fix"].startswith("Restarted")


# ---------------------------------------------------------- config editor

def test_system_validation(fresh, monkeypatch, tmp_path):
    from core import config_loader, config_editor as ce
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    (tmp_path / "systems.yaml").write_text("systems:\n- name: PS4\n  client: '500'\n  rfc: {ashost: 1.2.3.4, "
                                          "username: u, password: '${PS4_RFC_PASSWORD}'}\n", encoding="utf-8")
    monkeypatch.delenv("PS4_RFC_PASSWORD", raising=False)
    errors, warnings = ce.validate_system({"name": "PS4", "client": "500"}, editing=False)
    assert any("already exists" in e for e in errors)
    errors, warnings = ce.validate_system({"name": "NEW", "client": "50", "rfc_ashost": "bad host!",
                                           "rfc_username": "", "alert_receivers": "nope", "network_check": "xyz"})
    joined = " ".join(errors)
    assert "3 digits" in joined and "RFC host" in joined and "RFC user" in joined and "e-mail" in joined \
        and "VPN check" in joined
    errors, warnings = ce.validate_system({"name": "PS4", "client": "500"}, editing=True)
    assert not errors and any("PS4_RFC_PASSWORD" in w for w in warnings)
    errors, _ = ce.validate_threshold("sap.st22.dumps", 10, 5)
    assert errors
    errors, vals = ce.validate_threshold("sap.st22.dumps", "5", "10", True)
    assert not errors and vals == {"warning": 5, "critical": 10, "inclusive": True}


def test_save_system_and_threshold_safely(fresh, monkeypatch, tmp_path):
    from core import config_loader, config_editor as ce
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    (tmp_path / "systems.yaml").write_text("systems: []\n", encoding="utf-8")
    (tmp_path / "thresholds.yaml").write_text("# limits\ncpu:\n  warning: 80  # company\n  critical: 90\n", encoding="utf-8")
    monkeypatch.setattr(config_loader, "_env_path", lambda: str(tmp_path / ".env"))
    res = ce.save_system({"name": "QAS", "client": "100", "rfc_ashost": "10.0.0.1", "rfc_username": "mon",
                          "rfc_password": "s3cret", "network_name": "Lab", "network_vpn": True,
                          "actions": ["restart_job"], "customer": "Metro", "has_gui_access": False})
    assert res["saved"]
    text = (tmp_path / "systems.yaml").read_text()
    assert "s3cret" not in text and "${QAS_RFC_PASSWORD}" in text and "Lab" in text and "restart_job" in text
    assert (tmp_path / "backups").is_dir()
    ex = ce.system_extras("QAS")
    assert ex["network_vpn"] and ex["customer"] == "Metro" and ex["actions"] == ["restart_job"]
    # edit keeps extras when the form omits them
    ce.save_system({"name": "QAS", "client": "100", "rfc_ashost": "10.0.0.1", "rfc_username": "mon",
                    "has_gui_access": False}, editing=True)
    assert ce.system_extras("QAS")["network_name"] == "Lab"
    saved = ce.save_threshold("cpu", 85, 95)
    t = (tmp_path / "thresholds.yaml").read_text()
    assert saved == {"warning": 85, "critical": 95} and "# company" in t and "# limits" in t
    ce.save_threshold("sap.new.metric", 1, 2, True)
    assert ce.read_thresholds()["sap.new.metric"] == {"warning": 1, "critical": 2, "inclusive": True}


# -------------------------------------------------------------- peer watch

def test_peer_watch_silent_and_recovery(fresh, monkeypatch):
    from core import peer_watch, store
    sent = []
    import notifications.mailer as mailer
    from core import alert_ledger

    def _fake_send(**kw):
        sent.append(kw["subject"])
        alert_ledger.record(kw["system"], kw["kind"], kw["key"], kw.get("severity", ""), kw["subject"], kw["to"])
        return True
    monkeypatch.setattr(mailer, "send_html_now", _fake_send)
    monkeypatch.setattr("core.config_loader.get_smtp_config", lambda *a, **k: {})
    monkeypatch.setenv("IBO_BASIS_EMAILS", "b@x.com")
    peer_watch.receive("WINVM", {"host": "WINVM"})
    assert peer_watch.check_peers() == 0 and not sent
    store.conn().execute("UPDATE heartbeats SET beat_at=? WHERE service='peer:WINVM'",
                         ((datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"),))
    assert peer_watch.check_peers() == 1 and "[WINDOWS VM SILENT]" in sent[-1]
    peer_watch.receive("WINVM", {})
    peer_watch.check_peers()
    assert "[RECOVERED]" in sent[-1]
    assert peer_watch.forget("WINVM") and peer_watch.peers() == []


# ------------------------------------------------------------------ routes

def test_ops_routes(fresh, monkeypatch, tmp_path):
    cfgs = _systems(monkeypatch)
    monkeypatch.setenv("IBO_API_TOKEN", "tok")
    monkeypatch.setenv("IBO_PUBLIC_URL", "http://127.0.0.1:8000")
    from fastapi.testclient import TestClient
    import dashboard.app as app_module
    monkeypatch.setattr(app_module, "get_systems", lambda: [dict(c) for c in cfgs])
    client = TestClient(app_module.app)
    h = {"X-IBO-Token": "tok"}
    r = client.post("/api/ops/maintenance", json={"system": "PS4", "minutes": 60, "reason": "test"}, headers=h)
    assert r.status_code == 200, r.text
    r = client.post("/api/ops/maintenance", json={"system": "PS4", "minutes": 60, "reason": ""}, headers=h)
    assert r.status_code == 400
    assert client.post("/api/ops/maintenance", json={"system": "PS4", "minutes": 60, "reason": "x"}).status_code in (401, 503)
    st = client.get("/api/ops/status").json()
    assert st["systems"]["PS4"]["maintenance"]["reason"] == "test"
    r = client.post("/api/ops/silence", json={"system": "CARFOUR QAS", "mode": "ack"}, headers=h)
    assert r.status_code == 200
    assert client.get("/api/ops/silences").json()["silences"][0]["mode"] == "ack"
    assert client.get("/api/timeline?system=PS4").json()["events"]
    assert client.get("/api/timeline?system=NOPE").status_code == 404
    assert client.get("/api/ops/runbook?kind=system_down&system=PS4").json()["runbook"]["id"] == "system_down"
    assert client.get("/api/ops/health").json()["groups"]
    for page in ("/ops", "/timeline", "/health", "/runbook", "/thresholds-page", "/wall"):
        assert client.get(page).status_code == 200, page
    assert client.get("/static/ops.js").status_code == 200
    # mail link page: no login, confirm page then action
    from core import action_links
    tok = action_links.make("ack", "PS4", "system_down")
    monkeypatch.setenv("IBO_AUTH", "1")
    r = client.get(f"/access/action?t={tok}")
    assert r.status_code == 200 and "Confirm" in r.text and "Acknowledge" in r.text
    r = client.post("/access/action", data={"t": tok, "name": "Om"})
    assert r.status_code == 200 and "Done" in r.text
    assert client.post("/access/action", data={"t": tok}).status_code == 400
    monkeypatch.setenv("IBO_AUTH", "0")
    assert client.get("/api/ops/thresholds").json()["thresholds"]
    assert client.get("/api/ops/capacity").status_code == 200
    r = client.post("/api/peer/heartbeat", json={"name": "WINVM", "detail": {}}, headers=h)
    assert r.status_code == 200
    assert any(x["name"] == "Peer WINVM" for g in client.get("/api/ops/health").json()["groups"] for x in g["rows"])
