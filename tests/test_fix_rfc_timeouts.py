"""
Finding 2: real call timeouts, and a dead connection is not called again.
"""
import pytest

from collectors import rfc_collector as rc


class CommunicationError(Exception):
    key = "RFC_COMMUNICATION_FAILURE"


class ExternalRuntimeError(Exception):
    key = "RFC_CANCELED"


class ABAPRuntimeError(Exception):
    key = "TIME_OUT"


def _cfg(**rfc):
    base = {"ashost": "h.example", "sysnr": "00", "client": "100",
            "username": "MON", "password": "pw"}
    base.update(rfc)
    return {"rfc": base}


def test_fake_sdk_timeout_parameters_are_gone():
    p = rc.build_rfc_params(_cfg())
    assert "NWRFC_CONNECT_TIMEOUT" not in p and "NWRFC_COMM_TIMEOUT" not in p


def test_call_timeout_from_config_env_and_default(monkeypatch):
    monkeypatch.delenv("IBO_RFC_CALL_TIMEOUT", raising=False)
    monkeypatch.delenv("IBO_RFC_COMM_TIMEOUT", raising=False)
    assert rc.rfc_call_timeout(_cfg()) == 120
    monkeypatch.setenv("IBO_RFC_COMM_TIMEOUT", "45")
    assert rc.rfc_call_timeout(_cfg()) == 120          # old variable ignored (review fix 8c)
    monkeypatch.setenv("IBO_RFC_CALL_TIMEOUT", "30")
    assert rc.rfc_call_timeout(_cfg()) == 30
    assert rc.rfc_call_timeout(_cfg(call_timeout=10)) == 10
    assert rc.rfc_call_timeout(_cfg(call_timeout=0)) is None


def test_connection_gets_pyrfc_timeout_config(monkeypatch):
    seen = {}

    class Conn:
        def __init__(self, config=None, **params):
            seen["config"], seen["params"] = config, params

        def close(self):
            pass

    monkeypatch.setattr(rc, "Connection", Conn)
    monkeypatch.setattr(rc, "PYRFC_AVAILABLE", True)
    monkeypatch.setattr(rc, "_tcp_reachable", lambda *a, **k: (True, ""))
    with rc.SapSession("T1", _cfg(call_timeout=25)) as s:
        assert s.ok
    assert seen["config"] == {"timeout": 25}
    assert "NWRFC_COMM_TIMEOUT" not in seen["params"]


class _Conn:
    def __init__(self, exc):
        self.exc, self.calls, self.closed = exc, [], False

    def call(self, name, **kw):
        self.calls.append(name)
        raise self.exc

    def close(self):
        self.closed = True


def _session(exc):
    s = rc.SapSession("T2", _cfg())
    conn = _Conn(exc)
    s.ok, s.conn = True, conn
    return s, conn


@pytest.mark.parametrize("exc", [
    CommunicationError("RFC_COMMUNICATION_FAILURE: connection to partner broken"),
    ExternalRuntimeError("Connection was canceled: 2066443. New handle: 0"),
    RuntimeError("Key: RFC_TIMEOUT"),
])
def test_dead_connection_is_closed_and_not_called_again(exc):
    s, conn = _session(exc)
    assert s.call("TH_WPINFO") is None
    assert s.ok is False and s.connection_lost is True and conn.closed
    assert s.conn is None
    for name in ("TH_SERVER_LIST", "ENQUE_READ2", "RFC_READ_TABLE"):
        assert s.call(name) is None
        assert "no RFC session" in s.last_error
    assert conn.calls == ["TH_WPINFO"]
    assert s.read_table("SNAP", ["DATUM"]) is None


def test_abap_time_out_dump_does_not_kill_the_session():
    s, conn = _session(ABAPRuntimeError("TIME_OUT: time limit exceeded"))
    s.call("SWNC_GET_STATRECS_FRAME")
    assert s.ok is True


def test_logon_error_mid_session_blocks_future_logons():
    from collectors import rfc_logon_guard as guard

    class LogonError(Exception):
        key = "RFC_LOGON_FAILURE"

    s, _ = _session(LogonError("Name or password is incorrect"))
    s.call("TH_WPINFO")
    assert s.ok is False and s.logon_blocked
    assert guard.logon_block_status("T2")["blocked"]


def test_collection_stops_after_connection_loss():
    """The rest of a standard-module collection makes no further RFC calls."""
    s, conn = _session(CommunicationError("partner not reached"))
    metrics = rc._from_standard_modules(s)
    assert len(conn.calls) == 1
    # nothing read is shown as unknown, never as a healthy zero
    st22 = [m for m in metrics if m.name == "sap.st22.dumps"]
    assert st22 and st22[0].value is None
