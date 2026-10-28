"""Black-box test findings 28.10.2026 -- input validation and scoping."""
import pytest


def test_alert_routing_is_validated_and_merged(monkeypatch):
    from core import profiles
    saved = {}
    monkeypatch.setattr(profiles, "save_profiles", lambda p, alerts=None: saved.update(alerts=alerts))
    monkeypatch.setattr(profiles, "load_profiles", lambda: [])
    monkeypatch.setattr(profiles, "_load_raw", lambda: {})
    for bad in ({"BOGUS": {"deliver": ["email"]}}, {"WARNING": "x"}, {"WARNING": {"deliver": "email"}},
                {"WARNING": {"repeat_after_minutes": 10 ** 9}}, [], {}):
        with pytest.raises(ValueError):
            profiles.save_alerts(bad)
    out = profiles.save_alerts({"warning": {"deliver": ["email"], "repeat_after_minutes": 60}})
    assert out["CRITICAL"]["deliver"] == ["email", "whatsapp"]      # not dropped any more
    assert out["WARNING"] == {"deliver": ["email"], "repeat_after_minutes": 60}


def test_query_numbers_nan_fall_back():
    from dashboard.ops_api import _fin
    assert _fin("NaN", 7) == 7 and _fin("inf", 7) == 7 and _fin("x", 7) == 7 and _fin("3", 7) == 3


def test_system_form_rejects_objects_where_text_is_expected():
    from core import config_editor
    errors, _w = config_editor.validate_system({"name": {"x": 1}, "rfc_ashost": ["a"]})
    assert errors and all("must be text" in e for e in errors)


def test_oncall_override_accepts_a_list_of_addresses(monkeypatch):
    from core import oncall
    with pytest.raises(ValueError):
        oncall.add_override("2026-10-28 10:00", "2026-10-28 09:00", ["a@b.com"])   # end before start, no crash
