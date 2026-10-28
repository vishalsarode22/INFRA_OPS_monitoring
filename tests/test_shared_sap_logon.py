"""
The monitor must work alongside a person's open SAP Logon:

  * it drives only its own (pinned) connection, never Children(0);
  * cleanup after a sweep, after a hung sweep (watchdog) and after an RCA
    closes only the monitor's connection;
  * SAP Logon itself is killed only when nothing but the monitor was in it;
  * no keystrokes go to the desktop when other sessions are open.

The SAP GUI scripting engine and tasklist/taskkill are faked.
"""
import types

import sap_gui.launcher as L
import sap_gui.scripting_connection as S
import sap_gui.connection as C


class FakeSession:
    def __init__(self, user="", tx="S000"):
        self.Info = types.SimpleNamespace(User=user, Transaction=tx, SystemName="QAS", Client="100")


class FakeConnection:
    def __init__(self, cid, engine, sessions=1, hang=False):
        self.Id, self._engine, self.hang = cid, engine, hang
        self._sessions = [FakeSession() for _ in range(sessions)]
        self.closed = False

    @property
    def Children(self):
        sess = self._sessions

        class _C:
            Count = len(sess)

            def __call__(self, i):
                return sess[i]
        return _C()

    def CloseConnection(self):
        if self.hang:
            import time
            time.sleep(60)
        self.closed = True
        self._engine.conns.remove(self)


class FakeEngine:
    def __init__(self):
        self.conns = []

    def open(self, cid, **kw):
        c = FakeConnection(cid, self, **kw)
        self.conns.append(c)
        return c

    @property
    def Children(self):
        conns = self.conns

        class _C:
            Count = len(conns)

            def __call__(self, i):
                return conns[i]
        return _C()


def _setup(monkeypatch, engine, processes):
    monkeypatch.setattr(S, "_engine", lambda: engine)
    monkeypatch.setattr(L, "_sap_processes", lambda: list(processes))
    monkeypatch.setattr(L, "_save_pids", lambda: None)
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    killed = []
    monkeypatch.setattr(L.subprocess, "run", lambda args, **k: killed.append(args))
    S.unpin()
    L._launched_pids.clear()
    return killed


def test_monitor_pins_its_own_connection_not_the_persons(monkeypatch):
    eng = FakeEngine()
    eng.open("/app/con[0]")                       # the consultant's session
    _setup(monkeypatch, eng, [{"pid": 100, "image": "saplogon.exe", "user": "alex"}])
    before = S.connection_ids()
    eng.open("/app/con[1]")                       # the monitor's new connection
    assert S.pin_new_connection(before, timeout=1) == "/app/con[1]"
    assert S.get_scripting_session() is eng.conns[1]._sessions[0]


def test_unpinned_with_several_connections_refuses_to_guess(monkeypatch):
    eng = FakeEngine()
    eng.open("/app/con[0]"); eng.open("/app/con[1]")
    _setup(monkeypatch, eng, [])
    try:
        S.get_scripting_session()
        assert False, "should refuse"
    except RuntimeError as exc:
        assert "not driving a session" in str(exc)


def test_cleanup_closes_only_the_monitor_connection_and_keeps_sap_logon(monkeypatch):
    eng = FakeEngine()
    person = eng.open("/app/con[0]")
    killed = _setup(monkeypatch, eng, [{"pid": 100, "image": "saplogon.exe", "user": "alex"}])
    L._shared_at_launch = True
    L._launched_pids.add(4242)                    # our short-lived launcher process
    before = S.connection_ids()
    eng.open("/app/con[1]")
    S.pin_new_connection(before, timeout=1)

    L.kill_sap_processes()

    assert [c.Id for c in eng.conns] == ["/app/con[0]"] and not person.closed
    assert killed == []                           # SAP Logon not killed
    assert S.pinned_connection_id() is None


