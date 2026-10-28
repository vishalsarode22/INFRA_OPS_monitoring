"""
Session-lost COM errors must reach the collector's recovery path, zero
screenshots is a failed capture, and threads that drive SAP GUI initialise
COM (findings 4 and 5 of the session-safety review).
"""
import sys
import threading
import types

import pytest

import sap_gui.reliability as R


class FakeComError(Exception):
    """Shaped like pywintypes.com_error: args[0] is the hresult."""

    def __init__(self, hresult, text):
        super().__init__(hresult, text, None, None)
        self.hresult = hresult


DISCONNECTED = FakeComError(-2147417848, "The object invoked has disconnected from its clients.")
SERVERFAULT = FakeComError(-2147417851, "The server threw an exception.")


# --------------------------------------------------------------------------
# reliability
# --------------------------------------------------------------------------

def test_disconnected_and_serverfault_are_session_lost():
    assert R.is_session_lost(DISCONNECTED)
    assert R.is_session_lost(SERVERFAULT)
    assert R.is_session_lost(FakeComError(0x80010108, "x"))        # unsigned spelling
    assert R.is_session_failure(SERVERFAULT), "used to be classified transient"


def test_missing_control_is_not_a_lost_session():
    exc = RuntimeError("The control could not be found by id. wnd[0]/usr/cntlGRID")
    assert not R.is_session_lost(exc)
    assert R.classify_gui_exception(RuntimeError("invalid password")) == "non_retryable"


def test_com_thread_initialises_and_releases_com(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(CoInitialize=lambda: calls.append("init"),
                                 CoUninitialize=lambda: calls.append("uninit"))
    monkeypatch.setitem(sys.modules, "pythoncom", fake)

    @R.com_thread_entry
    def worker():
        calls.append("work")
        return 7

    out = {}
    t = threading.Thread(target=lambda: out.setdefault("v", worker()))
    t.start(); t.join(5)
    assert out["v"] == 7
    assert calls == ["init", "work", "uninit"]


def test_com_thread_uninitialises_even_on_error(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(CoInitialize=lambda: calls.append("init"),
                                 CoUninitialize=lambda: calls.append("uninit"))
    monkeypatch.setitem(sys.modules, "pythoncom", fake)
    with pytest.raises(ValueError):
        with R.com_thread():
            raise ValueError("boom")
    assert calls == ["init", "uninit"]


def test_sweep_and_rca_thread_entries_are_wrapped():
    import evaluation.rca_pipeline as rp
    assert hasattr(rp.capture, "__wrapped__"), "RCA capture must run inside com_thread()"
    src = open("main.py", encoding="utf-8").read()
    assert "@com_thread_entry" in src and "def target():" in src
    i = src.index("@com_thread_entry")
    assert "def target():" in src[i:i + 200]


# --------------------------------------------------------------------------
# screenshot
# --------------------------------------------------------------------------

class _Wnd:
    def __init__(self, exc):
        self.exc = exc

    def hardCopy(self, *a):
        raise self.exc


class _Sess:
    def __init__(self, exc):
        self.exc = exc

    def findById(self, cid):
        return _Wnd(self.exc)


def test_screenshot_reraises_a_lost_session(tmp_path):
    from sap_gui.screenshot import capture_screenshot
    with pytest.raises(FakeComError):
        capture_screenshot(_Sess(DISCONNECTED), "SM50", output_dir=str(tmp_path))


def test_screenshot_other_errors_still_return_empty(tmp_path):
    from sap_gui.screenshot import capture_screenshot
    assert capture_screenshot(_Sess(OSError("disk full")), "SM50", output_dir=str(tmp_path)) == ""


# --------------------------------------------------------------------------
# rca_actions._guarded
# --------------------------------------------------------------------------

def test_guarded_rca_reraises_lost_session():
    from sap_gui.rca_actions import _guarded

    def action(session, capture):
        raise DISCONNECTED

    with pytest.raises(FakeComError):
        _guarded("sm50", action)(object(), lambda *a: "")


def test_guarded_rca_keeps_partial_facts_for_ordinary_errors():
    from sap_gui.rca_actions import _guarded

    def action(session, capture):
        raise KeyError("column")

    facts = _guarded("sm66", action)(object(), lambda *a: "")
    assert facts["rca"] == "sm66" and "KeyError" in facts["error"]


# --------------------------------------------------------------------------
# collector
# --------------------------------------------------------------------------

@pytest.fixture
def collector(monkeypatch, tmp_path):
    from collectors import sap_gui_collector as g
    import core.parser_health as ph
    monkeypatch.setattr(g, "system_evidence_root", lambda system: tmp_path)
    monkeypatch.setattr(g, "write_evidence_index", lambda *a, **k: None)
    monkeypatch.setattr(g, "get_ocr_patterns", lambda: {})
    monkeypatch.setattr(ph, "assess", lambda *a, **k: {"broken": [], "degraded": []})
    monkeypatch.setattr(R.time, "sleep", lambda *_: None)
    return g


def test_session_lost_swallowed_by_action_still_triggers_recovery(collector, monkeypatch):
    g = collector
    sessions = iter(["s1", "s2"])
    acquired, recovered = [], []
    monkeypatch.setattr(g, "get_scripting_session",
                        lambda: acquired.append(1) or next(sessions))
    monkeypatch.setattr(g, "recover_session", lambda s: recovered.append(s))

    def shot(session, name, output_dir=None):
        if session == "s1":
            raise DISCONNECTED
        return f"{output_dir}/{name}.png"

    monkeypatch.setattr(g, "capture_screenshot", shot)

    def action(session, capture):
        try:                       # many T-code actions swallow capture errors
            capture()
        except Exception:
            pass
        return {"value": 1}

    monkeypatch.setitem(g.ACTIONS, "fake_action", action)
    out = g.collect_tcode_evidence([{"tcode": "SM50", "action": "fake_action"}], system="X")
    assert recovered == ["s1"], "the collector's session recovery must run"
    assert len(acquired) == 2, "session re-acquired after the loss"
    assert out[0].display_value == "captured (1)"
    assert "session_recovery_after_attempt_1" in out[0].extra_data["recovery_actions"]


def test_zero_screenshots_is_a_failed_capture(collector, monkeypatch):
    g = collector
    monkeypatch.setattr(g, "get_scripting_session", lambda: "s1")
    monkeypatch.setattr(g, "capture_screenshot", lambda *a, **k: "")
    monkeypatch.setitem(g.ACTIONS, "fake_action", lambda s, c: {"value": 1})
    out = g.collect_tcode_evidence([{"tcode": "SM12", "action": "fake_action"}], system="X")
    assert out[0].display_value == "failed"
    assert out[0].screenshot_paths == []


def test_no_session_reports_every_tcode_failed_not_an_empty_success(collector, monkeypatch):
    g = collector

    def boom():
        raise RuntimeError("The monitor's SAP connection /app/con[1] is no longer open.")

    monkeypatch.setattr(g, "get_scripting_session", boom)
    out = g.collect_tcode_evidence([{"tcode": "SM50"}, {"tcode": "ST22"}], system="X")
    assert [r.tcode for r in out] == ["SM50", "ST22"]
    assert all(r.display_value == "failed" for r in out)
    assert all(r.extra_data["extraction_failed"] for r in out)
