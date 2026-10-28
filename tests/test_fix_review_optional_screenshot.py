"""
Review fix 6: capture_screenshot re-raises only a genuinely lost session
(is_session_lost), so an optional screenshot that fails with "control not
found" / "wnd[0]" / "session" text inside an action's try/except no longer
fails the T-code, triggers recovery or mail. Zero screenshots in total is
still a failed capture.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fix_sap_gui_session_errors import (  # noqa: E402
    DISCONNECTED, FakeComError, _Sess, collector)  # noqa: F401

import sap_gui.reliability as R  # noqa: E402

BROAD = [RuntimeError("The control could not be found by id. wnd[0]/usr/cntlGRID"),
         RuntimeError("session busy: element not found"),
         RuntimeError("wnd[0] is not available")]


@pytest.mark.parametrize("exc", BROAD)
def test_screenshot_returns_empty_for_non_lost_errors(tmp_path, exc):
    from sap_gui.screenshot import capture_screenshot
    assert capture_screenshot(_Sess(exc), "SM50", output_dir=str(tmp_path)) == ""


@pytest.mark.parametrize("hresult", [-2147417848, -2147023174, -2147417851])
def test_screenshot_reraises_disconnected_server_hresults(tmp_path, hresult):
    from sap_gui.screenshot import capture_screenshot
    with pytest.raises(FakeComError):
        capture_screenshot(_Sess(FakeComError(hresult, "x")), "SM50", output_dir=str(tmp_path))


def test_optional_screenshot_failure_does_not_fail_the_tcode(collector, monkeypatch):
    g = collector
    recovered = []
    monkeypatch.setattr(g, "get_scripting_session", lambda: "s1")
    monkeypatch.setattr(g, "recover_session", lambda s: recovered.append(s))
    calls = {"n": 0}

    def shot(session, name, output_dir=None):
        calls["n"] += 1
        if "optional" in name:
            raise RuntimeError("The control could not be found by id. wnd[1]/usr")
        return f"{output_dir}/{name}.png"
    monkeypatch.setattr(g, "capture_screenshot", shot)

    def action(session, capture):
        capture()                               # main screen
        try:
            capture("optional")                 # e.g. a detail popup that may not exist
        except Exception:
            pass
        return {"value": 1}
    monkeypatch.setitem(g.ACTIONS, "fake_action", action)
    out = g.collect_tcode_evidence([{"tcode": "SM37", "action": "fake_action"}], system="X")
    assert out[0].display_value == "captured (1)"
    assert recovered == []
    assert calls["n"] == 2                      # one attempt, not three


def test_optional_screenshot_failing_with_zero_total_still_fails(collector, monkeypatch):
    g = collector
    monkeypatch.setattr(g, "get_scripting_session", lambda: "s1")
    monkeypatch.setattr(g, "recover_session", lambda s: None)
    monkeypatch.setattr(g, "capture_screenshot", lambda *a, **k: "")
    monkeypatch.setitem(g.ACTIONS, "fake_action", lambda s, c: {"value": 1})
    out = g.collect_tcode_evidence([{"tcode": "SM12", "action": "fake_action"}], system="X")
    assert out[0].display_value == "failed"


def test_is_session_lost_is_narrow():
    for exc in BROAD:
        assert not R.is_session_lost(exc)
    assert R.is_session_lost(DISCONNECTED)
