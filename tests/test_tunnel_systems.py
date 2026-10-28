"""
Systems reached Windows -> SSH tunnel -> Ubuntu -> VPN (check: remote:...).
Tunnel down, far-end VPN down and system down must each be told apart.
"""
import socket
import threading
import time

import pytest

from core import reachability as R

SAR = {"name": "SARLOHA PRD", "rfc": {"ashost": "127.0.0.3", "sysnr": "00"},
       "network": {"name": "Pune_TTML", "vpn": True, "check": "remote:ubuntu/pune_ttml"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_all_systems", lambda: [SAR])
    monkeypatch.setattr(R, "_STATE_PATH", str(tmp_path / "reach.json"))
    monkeypatch.setenv("IBO_STATE_BACKEND", "files")
    sent = []
    monkeypatch.setattr(R, "_send", lambda *a, **k: sent.append(a))
    return sent


def _remote(monkeypatch, status):
    monkeypatch.setattr(R, "remote_vpn", lambda cfg, fresh=False: status)


def test_local_tunnel_port_refused_is_tunnel_down(monkeypatch):
    _remote(monkeypatch, {"state": "up"})
    msg = R.diagnose_unreachable("SARLOHA PRD", SAR, "127.0.0.3", 3300)
    assert R.state_of(msg) == R.TUNNEL_DOWN and "IBOPS Tunnel" in msg


def test_far_end_vpn_down_is_said_before_connecting(monkeypatch):
    _remote(monkeypatch, {"state": "retrying", "reason": "the tunnel dropped", "attempt": "1 of 3"})
    msg = R.precheck("SARLOHA PRD", SAR)
    assert R.state_of(msg) == R.VPN_DOWN and "on ubuntu" in msg and "attempt 1 of 3" in msg


def test_vpn_up_and_far_machine_cannot_reach_it_either_is_system_down(monkeypatch):
    _remote(monkeypatch, {"state": "up"})
    monkeypatch.setattr(R, "tunnel_target", lambda h, p: ("192.0.2.123", 3300))
    monkeypatch.setattr(R, "remote_reach", lambda cfg, h, p: False)
    msg = R.diagnose_connect_error("SARLOHA PRD", SAR, "partner '127.0.0.3:3300' not reached")
    assert R.state_of(msg) == R.SYSTEM_DOWN and "cannot reach 192.0.2.123:3300 either" in msg


def test_refused_forward_is_tunnel_not_system(monkeypatch):
    """What happened on 7 Oct: Ubuntu reached Sarloha, the key did not allow it."""
    _remote(monkeypatch, {"state": "up"})
    monkeypatch.setattr(R, "tunnel_target", lambda h, p: ("192.0.2.123", 3300))
    monkeypatch.setattr(R, "remote_reach", lambda cfg, h, p: True)
    msg = R.diagnose_connect_error("SARLOHA PRD", SAR, "connection to partner '127.0.0.3:3300' broken")
    assert R.state_of(msg) == R.TUNNEL_DOWN and "The system is up" in msg and "allow_tunnel_targets" in msg


def test_cannot_check_far_end_says_so_not_down(monkeypatch):
    _remote(monkeypatch, {"state": "up"})
    monkeypatch.setattr(R, "tunnel_target", lambda h, p: ("192.0.2.123", 3300))
    monkeypatch.setattr(R, "remote_reach", lambda cfg, h, p: None)
    msg = R.diagnose_connect_error("SARLOHA PRD", SAR, "connection broken")
    assert R.state_of(msg) == R.NOT_REACHABLE


def test_tunnel_target_is_read_from_the_forwards_file(tmp_path, monkeypatch):
    import os
    cfgdir = tmp_path / "config"; cfgdir.mkdir()
    (cfgdir / "tunnel_forwards.txt").write_text("# x\n127.0.0.3:3300:192.0.2.123:3300  # PRD RFC\n")
    monkeypatch.setattr(R.os.path, "abspath", lambda p: str(tmp_path / "core" / "reachability.py"))
    assert R.tunnel_target("127.0.0.3", 3300) == ("192.0.2.123", 3300)
    assert R.tunnel_target("127.0.0.9", 3300) is None


def test_credentials_are_not_turned_into_down(monkeypatch):
    _remote(monkeypatch, {"state": "up"})
    assert R.diagnose_connect_error("SARLOHA PRD", SAR, "Name or password is incorrect") is None


def test_collector_not_answering_about_vpn_says_so(monkeypatch):
    _remote(monkeypatch, None)
    msg = R.diagnose_connect_error("SARLOHA PRD", SAR, "connection closed")
    assert R.state_of(msg) == R.NOT_REACHABLE and "could not be asked" in msg


def test_periodic_check_reports_far_end_vpn_and_never_fake_recovers(monkeypatch, _env):
    _remote(monkeypatch, {"state": "in_use", "reason": "the VPN account is in use by another session"})
    assert R.check_vpn_watchdogs() == 1
    assert "account in use elsewhere" in _env[-1][0]
    # Local tunnel port answers, but the VPN behind it is down: NOT recovered.
    monkeypatch.setattr(R, "tcp_open", lambda *a, **k: True)
    monkeypatch.setattr(R, "tcp_open_far_end", lambda *a, **k: True)
    R.recheck_known_failures()
    assert not any(a[0].startswith("[RECOVERED]") for a in _env)
    _remote(monkeypatch, {"state": "up"})
    R.recheck_known_failures()
    assert _env[-1][0].startswith("[RECOVERED]")


def test_far_end_probe_sees_a_tunnel_that_cannot_reach_its_target():
    def server(mode):
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0)); s.listen(5)

        def run():
            while True:
                c, _ = s.accept()
                if mode == "close":
                    time.sleep(0.1); c.close()      # what ssh does when the target is unreachable
                else:
                    threading.Timer(4, c.close).start()
        threading.Thread(target=run, daemon=True).start()
        return s.getsockname()[1]
    assert R.tcp_open_far_end("127.0.0.1", server("close"), wait=1.0) is False
    assert R.tcp_open_far_end("127.0.0.1", server("open"), wait=1.0) is True


def test_vpn_status_endpoint(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import json
    monkeypatch.setattr(R, "_VPN_STATUS_DIR", str(tmp_path))
    (tmp_path / "pune_ttml.json").write_text(json.dumps({"name": "pune_ttml", "state": "up"}))
    from dashboard.app import app
    c = TestClient(app)
    assert c.get("/api/vpn-status/pune_ttml").json()["state"] == "up"
    assert c.get("/api/vpn-status/nope").status_code == 404
