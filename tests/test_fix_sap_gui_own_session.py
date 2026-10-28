"""
The monitor drives, logs on to, verifies and kills only what is its own
(findings 1, 2, 3 and 6 of the session-safety review). SAP GUI scripting,
pywinauto, tasklist/taskkill and the foreground window are faked.
"""
import threading
import types

import pytest

import sap_gui.connection as C
import sap_gui.launcher as L
import sap_gui.scripting_connection as S


class FakeCtl:
    def __init__(self, session, cid):
        self.session, self.cid = session, cid
        self.Text = ""
        self.Handle = session.hwnd

    def sendVKey(self, key):
        self.session.keys.append((self.cid, key))


class FakeSession:
    def __init__(self, user="", client="100", system="QAS", logon_screen=False, hwnd=4711):
        self.Info = types.SimpleNamespace(User=user, Client=client, SystemName=system,
                                          Transaction="SESSION_MANAGER" if user else "S000")
        self.logon_screen, self.hwnd, self.keys = logon_screen, hwnd, []

    def findById(self, cid):
        if "RSYST" in cid and not self.logon_screen:
            raise Exception(f"control not found: {cid}")
        return FakeCtl(self, cid)


class FakeConnection:
    def __init__(self, cid, desc="", sessions=None):
        self.Id, self.Description = cid, desc
        self._sessions = sessions if sessions is not None else [FakeSession()]

    @property
    def Children(self):
        sess = self._sessions

        class _C:
            Count = len(sess)

            def __call__(self, i):
                return sess[i]
        return _C()


class FakeEngine:
    def __init__(self):
        self.conns, self.opened = [], []

    def add(self, cid, desc="", sessions=None):
        c = FakeConnection(cid, desc, sessions)
        self.conns.append(c)
        return c

    def OpenConnection(self, desc, sync):
        self.opened.append((desc, sync))
        return self.add(f"/app/con[{len(self.conns)}]", desc,
                        [FakeSession(logon_screen=True)])

    @property
    def Children(self):
        conns = self.conns

        class _C:
            Count = len(conns)

            def __call__(self, i):
                return conns[i]
        return _C()


@pytest.fixture
def eng(monkeypatch):
    e = FakeEngine()
    monkeypatch.setattr(S, "_engine", lambda: e)
    monkeypatch.setattr(L.time, "sleep", lambda *_: None)
    S.unpin()
    yield e
    S.unpin()


# --------------------------------------------------------------------------
# 2. scripting_connection: never an arbitrary session
# --------------------------------------------------------------------------

def test_open_connection_pins_the_object_sap_returns(eng):
    eng.add("/app/con[0]", "Consultant PRD")
    conn = S.open_connection("Fuji QAS")
    assert eng.opened == [("Fuji QAS", False)]
    assert S.pinned_connection_id() == conn.Id == "/app/con[1]"
    assert S.get_scripting_session() is conn._sessions[0]


def test_the_only_connection_is_not_used_when_it_is_not_identified(eng):
    eng.add("/app/con[0]", "Consultant PRD", [FakeSession(user="ANITA", client="300")])
    with pytest.raises(RuntimeError, match="not driving a session"):
        S.get_scripting_session()


def test_identity_match_picks_only_the_monitors_session(eng):
    eng.add("/app/con[0]", "PRD", [FakeSession(user="ANITA", client="100", system="QAS")])
    mine = eng.add("/app/con[1]", "QAS", [FakeSession(user="IBOPS_MON", client="100", system="QAS")])
    S.register_identity(client="100", user="ibops_mon", system="qas")      # case-insensitive
    assert S.get_scripting_session() is mine._sessions[0]


def test_identity_matching_two_connections_is_refused(eng):
    eng.add("/app/con[0]", "", [FakeSession(user="MON", client="100")])
    eng.add("/app/con[1]", "", [FakeSession(user="MON", client="100")])
    with pytest.raises(RuntimeError, match="need exactly one"):
        S.get_scripting_session(client="100", user="MON")


def test_reused_connection_id_with_another_entry_is_not_ours(eng):
    S.open_connection("Fuji QAS")                     # /app/con[0]
    eng.conns.clear()
    eng.add("/app/con[0]", "Consultant PRD")          # id reused by someone else
    with pytest.raises(RuntimeError, match="no longer open"):
        S.get_scripting_session()


def test_pad_fallback_pins_only_a_connection_of_the_opened_entry(eng):
    before = S.connection_ids()
    eng.add("/app/con[0]", "Consultant PRD")          # someone else, same seconds
    eng.add("/app/con[1]", "Fuji QAS")
    assert S.pin_new_connection(before, timeout=1, description="fuji qas") == "/app/con[1]"


def test_pad_fallback_refuses_ambiguous_new_connections(eng):
    before = S.connection_ids()
    eng.add("/app/con[0]", "")
    eng.add("/app/con[1]", "")
    assert S.pin_new_connection(before, timeout=0.6) is None
    assert S.pinned_connection_id() is None


