"""
Round 34: a function module that SAP reports as FU_NOT_FOUND is not called
again on every poll. dev_rfc.log held ~92,000 such errors since 01.09.2026
(Z_GET_OBSERVABILITY_DATA, Z_GET_LOGON_LOAD, RZL_INTG_READALL_C,
TH_LOAD_DISTRIBUTION, TH_GET_LOAD_DISTRIBUTION).
"""
from collectors.rfc_collector import SapSession


class _FuNotFound(Exception):
    key = "FU_NOT_FOUND"


class _Conn:
    def __init__(self, missing=(), other_error=None):
        self.missing, self.other_error = set(missing), other_error
        self.calls = []

    def call(self, name, **kw):
        self.calls.append(name)
        if name in self.missing:
            raise _FuNotFound(f"Key: FU_NOT_FOUND, Message: ID:FL Type:E Number:046 {name}")
        if self.other_error:
            raise RuntimeError(self.other_error)
        return {"OK": name}


def _session(system, conn):
    s = SapSession(system, {})
    s.ok, s.conn = True, conn
    return s


def test_missing_fm_is_asked_once():
    conn = _Conn(missing={"Z_GET_OBSERVABILITY_DATA"})
    s = _session("R34A", conn)
    for _ in range(5):
        assert s.call("Z_GET_OBSERVABILITY_DATA") is None
    assert conn.calls.count("Z_GET_OBSERVABILITY_DATA") == 1
    assert "FU_NOT_FOUND" in s.last_error


def test_other_functions_and_systems_are_unaffected():
    conn = _Conn(missing={"Z_GET_LOGON_LOAD"})
    s = _session("R34B", conn)
    s.call("Z_GET_LOGON_LOAD")
    assert s.call("TH_WPINFO") == {"OK": "TH_WPINFO"}
    other = _session("R34C", _Conn())
    assert other.call("Z_GET_LOGON_LOAD") == {"OK": "Z_GET_LOGON_LOAD"}


def test_new_session_for_same_system_remembers():
    s1 = _session("R34D", _Conn(missing={"RZL_INTG_READALL_C"}))
    s1.call("RZL_INTG_READALL_C")
    conn2 = _Conn(missing={"RZL_INTG_READALL_C"})
    s2 = _session("R34D", conn2)
    assert s2.call("RZL_INTG_READALL_C") is None
    assert conn2.calls == []


def test_other_errors_are_retried_every_time():
    conn = _Conn(other_error="RFC_COMMUNICATION_FAILURE")
    s = _session("R34E", conn)
    s.call("TH_WPINFO"); s.call("TH_WPINFO")
    assert conn.calls.count("TH_WPINFO") == 2


def test_recheck_after_the_interval(monkeypatch):
    conn = _Conn(missing={"TH_LOAD_DISTRIBUTION"})
    s = _session("R34F", conn)
    s.call("TH_LOAD_DISTRIBUTION")
    monkeypatch.setattr(SapSession, "MISSING_FM_RECHECK_SECONDS", 0)
    s.call("TH_LOAD_DISTRIBUTION")
    assert conn.calls.count("TH_LOAD_DISTRIBUTION") == 2


def test_forget_missing_functions_asks_again():
    conn = _Conn(missing={"TH_GET_LOAD_DISTRIBUTION"})
    s = _session("R34G", conn)
    s.call("TH_GET_LOAD_DISTRIBUTION")
    SapSession.forget_missing_functions("R34G")
    s.call("TH_GET_LOAD_DISTRIBUTION")
    assert conn.calls.count("TH_GET_LOAD_DISTRIBUTION") == 2
