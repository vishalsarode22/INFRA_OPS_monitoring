"""Review fix 8c: the pyrfc call timeout ignores the old IBO_RFC_COMM_TIMEOUT."""
from collectors import rfc_collector as rc


def _cfg(**rfc):
    return {"rfc": dict({"ashost": "h", "sysnr": "00", "client": "100",
                         "username": "u", "password": "p"}, **rfc)}


def test_old_comm_timeout_of_30_does_not_cancel_stat_reads(monkeypatch):
    monkeypatch.delenv("IBO_RFC_CALL_TIMEOUT", raising=False)
    monkeypatch.setenv("IBO_RFC_COMM_TIMEOUT", "30")
    assert rc.rfc_call_timeout(_cfg()) == 120


def test_call_timeout_sources(monkeypatch):
    monkeypatch.setenv("IBO_RFC_COMM_TIMEOUT", "30")
    monkeypatch.setenv("IBO_RFC_CALL_TIMEOUT", "90")
    assert rc.rfc_call_timeout(_cfg()) == 90
    assert rc.rfc_call_timeout(_cfg(call_timeout=200)) == 200


def test_comm_timeout_logged_once(monkeypatch):
    seen = []
    monkeypatch.setattr(rc, "_comm_timeout_warned", False)
    monkeypatch.setattr(rc.log, "info", lambda msg, *a, **k: seen.append(msg))
    monkeypatch.setenv("IBO_RFC_COMM_TIMEOUT", "30")
    rc.rfc_call_timeout(_cfg())
    rc.rfc_call_timeout(_cfg())
    assert len([m for m in seen if "IBO_RFC_COMM_TIMEOUT" in m]) == 1