def test_unpin_forgets_the_identity(eng):
    S.register_identity(client="100", user="MON")
    S.unpin()
    assert S.identity() == {}


# --------------------------------------------------------------------------
# 3. logon through scripting; keystrokes only into our own window
# --------------------------------------------------------------------------

def test_scripting_logon_sets_fields_on_our_session_and_registers_identity(eng, monkeypatch):
    monkeypatch.setattr(C, "_LOGIN_SUBMIT_WAIT", 0)
    conn = S.open_connection("Fuji QAS")
    session = conn._sessions[0]
    typed = {}
    orig = session.findById

    def find(cid):
        ctl = orig(cid)
        if cid == "wnd[0]":
            def submit(key):
                session.logon_screen = False
                session.Info.User, session.Info.Transaction = typed.get("BNAME"), "SESSION_MANAGER"
            ctl.sendVKey = submit
        else:
            class _T:
                Text = ""

                def __setattr__(self, k, v):
                    object.__setattr__(self, k, v)
                    typed[cid.split("-")[-1]] = v
            return _T()
        return ctl

    session.findById = find
    out = C.login("100", "IBOPS_MON", "pw", submit=True, verify=True)
    assert out["success"] is True and out["method"] == "scripting"
    assert typed["BNAME"] == "IBOPS_MON" and typed["MANDT"] == "100"
    assert S.identity()["user"] == "IBOPS_MON"


def test_already_logged_on_session_is_detected_without_typing(eng):
    conn = S.open_connection("Fuji QAS")
    conn._sessions[0] = FakeSession(user="IBOPS_MON")     # SSO: no logon screen
    assert C.login_via_scripting("100", "IBOPS_MON", "pw", timeout=0) is True
    assert conn._sessions[0].keys == []