def test_cleanup_kills_sap_logon_when_only_the_monitor_used_it(monkeypatch):
    eng = FakeEngine()
    killed = _setup(monkeypatch, eng, [{"pid": 555, "image": "saplogon.exe", "user": "svc"}])
    L._shared_at_launch = False
    L._launched_pids.add(555)
    eng.open("/app/con[0]")
    S.pin_new_connection(set(), timeout=1)

    L.kill_sap_processes()

    assert eng.conns == []
    assert killed and killed[0][:4] == ["taskkill", "/F", "/T", "/PID"] and killed[0][4] == "555"


def test_stale_pid_that_is_no_longer_sap_logon_is_never_killed(monkeypatch):
    eng = FakeEngine()
    killed = _setup(monkeypatch, eng, [])          # no SAP process alive
    L._shared_at_launch = False
    L._launched_pids.add(777)                      # PID reused by something else
    L.kill_sap_processes()
    assert killed == []


def test_watchdog_on_hung_monitor_connection_spares_other_sessions(monkeypatch):
    eng = FakeEngine()
    person = eng.open("/app/con[0]")
    killed = _setup(monkeypatch, eng, [{"pid": 100, "image": "saplogon.exe", "user": "alex"}])
    L._shared_at_launch = True
    before = S.connection_ids()
    eng.open("/app/con[1]", hang=True)            # our session is hung
    S.pin_new_connection(before, timeout=1)
    monkeypatch.setattr(S, "close_pinned_connection",
                        lambda timeout=8.0, _orig=S.close_pinned_connection: _orig(timeout=0.2))

    L.kill_sap_processes()                        # what main.py's watchdog calls

    assert person in eng.conns and not person.closed
    assert killed == []                           # never kills a process with someone's work


def test_watchdog_on_hung_connection_kills_monitor_only_sap_logon(monkeypatch):
    eng = FakeEngine()
    killed = _setup(monkeypatch, eng, [{"pid": 900, "image": "saplogon.exe", "user": "svc"}])
    L._shared_at_launch = False
    L._launched_pids.add(900)
    eng.open("/app/con[0]", hang=True)
    S.pin_new_connection(set(), timeout=1)
    monkeypatch.setattr(S, "close_pinned_connection",
                        lambda timeout=8.0, _orig=S.close_pinned_connection: _orig(timeout=0.2))

    L.kill_sap_processes()

    assert killed and killed[0][4] == "900"       # hung SAP Logon was the monitor's alone


def test_login_never_types_when_other_sessions_are_open(monkeypatch):
    eng = FakeEngine()
    eng.open("/app/con[0]")                       # person
    _setup(monkeypatch, eng, [])
    before = S.connection_ids()
    eng.open("/app/con[1]")
    S.pin_new_connection(before, timeout=1)
    monkeypatch.setattr(C, "login_via_scripting", lambda *a, **k: False)
    monkeypatch.setattr(C, "find_login_window",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not type")))
    out = C.login("100", "MON", "pw", submit=True, verify=True)
    assert out["success"] is False and out["method"] == "skipped-shared-desktop"


def test_login_success_is_checked_on_our_session_when_shared(monkeypatch):
    eng = FakeEngine()
    eng.open("/app/con[0]")
    _setup(monkeypatch, eng, [])
    before = S.connection_ids()
    mine = eng.open("/app/con[1]")
    S.pin_new_connection(before, timeout=1)
    mine._sessions[0].Info.User, mine._sessions[0].Info.Transaction = "MON", "SESSION_MANAGER"
    monkeypatch.setattr(C, "login_via_scripting", lambda *a, **k: True)
    monkeypatch.setattr(C, "verify_login_success",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("title check used")))
    assert C.login("100", "MON", "pw", submit=True, verify=True)["success"] is True


def test_empty_user_is_refused_instead_of_hanging_on_the_logon_screen(monkeypatch):
    monkeypatch.setattr(C, "login_via_scripting",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not try")))
    import pytest
    with pytest.raises(ValueError, match="user for this system is empty"):
        C.login("500", "", "secret", submit=True, verify=True)
