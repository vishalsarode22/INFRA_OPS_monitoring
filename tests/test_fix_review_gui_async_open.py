"""
Review fix 1: OpenConnection is asynchronous (a SAP GUI "script opens a
connection" prompt cannot block the sweep), the new connection is found by
id comparison, and the WarnOnConnection / WarnOnAttach options are reported.
"""
import sys
import types

import pytest

import sap_gui.launcher as L
import sap_gui.scripting_connection as S
from tests.test_fix_sap_gui_own_session import FakeEngine, FakeSession


@pytest.fixture
def eng(monkeypatch):
    e = FakeEngine()
    monkeypatch.setattr(S, "_engine", lambda: e)
    monkeypatch.setattr(L.time, "sleep", lambda *_: None)
    monkeypatch.setattr(S, "_security_checked", True)
    S.unpin()
    yield e
    S.unpin()


def test_open_is_asynchronous_and_pins_the_new_connection(eng):
    eng.add("/app/con[0]", "Consultant PRD")
    conn = S.open_connection("Fuji QAS", timeout=2)
    assert eng.opened == [("Fuji QAS", False)]
    assert S.pinned_connection_id() == conn.Id == "/app/con[1]"


def test_async_open_returning_none_is_found_by_id_comparison(eng):
    eng.add("/app/con[0]", "Consultant PRD")
    real = eng.OpenConnection

    def async_open(desc, sync):
        real(desc, sync)
        return None                               # async call returns before the object exists
    eng.OpenConnection = async_open
    S.open_connection("Fuji QAS", timeout=2)
    assert S.pinned_connection_id() == "/app/con[1]"


def test_connection_that_never_appears_raises_so_the_pad_is_used(eng, monkeypatch):
    eng.OpenConnection = lambda desc, sync: None  # prompt waiting: nothing appears
    with pytest.raises(RuntimeError, match="did not produce a new connection"):
        S.open_connection("Fuji QAS", timeout=0.6)
    assert S.pinned_connection_id() is None


def test_connection_without_session_is_unpinned_and_raises(eng):
    eng.OpenConnection = lambda desc, sync: eng.add("/app/con[0]", desc, [])
    with pytest.raises(RuntimeError, match="no session appeared"):
        S.open_connection("Fuji QAS", timeout=0.6)
    assert S.pinned_connection_id() is None


def test_select_connection_falls_back_to_pad_when_async_open_produces_nothing(eng, monkeypatch):
    monkeypatch.setenv("IBO_SAP_OPEN_CONNECTION_TIMEOUT", "1")
    eng.OpenConnection = lambda desc, sync: None
    pad = []

    def fake_pad(name, timeout):
        pad.append(name)
        eng.add("/app/con[0]", name, [FakeSession(logon_screen=True)])
        return True
    monkeypatch.setattr(L, "_select_connection_on_pad", fake_pad)
    assert L.select_connection("Fuji QAS", timeout=2) is True
    assert pad == ["Fuji QAS"]
    assert S.pinned_connection_id() == "/app/con[0]"


def _fake_winreg(values):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def query(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 4
    return types.SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=lambda *a: Key(),
                                 QueryValueEx=query)


def test_security_prompts_warned_once(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({"WarnOnConnection": 1,
                                                            "WarnOnAttach": 1}))
    monkeypatch.setattr(S, "_security_checked", False)
    warnings = []
    monkeypatch.setattr(S.log, "warning", lambda msg, *a, **k: warnings.append(msg))
    assert S.check_scripting_security_prompts() == ["WarnOnConnection", "WarnOnAttach"]
    assert S.check_scripting_security_prompts() == []           # once per process
    assert len(warnings) == 1
    assert "Accessibility & Scripting" in warnings[0]


def test_security_prompts_off_no_warning(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({"WarnOnConnection": 0,
                                                            "WarnOnAttach": 0}))
    monkeypatch.setattr(S, "_security_checked", False)
    assert S.check_scripting_security_prompts() == []


def test_warn_on_connection_absent_counts_as_default_on(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({}))
    monkeypatch.setattr(S, "_security_checked", False)
    assert S.check_scripting_security_prompts() == ["WarnOnConnection"]


def test_no_winreg_is_silent(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", None)            # import raises ImportError
    monkeypatch.setattr(S, "_security_checked", False)
    assert S.check_scripting_security_prompts() == []