def test_keystroke_fallback_refuses_without_a_verified_own_window(eng, monkeypatch):
    monkeypatch.setattr(C, "login_via_scripting", lambda *a, **k: False)
    monkeypatch.setattr(C, "Desktop", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not look for windows")))
    with pytest.raises(RuntimeError, match="Refusing to type"):
        C.login("100", "MON", "pw", submit=True, verify=True)


def test_find_login_window_without_handle_refuses():
    with pytest.raises(RuntimeError, match="Refusing to type"):
        C.find_login_window(timeout=0)


def test_keystrokes_refused_when_another_window_has_focus(monkeypatch):
    sent = []
    window = types.SimpleNamespace(handle=4711, type_keys=lambda *a, **k: sent.append(a))
    monkeypatch.setattr(C, "_foreground_handle", lambda: 999)          # e.g. Notepad
    with pytest.raises(RuntimeError, match="not the monitor's own"):
        C.fill_login_fields(window, "100", "MON", "pw")
    assert sent == []
    monkeypatch.setattr(C, "_foreground_handle", lambda: 4711)
    C.submit_login(window)
    assert sent == [("{ENTER}",)]


def test_own_logon_handle_comes_from_the_pinned_session(eng):
    conn = S.open_connection("Fuji QAS")
    conn._sessions[0].hwnd = 1234
    assert C._own_logon_handle() == 1234
    S.unpin()
    assert C._own_logon_handle() is None


def test_verify_login_checks_the_sessions_user(eng):
    conn = S.open_connection("Fuji QAS")
    conn._sessions[0] = FakeSession(user="OTHER")
    assert C.verify_login_success(timeout=0, expected_user="IBOPS_MON") is False
    conn._sessions[0] = FakeSession(user="ibops_mon")
    assert C.verify_login_success(timeout=0, expected_user="IBOPS_MON") is True
    conn._sessions[0] = FakeSession(user="", logon_screen=True)
    assert C.verify_login_success(timeout=0) is False


# --------------------------------------------------------------------------
# 1 + 6. launcher
# --------------------------------------------------------------------------

class FakeProc:
    def __init__(self, pid, alive=True):
        self.pid, self.alive = pid, alive

    def poll(self):
        return None if self.alive else 0


@pytest.fixture
def launcher(monkeypatch, eng):
    calls = {"popen": [], "run": []}
    monkeypatch.setattr(L, "_save_pids", lambda: None)
    monkeypatch.setattr(L, "find_pad_title", lambda backend="win32": None)
    monkeypatch.setattr(L.subprocess, "run", lambda args, **k: calls["run"].append(args))
    monkeypatch.setattr(L, "_process_create_time", lambda pid: None)
    L._launched_pids.clear()
    L._launched_ctimes.clear()
    yield calls
    L._launched_pids.clear()
    L._launched_ctimes.clear()


def test_running_sap_logon_is_reused_never_claimed(launcher, monkeypatch):
    monkeypatch.setattr(L, "_sap_processes", lambda: [{"pid": 100, "image": "saplogon.exe"}])
    monkeypatch.setattr(L.subprocess, "Popen", lambda *a, **k: launcher["popen"].append(a))
    assert L.launch_saplogon("saplogon.exe", timeout=2) is None     # pad in tray, scripting up
    assert launcher["popen"] == []
    assert L._launched_pids == set()


def test_started_sap_logon_is_remembered_with_its_creation_time(launcher, monkeypatch):
    monkeypatch.setattr(L, "_sap_processes", lambda: [])
    monkeypatch.setattr(L.subprocess, "Popen", lambda *a, **k: FakeProc(4242))
    monkeypatch.setattr(L, "_process_create_time", lambda pid: 777)
    monkeypatch.setattr(L, "find_pad_title", lambda backend="win32": "SAP Logon 770")
    monkeypatch.setattr(L, "Desktop", lambda **k: types.SimpleNamespace(
        window=lambda **kw: types.SimpleNamespace(wait=lambda *a, **kk: None)))
    L.launch_saplogon("saplogon.exe", timeout=2)
    assert L._launched_pids == {4242} and L._launched_ctimes[4242] == 777


def test_handover_process_is_not_recorded(launcher, monkeypatch):
    monkeypatch.setattr(L, "_sap_processes", lambda: [])
    monkeypatch.setattr(L.subprocess, "Popen", lambda *a, **k: FakeProc(4242, alive=False))
    monkeypatch.setattr(L, "find_pad_title", lambda backend="win32": "SAP Logon")
    monkeypatch.setattr(L, "Desktop", lambda **k: types.SimpleNamespace(
        window=lambda **kw: types.SimpleNamespace(wait=lambda *a, **kk: None)))
    L.launch_saplogon("saplogon.exe", timeout=2)
    assert L._launched_pids == set()


def test_reused_pid_is_never_killed(launcher, monkeypatch):
    monkeypatch.setattr(L, "_sap_processes", lambda: [{"pid": 555, "image": "saplogon.exe"}])
    L._shared_at_launch = False
    L._launched_pids.add(555)
    L._launched_ctimes[555] = 111                       # what we started
    monkeypatch.setattr(L, "_process_create_time", lambda pid: 222)   # a different process now
    L.kill_sap_processes()
    assert launcher["run"] == []


def test_kill_never_uses_image_name_taskkill(launcher, monkeypatch):
    monkeypatch.setattr(L, "_sap_processes", lambda: [{"pid": 555, "image": "saplogon.exe"},
                                                     {"pid": 600, "image": "saplogon.exe"}])
    monkeypatch.delenv("IBO_DEDICATED_GUI_HOST", raising=False)
    L._shared_at_launch = False
    L._launched_pids.add(555)
    L.kill_sap_processes()
    assert launcher["run"] == [["taskkill", "/F", "/T", "/PID", "555"]]


def test_kill_from_a_thread_without_com_lists_connections_in_its_own_apartment(launcher, monkeypatch):
    monkeypatch.setattr(L, "_sap_processes", lambda: [{"pid": 100, "image": "saplogon.exe"}])
    S.open_connection("Fuji QAS")
    seen = {}
    real = S.connection_ids_guarded
    monkeypatch.setattr(S, "connection_ids_guarded",
                        lambda timeout=5.0: seen.setdefault("ids", real(timeout)))
    t = threading.Thread(target=L.kill_sap_processes)
    t.start(); t.join(10)
    assert "ids" in seen


def test_select_connection_uses_scripting_and_never_touches_the_pad(launcher, monkeypatch):
    monkeypatch.setattr(L, "_select_connection_on_pad",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("pad driven")))
    assert L.select_connection("Fuji QAS") is True
    assert S.pinned_connection_id() is not None


def test_select_connection_fails_when_its_connection_cannot_be_identified(launcher, monkeypatch, eng):
    def no_open(desc, sync):
        raise Exception("entry not found")
    eng.OpenConnection = no_open
    monkeypatch.setattr(L, "_select_connection_on_pad", lambda *a, **k: True)
    monkeypatch.setattr(S, "pin_new_connection", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="could not identify the connection"):
        L.select_connection("Fuji QAS", timeout=1)


def test_select_connection_timeout_matches_launch():
    import inspect
    assert inspect.signature(L.select_connection).parameters["timeout"].default == 30
    assert inspect.signature(L.launch_saplogon).parameters["timeout"].default == 30


def test_pad_entry_not_activated_when_another_window_has_focus(monkeypatch):
    actions = []
    item = types.SimpleNamespace(select=lambda: actions.append("select"),
                                 type_keys=lambda k: actions.append(k),
                                 set_focus=lambda: None,
                                 double_click_input=lambda: actions.append("click"))
    monkeypatch.setattr(L.time, "sleep", lambda *_: None)
    monkeypatch.setattr(L, "_foreground_handle", lambda: 999)       # Notepad in front
    assert L._activate(item, "Fuji QAS", pad_handle=123) is False
    assert "{ENTER}" not in actions and "click" not in actions
    monkeypatch.setattr(L, "_foreground_handle", lambda: 123)
    assert L._activate(item, "Fuji QAS", pad_handle=123) is True
    assert "{ENTER}" in actions
