"""
Round 28: the SAP logon is filled through the scripting API instead of typed,
so a sweep no longer fails when another window takes focus. Three PS4 sweeps
failed that way ("Logon window changed under us").
"""
import time

import pytest

import sap_gui.connection as conn


class _Ctl:
    def __init__(self, session, control_id):
        self.session, self.id, self.Text = session, control_id, ""
        self.selected = False

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name == "Text" and value and getattr(self, "session", None) is not None:
            self.session.typed[self.id] = value

    def sendVKey(self, key): self.session.keys.append((self.id, key))
    def press(self): self.session.keys.append((self.id, "press"))
    def Select(self): self.selected = True; self.session.selected.append(self.id)


class _Session:
    def __init__(self, known):
        self.known, self.typed, self.keys, self.selected = set(known), {}, [], []
        self.controls = {}

    def findById(self, control_id):
        if control_id not in self.known:
            raise Exception(f"control not found: {control_id}")
        return self.controls.setdefault(control_id, _Ctl(self, control_id))


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)


def _use(monkeypatch, session):
    import sap_gui.scripting_connection as sc
    monkeypatch.setattr(sc, "get_scripting_session", lambda: session)


def test_logon_fields_are_set_without_keystrokes(monkeypatch):
    session = _Session(["wnd[0]", "wnd[0]/usr/txtRSYST-MANDT", "wnd[0]/usr/txtRSYST-BNAME",
                        "wnd[0]/usr/pwdRSYST-BCODE", "wnd[0]/usr/txtRSYST-LANGU"])
    _use(monkeypatch, session)
    assert conn.login_via_scripting("500", "ps4_admin", "secret", "EN") is True
    assert session.typed == {"wnd[0]/usr/txtRSYST-MANDT": "500",
                             "wnd[0]/usr/txtRSYST-BNAME": "ps4_admin",
                             "wnd[0]/usr/pwdRSYST-BCODE": "secret",
                             "wnd[0]/usr/txtRSYST-LANGU": "EN"}
    assert session.keys == [("wnd[0]", 0)], "submitted with Enter, nothing typed at the desktop"


def test_it_falls_back_to_typing_when_the_logon_screen_is_not_there(monkeypatch):
    _use(monkeypatch, _Session(["wnd[0]"]))          # no logon fields
    monkeypatch.setattr(conn, "_LOGIN_SUBMIT_WAIT", 0, raising=False)
    assert conn.login_via_scripting("500", "u", "p", timeout=0) is False


def test_multiple_logon_never_ends_someone_elses_session(monkeypatch):
    session = _Session(["wnd[0]", "wnd[0]/usr/txtRSYST-BNAME", "wnd[0]/usr/pwdRSYST-BCODE",
                        "wnd[1]", "wnd[1]/usr/radMULTI_LOGON_OPT2", "wnd[1]/tbar[0]/btn[0]"])
    _use(monkeypatch, session)
    conn.login_via_scripting("500", "ps4_admin", "secret")
    assert session.selected == ["wnd[1]/usr/radMULTI_LOGON_OPT2"]
    assert ("wnd[1]/tbar[0]/btn[0]", "press") in session.keys

    # An unrecognised dialog is cancelled, not confirmed.
    other = _Session(["wnd[0]", "wnd[0]/usr/txtRSYST-BNAME", "wnd[0]/usr/pwdRSYST-BCODE", "wnd[1]"])
    _use(monkeypatch, other)
    conn.login_via_scripting("500", "ps4_admin", "secret")
    assert ("wnd[1]", 12) in other.keys


def test_keyboard_mode_can_be_forced(monkeypatch):
    calls = []
    monkeypatch.setenv("IBO_GUI_LOGIN_MODE", "keyboard")
    monkeypatch.setattr(conn, "login_via_scripting", lambda *a, **k: calls.append(a) or True)
    monkeypatch.setattr(conn, "find_login_window", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("typed path")))
    with pytest.raises(RuntimeError, match="typed path"):
        conn.login("500", "u", "p")
    assert calls == [], "scripting is skipped when the mode is keyboard"
