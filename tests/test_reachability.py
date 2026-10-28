"""
VPN not connected vs SYSTEM DOWN vs credentials, and the Ubuntu-collector
chain (VM / tunnel / dashboard). TCP probes, FortiClient and mail are faked.
"""
import urllib.error

import pytest

from core import reachability as R

PS4 = {"name": "PS4", "rfc": {"ashost": "198.51.100.31", "sysnr": "00"},
       "network": {"name": "Infrabeat VPN", "vpn": True}}
CAR = {"name": "CARFOUR QAS", "rfc": {"ashost": "198.51.100.213", "sysnr": "00"},
       "network": {"name": "Infrabeat VPN", "vpn": True}}
PRD = {"name": "PRD", "rfc": {"ashost": "10.1.0.193", "sysnr": "00"}}
SAR = {"name": "SARLOHA_QAS", "rfc": {"ashost": "192.0.2.127", "sysnr": "00"},
       "network": {"name": "Pune_TTML", "vpn": True, "check": "forticlient:Pune_TTML"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    R._net_cache.clear()
    monkeypatch.setattr(R, "_STATE_PATH", str(tmp_path / "reach.json"))
    monkeypatch.setenv("IBO_STATE_BACKEND", "files")
    sent = []
    monkeypatch.setattr(R, "_send", lambda subject, html, to, cc, cfg, system, kind, key, interval, sev:
                        sent.append({"subject": subject, "to": to, "cc": cc, "kind": kind,
                                     "key": key, "interval": interval, "system": system}))
    return sent


def _systems(monkeypatch, *cfgs):
    monkeypatch.setattr(R, "_all_systems", lambda: list(cfgs))


def _open(monkeypatch, *open_hostports):
    allowed = set(open_hostports)
    monkeypatch.setattr(R, "tcp_open", lambda h, p, timeout=None: (h, int(p)) in allowed)


def test_vpn_not_connected_when_nothing_in_the_network_answers(monkeypatch):
    _systems(monkeypatch, PS4, CAR, PRD)
    _open(monkeypatch, ("10.1.0.193", 3300))            # only the LAN system answers
    msg = R.diagnose_unreachable("PS4", PS4, "198.51.100.31", 3300)
    assert R.state_of(msg) == R.VPN_DOWN
    assert "VPN not connected (Infrabeat VPN)" in msg and "CARFOUR QAS" in msg


def test_system_down_when_a_sibling_in_the_same_network_answers(monkeypatch):
    _systems(monkeypatch, PS4, CAR)
    _open(monkeypatch, ("198.51.100.213", 3300))        # CARFOUR answers, PS4 does not
    msg = R.diagnose_unreachable("PS4", PS4, "198.51.100.31", 3300)
    assert R.state_of(msg) == R.SYSTEM_DOWN
    assert "PS4 is DOWN" in msg and "CARFOUR QAS" in msg


def test_sap_gateway_down_when_server_answers_on_dispatcher(monkeypatch):
    _systems(monkeypatch, PS4, CAR)
    _open(monkeypatch, ("198.51.100.213", 3300), ("198.51.100.31", 3200))
    msg = R.diagnose_unreachable("PS4", PS4, "198.51.100.31", 3300)
    assert R.state_of(msg) == R.SYSTEM_DOWN and "RFC gateway 3300 does not" in msg


def test_explicit_probe_host_is_used(monkeypatch):
    cfg = dict(PS4, network={"name": "Infrabeat VPN", "vpn": True, "probe": "198.51.100.1:443"})
    _systems(monkeypatch, cfg)
    _open(monkeypatch, ("198.51.100.1", 443))
    assert R.state_of(R.diagnose_unreachable("PS4", cfg, "198.51.100.31", 3300)) == R.SYSTEM_DOWN


def test_without_vpn_flag_it_is_a_network_link_not_a_vpn(monkeypatch):
    a = {"name": "A", "rfc": {"ashost": "10.9.9.1", "sysnr": "00"}}
    b = {"name": "B", "rfc": {"ashost": "10.9.9.2", "sysnr": "00"}}
    _systems(monkeypatch, a, b)
    _open(monkeypatch)
    msg = R.diagnose_unreachable("A", a, "10.9.9.1", 3300)
    assert R.state_of(msg) == R.NOT_REACHABLE and "subnet 10.9.9.0/24" in msg and "VPN" not in msg.split("(Add")[0]


def test_no_evidence_says_so_instead_of_guessing(monkeypatch):
    _systems(monkeypatch, PRD)
    _open(monkeypatch)
    msg = R.diagnose_unreachable("PRD", PRD, "10.1.0.193", 3300)
    assert R.state_of(msg) == R.NOT_REACHABLE and "may be down" in msg and "VPN" not in msg


def test_company_network_system_says_server_down_not_vpn(monkeypatch):
    """28.10.2026: CENTOR_QAS (company network, no VPN) got 'Cannot tell a VPN problem ...'."""
    cfg = dict(PRD, network={"name": "Centaur network", "vpn": False})
    _systems(monkeypatch, cfg)
    _open(monkeypatch)
    msg = R.diagnose_unreachable("PRD", cfg, "10.1.0.193", 3300)
    assert R.state_of(msg) == R.NOT_REACHABLE and "company network (no VPN)" in msg and "VPN problem" not in msg


def test_forticlient_disconnected_is_vpn_down(monkeypatch):
    _systems(monkeypatch, SAR)
    _open(monkeypatch)
    monkeypatch.setattr(R, "forticlient_status", lambda profile=None: (False, "status 'Not Running'"))
    msg = R.diagnose_unreachable("SARLOHA_QAS", SAR, "192.0.2.127", 3300)
    assert R.state_of(msg) == R.VPN_DOWN and "FortiClient profile Pune_TTML" in msg


def test_forticlient_connected_and_system_silent_is_down(monkeypatch):
    _systems(monkeypatch, SAR)
    _open(monkeypatch)
    monkeypatch.setattr(R, "forticlient_status", lambda profile=None: (True, "status 'Connected'"))
    msg = R.diagnose_unreachable("SARLOHA_QAS", SAR, "192.0.2.127", 3300)
    assert R.state_of(msg) == R.SYSTEM_DOWN and "VPN is connected" in msg


def test_forticlient_output_parsing(monkeypatch, tmp_path):
    cli = tmp_path / "forticlient-cli"
    cli.write_text("#!/bin/sh\necho 'Status: Connected'\necho '  VPN name: Pune_TTML'\n")
    cli.chmod(0o755)
    monkeypatch.setattr(R, "_FORTI_CLI", str(cli))
    import sys
    if sys.platform.startswith("win"):
        pytest.skip("shell script CLI stand-in is POSIX-only")
    assert R.forticlient_status("Pune_TTML")[0] is True
    assert R.forticlient_status("Other")[0] is False


def test_alert_routing_and_recovery(monkeypatch, _env):
    monkeypatch.setenv("IBO_BASIS_EMAILS", "basis@infrabeat.com")
    monkeypatch.setenv("ALERT_MANAGER_EMAILS", "manager@infrabeat.com")
    sent = _env
    # Owner's rule (Oct 2026): DOWN is mailed after 2 consecutive failed polls
    # (tests/test_fix_alerts_down.py), so the first report alone sends nothing.
    R.report("PS4", R.tag(R.SYSTEM_DOWN, "PS4 is DOWN"), PS4)
    assert sent == []
    R.report("PS4", R.tag(R.SYSTEM_DOWN, "PS4 is DOWN"), PS4)
    assert sent[-1]["to"] == ["manager@infrabeat.com"] and sent[-1]["cc"] == ["basis@infrabeat.com"]
    assert sent[-1]["interval"] == 300 and sent[-1]["kind"] == "system_down"

    R.report("CARFOUR QAS", R.tag(R.VPN_DOWN, "VPN not connected"), CAR)
    assert sent[-1]["to"] == ["basis@infrabeat.com"] and sent[-1]["cc"] == []
    assert sent[-1]["key"] == "Infrabeat VPN"            # one VPN mail per network

    R.report("PRD", R.credentials_message("PRD", "Name or password is incorrect"), PRD)
    assert sent[-1]["kind"] == "credentials" and sent[-1]["to"] == ["basis@infrabeat.com"]

    R.report_ok("PS4", PS4)
    assert sent[-1]["subject"].startswith("[RECOVERED]") and sent[-1]["to"] == ["manager@infrabeat.com"]
    n = len(sent)
    R.report_ok("PS4", PS4)                              # already OK: no second mail
    assert len(sent) == n


def test_one_vpn_mail_per_network_not_per_system(monkeypatch, _env):
    _systems(monkeypatch, PS4, CAR)
    R.report("PS4", R.tag(R.VPN_DOWN, "VPN not connected"), PS4)
    R.report("CARFOUR QAS", R.tag(R.VPN_DOWN, "VPN not connected"), CAR)
    # Same ledger identity for both -> the ledger sends the first, holds the second.
    assert _env[0]["system"] == _env[1]["system"] == "network:Infrabeat VPN"
    assert _env[0]["key"] == _env[1]["key"] == "Infrabeat VPN"
    assert "Infrabeat VPN" in _env[0]["subject"] and "and others" in _env[0]["subject"]


def test_untagged_errors_send_nothing(_env):
    R.report("PS4", "connected, but no metrics could be read", PS4)
    assert _env == []


def test_credentials_message_still_triggers_auth_freeze_matching():
    from collectors.rfc_collector import _is_auth_error
    msg = R.credentials_message("PS4", "Name or password is incorrect (repeat logon)")
    assert R.state_of(msg) == R.CREDENTIALS and _is_auth_error(msg)


# ---------------------------------------------------------------- collector

SRC = {"name": "ubuntu", "label": "Ubuntu collector", "url": "http://127.0.0.1:8001",
       "ssh_host": "10.1.0.37", "ssh_port": 22, "ssh_user": "ibops", "remote_port": 8000}


def test_collector_vm_down(monkeypatch):
    from dashboard import remote_sources as rs
    _open(monkeypatch)
    msg = rs._diagnose(SRC, urllib.error.URLError("refused"))
    assert R.state_of(msg) == R.COLLECTOR_UNREACHABLE and "10.1.0.37" in msg


def test_tunnel_down_with_vm_up_gives_the_exact_restart_command(monkeypatch):
    from dashboard import remote_sources as rs
    _open(monkeypatch, ("10.1.0.37", 22))
    msg = rs._diagnose(SRC, ConnectionRefusedError(10061, "refused"))
    assert R.state_of(msg) == R.TUNNEL_DOWN
    assert "ssh -N -L 8001:127.0.0.1:8000 ibops@10.1.0.37" in msg and "VM 10.1.0.37 is up" in msg


def test_tunnel_up_but_dashboard_silent(monkeypatch):
    from dashboard import remote_sources as rs
    _open(monkeypatch, ("10.1.0.37", 22), ("127.0.0.1", 8001))
    msg = rs._diagnose(SRC, TimeoutError("timed out"))
    assert R.state_of(msg) == R.COLLECTOR_HUNG and "Restart the dashboard" in msg


def test_token_refused_is_not_called_a_network_problem(monkeypatch):
    from dashboard import remote_sources as rs
    _open(monkeypatch, ("10.1.0.37", 22), ("127.0.0.1", 8001))
    err = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
    assert rs._diagnose(SRC, err).startswith("[COLLECTOR REFUSED]")


def test_smtp_config_for_alerts_does_not_need_the_report_list(monkeypatch):
    """VPN/DOWN alerts pick their own recipients; a missing ALERT_TO_EMAILS
    (as on the Ubuntu collector) must not stop them."""
    from core.config_loader import get_smtp_config
    for k, v in {"SMTP_HOST": "smtp.x", "SMTP_PORT": "587", "SMTP_USERNAME": "u",
                 "SMTP_PASSWORD": "p", "ALERT_FROM_EMAIL": "ibops@x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ALERT_TO_EMAILS", raising=False)
    with pytest.raises(ValueError):
        get_smtp_config(None)                               # report path: unchanged
    cfg = get_smtp_config(None, require_recipients=False)   # alert path
    assert cfg["host"] == "smtp.x" and cfg["to_emails"] == []
