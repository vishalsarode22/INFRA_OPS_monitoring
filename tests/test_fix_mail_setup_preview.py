"""Systems form 'Who gets mails' preview: effective lists, where they come from, warnings."""
import pytest

from core import mail_explain as M


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("IBO_BASIS_EMAILS", "ALERT_TO_EMAILS", "ALERT_CC_EMAILS", "ALERT_MANAGER_EMAILS",
              "IBO_MANAGER_EMAILS", "PS4_MANAGER_RECEIVERS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("IBO_INTERNAL_DOMAINS", "infrabeat.com")
    from core import alert_teams, oncall
    monkeypatch.setattr(alert_teams, "addresses", lambda *a, **k: [])
    monkeypatch.setattr(oncall, "basis_emails", lambda: [])
    import core.profiles as P
    monkeypatch.setattr(P, "load_profiles", lambda: [])


def test_form_values_win_and_sources_are_named():
    d = M.preview({"name": "PS4"}, {"basis_emails": "staff.e@infrabeat.com",
                                    "manager_receivers": "staff.a@infrabeat.com",
                                    "alert_receivers": "sap.basis@client.com", "alert_receivers_cc": ""})
    assert d["basis"]["addresses"] == ["staff.e@infrabeat.com"] and d["basis"]["source"] == "this form"
    assert d["manager"]["addresses"] == ["staff.a@infrabeat.com"]
    assert d["report_to"]["addresses"] == ["sap.basis@client.com"]
    assert d["report_cc"]["source"] == "not set"
    assert not any("No Basis manager" in w for w in d["warnings"])


def test_unset_env_placeholder_is_flagged(monkeypatch):
    d = M.preview({"name": "PS4"}, {"manager_receivers": "${PS4_MANAGER_RECEIVERS}"})
    assert d["manager"]["addresses"] == []
    assert any("PS4_MANAGER_RECEIVERS is not set" in w and "NO manager" in w for w in d["warnings"])
    monkeypatch.setenv("PS4_MANAGER_RECEIVERS", "staff.a@infrabeat.com")
    d = M.preview({"name": "PS4"}, {"manager_receivers": "${PS4_MANAGER_RECEIVERS}"})
    assert d["manager"]["addresses"] == ["staff.a@infrabeat.com"]
    assert "PS4_MANAGER_RECEIVERS" in d["manager"]["source"]


def test_empty_fields_show_the_env_fallback(monkeypatch):
    monkeypatch.setenv("ALERT_MANAGER_EMAILS", "boss@infrabeat.com")
    monkeypatch.setenv("ALERT_TO_EMAILS", "ops@client.com")
    d = M.preview({"name": "PS4"}, {})
    assert d["manager"]["addresses"] == ["boss@infrabeat.com"] and "ALERT_MANAGER_EMAILS" in d["manager"]["source"]
    assert d["report_to"]["addresses"] == ["ops@client.com"] and "field empty" in d["report_to"]["source"]


def test_unset_placeholder_with_env_manager_falls_back_and_says_so(monkeypatch):
    monkeypatch.setenv("ALERT_MANAGER_EMAILS", "boss@infrabeat.com")
    d = M.preview({"name": "PS4"}, {"manager_receivers": "${PS4_MANAGER_RECEIVERS}"})
    assert d["manager"]["addresses"] == ["boss@infrabeat.com"]
    assert "because PS4_MANAGER_RECEIVERS is not set" in d["manager"]["source"]
    assert not any("NO manager" in w for w in d["warnings"]) and any("used instead" in i for i in d["info"])


def test_personal_address_in_alert_lists_is_warned():
    d = M.preview({"name": "PS4"}, {"basis_emails": "user.b.personal.example@gmail.com"})
    assert any("not an internal address" in w for w in d["warnings"])


def test_alert_teams_override_is_explained(monkeypatch):
    from core import alert_teams
    # this system's own Basis team wins over the form field ...
    monkeypatch.setattr(alert_teams, "own_addresses", lambda *a, **k: ["team@infrabeat.com"])
    d = M.preview({"name": "PS4"}, {"basis_emails": "someone@infrabeat.com"})
    assert d["basis"]["addresses"] == ["team@infrabeat.com"]
    assert any("is ignored" in w for w in d["warnings"])
    # ... but the standard (all systems) team does not
    monkeypatch.setattr(alert_teams, "own_addresses", lambda *a, **k: [])
    monkeypatch.setattr(alert_teams, "load", lambda: {"standard": {"Basis": ["std@infrabeat.com"]}, "systems": {}})
    d = M.preview({"name": "PS4"}, {"basis_emails": "someone@infrabeat.com"})
    assert d["basis"]["addresses"] == ["someone@infrabeat.com"]


def test_profile_with_its_own_report_list(monkeypatch):
    import core.profiles as P
    monkeypatch.setattr(P, "load_profiles", lambda: [
        {"id": "prd-quick", "label": "Quick check", "system": "PS4", "deliver": ["email"],
         "schedule": {"enabled": True, "mode": "hours", "every": 2, "at": "08:00"},
         "email_to": ["basis@client-a.example"], "email_cc": []}])
    d = M.preview({"name": "PS4"}, {"alert_receivers": "x@client.com", "alert_receivers_cc": "cc@infrabeat.com"})
    p = d["profiles"][0]
    assert p["to"] == ["basis@client-a.example"] and p["to_source"] == "the profile's own list"
    assert p["cc"] == ["cc@infrabeat.com"] and p["cc_source"] == "the system's Report Cc"
    assert any("own report list" in i for i in d["info"])


def test_endpoint(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import dashboard.app as A
    import dashboard.ops_api as O
    monkeypatch.setattr(O, "_system_configs", lambda: {"PS4": {"name": "PS4"}})
    monkeypatch.setattr(O, "_visible_systems", lambda: ["PS4"])
    c = TestClient(A.app, base_url="http://127.0.0.1")
    csrf = c.get("/api/me").json().get("csrf_token") or ""
    r = c.post("/api/ops/mail-preview", json={"system": "PS4", "fields": {"basis_emails": "a@infrabeat.com"}},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["basis"]["addresses"] == ["a@infrabeat.com"]
