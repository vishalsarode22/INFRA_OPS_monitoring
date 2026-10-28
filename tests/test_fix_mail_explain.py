"""Alert history in plain words: who got each mail (by role) and why."""
import pytest

from core import mail_explain as M

R = {"basis": ["staff.c@infrabeat.com"], "manager": ["staff.a@infrabeat.com"],
     "client": ["ops@client.com"], "report_to": ["ops@client.com"], "report_cc": ["staff.b@infrabeat.com"],
     "rca": ["staff.c@infrabeat.com"]}


def _row(**kw):
    base = {"system": "CEAT S4P", "metric": "sap.st22.dumps", "instance": "", "label": "ABAP dumps today",
            "level": "CRITICAL", "value": "3900", "limits": "warning >= 120, critical >= 150", "kind": "repeat",
            "channel": "email", "recipients": "staff.a@infrabeat.com, staff.c@infrabeat.com",
            "ok": 1, "note": "RED: Basis manager informed"}
    base.update(kw)
    return base


def test_reminder_names_roles_and_reason():
    e = M.explain(_row(), R)
    assert e["category"] == "Value alert"
    assert e["headline"].startswith("Reminder: ABAP dumps today still CRITICAL")
    assert "60 min" in e["why"] and "Basis manager" in e["why"]
    assert [p["role"] for p in e["people"]] == ["Manager", "Basis"]
    assert e["outcome"] == "Delivered by email"


def test_first_alert_goes_to_basis():
    e = M.explain(_row(kind="alert", note="", recipients="staff.c@infrabeat.com"), R)
    assert "First time" in e["why"] and e["people"][0]["role"] == "Basis"


def test_reachability_rows_are_explained():
    e = M.explain(_row(metric="not_reachable", label="NOT REACHABLE", kind="sent", value="CENTOR_QAS",
                       system="CENTOR_QAS", limits="", note=""), R)
    assert e["category"] == "System reachability" and e["headline"] == "System NOT REACHABLE"
    assert "2 checks in a row" in e["why"]
    e2 = M.explain(_row(metric="recovered", label="RECOVERED", kind="sent", level="INFO"), R)
    assert e2["headline"] == "Reachable again"


def test_not_sent_reasons_are_plain():
    e = M.explain(_row(kind="suppressed-cap", ok=0, note="suppressed-cap"), R)
    assert e["outcome"] == "Not sent: a mail limit was reached"
    e = M.explain(_row(kind="alert", ok=0, note="held: acknowledged by alex"), R)
    assert e["outcome"].startswith("Held")


def test_client_address_is_flagged_as_client():
    e = M.explain(_row(metric="report", kind="sent", recipients="ops@client.com, staff.b@infrabeat.com"), R)
    roles = {p["address"]: p["role"] for p in e["people"]}
    assert roles["ops@client.com"] == "Client" and e["category"] == "Monitoring report"


def test_routing_rules_and_missing_manager_is_said():
    r = dict(R, manager=[])
    info = M.routing({"name": "PS4"}, r)
    mgr = [l for l in info["lists"] if l["role"] == "Manager"][0]
    assert "NOT SET" in mgr["meaning"]
    whens = [x["when"] for x in info["rules"]]
    assert any("still bad" in w for w in whens) and any("NOT REACHABLE" in w for w in whens)
    assert "never" in info["client_never"]


def test_api_adds_explanations_and_routing(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import dashboard.app as A
    from core import alert_engine
    import dashboard.ops_api as O
    monkeypatch.setattr(alert_engine, "history", lambda *a, **k: [_row(at="2026-10-26 10:24:39")])
    monkeypatch.setattr(O, "_visible_systems", lambda: ["CEAT S4P"])
    monkeypatch.setattr(O, "_system_configs", lambda: {"CEAT S4P": {"name": "CEAT S4P"}})
    monkeypatch.setattr(M, "roles", lambda cfg: R)
    c = TestClient(A.app)
    d = c.get("/api/ops/alerts?days=1").json()
    assert d["rows"][0]["explain"]["people"][0]["role"] == "Manager"
    d = c.get("/api/ops/mail-routing").json()
    assert d["systems"][0]["system"] == "CEAT S4P" and d["systems"][0]["rules"]


def test_internal_cc_is_not_called_client_and_missing_manager_is_said():
    r = dict(R, client=[])
    e = M.explain(_row(metric="report", kind="sent", recipients="ops@client.com, staff.b@infrabeat.com"), r)
    roles = {p["address"]: p["role"] for p in e["people"]}
    assert roles["staff.b@infrabeat.com"] == "Infrabeat" and roles["ops@client.com"] == "Client"
    e = M.explain(_row(metric="not_reachable", kind="sent", note=""), dict(R, manager=[]))
    assert "no manager address is set" in e["why"] and "manager and Basis are told" not in e["why"]
