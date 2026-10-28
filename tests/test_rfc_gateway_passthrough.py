"""Gateway host/port pass-through for the RFC collector."""
import socket

from collectors import rfc_collector


def _cfg(**extra):
    rfc = {"ashost": "192.0.2.127", "sysnr": "00",
           "username": "u", "password": "p"}
    rfc.update(extra)
    return {"name": "T", "client": "500", "rfc": rfc}


def test_gateway_params_are_passed_through():
    params = rfc_collector.build_rfc_params(
        _cfg(gwhost="127.0.0.1", gwserv="3300"))
    assert params["ashost"] == "192.0.2.127"
    assert params["gwhost"] == "127.0.0.1"
    assert params["gwserv"] == "3300"


def test_no_gateway_params_by_default():
    params = rfc_collector.build_rfc_params(_cfg())
    assert "gwhost" not in params and "gwserv" not in params


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_reachability_check_probes_the_gateway(monkeypatch):
    seen = {}

    def fake_connect(addr, timeout=None):
        seen["addr"] = addr
        return _Conn()

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    cfg = _cfg(gwhost="127.0.0.1", gwserv="3300")
    params = rfc_collector.build_rfc_params(cfg)
    ok, _ = rfc_collector._tcp_reachable(params["ashost"], params, cfg)
    assert ok and seen["addr"] == ("127.0.0.1", 3300)


def test_reachability_check_unchanged_without_gateway(monkeypatch):
    seen = {}

    def fake_connect(addr, timeout=None):
        seen["addr"] = addr
        return _Conn()

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    cfg = _cfg()
    params = rfc_collector.build_rfc_params(cfg)
    rfc_collector._tcp_reachable(params["ashost"], params, cfg)
    assert seen["addr"] == ("192.0.2.127", 3300)
