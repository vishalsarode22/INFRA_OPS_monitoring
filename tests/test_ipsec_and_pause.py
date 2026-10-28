"""
Ceat onboarding: the IPsec (strongSwan) side of the VPN watchdog, the
"paused" VPN state, and pause / resume of systems (scripts/pause_system.py).
"""
import importlib.util
import os
import stat
import sys
import textwrap

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_spec = importlib.util.spec_from_file_location(
    "ibops_vpn_watchdog_ipsec", os.path.join(_ROOT, "packaging", "vpn", "ibops_vpn_watchdog.py"))
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)

sys.path.insert(0, os.path.join(_ROOT, "scripts"))
import pause_system as P  # noqa: E402


# ------------------------------------------------------------- classify

def test_ipsec_xauth_failure_is_auth():
    out = "XAuth authentication of 'Infrabeat-basis2' (myself) failed\nestablishing connection 'ibops-ceat' failed"
    assert W.classify_ipsec(out) == "auth"


def test_ipsec_wrong_psk_is_auth():
    assert W.classify_ipsec("received INVALID_HASH_INFORMATION error notify") == "auth"


def test_ipsec_no_proposal_is_proposal_not_auth():
    out = ("authentication of '10.1.0.37' (myself) with pre-shared key successful\n"
           "received NO_PROPOSAL_CHOSEN error notify")
    assert W.classify_ipsec(out) == "proposal"


def test_ipsec_timeout_is_network():
    out = "sending retransmit 3 of request message ID 0\ngiving up after 3 retransmits\ntimed out waiting"
    assert W.classify_ipsec(out) == "network"


def test_ipsec_charon_words_in_use_do_not_mean_account_in_use():
    # charon says "in use" about its own reqids; only the gateway's login-limit words count
    assert W.classify_ipsec("reqid 1 in use, establishing connection failed") == "network"
    assert W.classify_ipsec("XAuth: login limit reached for user") == "in_use"


def test_proposal_failure_follows_the_three_try_rule():
    p = W.Policy()
    assert p.on_failure("proposal")[0] == W.RETRYING
    assert p.on_failure("proposal")[0] == W.RETRYING
    assert p.on_failure("proposal")[0] == W.RETRYING
    state, delay, reason = p.on_failure("proposal")
    assert state == W.STOPPED and "IPsec settings" in reason


# ------------------------------------------------------------- backend

def test_backend_and_settings_from_etc_ibops_vpn(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "IPSEC_CONF_DIR", str(tmp_path / "ipsec"))
    monkeypatch.setattr(W, "CONF_DIR", str(tmp_path / "forti"))
    os.makedirs(tmp_path / "ipsec")
    os.makedirs(tmp_path / "forti")
    (tmp_path / "forti" / "pune_ttml.conf").write_text("host = x\n# ibops-probe = 192.0.2.127:3300\n")
    (tmp_path / "ipsec" / "ceat.conf").write_text("conn = ibops-ceat\ngateway = 203.0.113.189\n"
                                                  "# ibops-probe = 10.250.10.135:3310\n")
    assert W.backend("ceat") == "ipsec"
    assert W.backend("pune_ttml") == "openfortivpn"
    assert W.ipsec_settings("ceat")["conn"] == "ibops-ceat"
    assert W.probe_targets("ceat") == [("10.250.10.135", 3310)]
    assert W.probe_targets("pune_ttml") == [("192.0.2.127", 3300)]


def _fake_ipsec(tmp_path, status_out: str, up_out: str = "", up_rc: int = 0) -> str:
    """A stand-in `ipsec` command that logs its arguments."""
    log = tmp_path / "calls.log"
    sfile = tmp_path / "status.txt"
    sfile.write_text(status_out)
    script = tmp_path / "ipsec"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> {log}
        case "$1" in
          status) cat {sfile};;
          up) printf '%s\\n' "{up_out}"; exit {up_rc};;
          down) echo "closing";;
        esac
        exit 0
        """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.mark.skipif(os.name != "posix", reason="shell script stand-in")
def test_ipsec_connect_up_and_link_watches_installed_state(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "etc")
    (tmp_path / "etc" / "ceat.conf").write_text("conn = ibops-ceat\n")
    monkeypatch.setattr(W, "IPSEC_CONF_DIR", str(tmp_path / "etc"))
    status = ("Security Associations (1 up, 0 connecting):\n"
              "  ibops-ceat[1]: ESTABLISHED 3 seconds ago, 10.1.0.37[10.1.0.37]...203.0.113.189[10.0.1.5]\n"
              "  ibops-ceat{1}:  INSTALLED, TUNNEL, reqid 1, ESP in UDP SPIs: c1_i c2_o\n")
    monkeypatch.setattr(W, "IPSEC", _fake_ipsec(tmp_path, status, "connection 'ibops-ceat' established successfully"))
    ok, link, out = W.connect("ceat")
    assert ok and isinstance(link, W.IpsecLink)
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert "down ibops-ceat" in calls and "up ibops-ceat" in calls
    assert calls.index("down ibops-ceat") < calls.index("up ibops-ceat")     # leftovers cleared first
    # tunnel goes away -> the link notices at the next check
    (tmp_path / "status.txt").write_text("Security Associations (0 up, 0 connecting):\n  none\n")
    link._checked = 0
    assert link.alive() is False


@pytest.mark.skipif(os.name != "posix", reason="shell script stand-in")
def test_ipsec_failed_up_is_brought_down_so_charon_never_retries_alone(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "etc")
    (tmp_path / "etc" / "ceat.conf").write_text("conn = ibops-ceat\n")
    monkeypatch.setattr(W, "IPSEC_CONF_DIR", str(tmp_path / "etc"))
    monkeypatch.setattr(W, "IPSEC", _fake_ipsec(tmp_path, "Security Associations (0 up, 1 connecting):\n",
                                               "received NO_PROPOSAL_CHOSEN error notify", 1))
    ok, link, out = W.connect("ceat")
    assert not ok and link is None
    assert W.classify_for("ceat", out) == "proposal"
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert calls[-1] == "down ibops-ceat"


def test_a_child_of_another_connection_is_not_ours():
    assert not __import__("re").search(r"^\s*" + "ibops-ceat" + r"\{\d+\}:\s+INSTALLED",
                                       "  ibops-ceat2{1}:  INSTALLED, TUNNEL", __import__("re").M)


# ------------------------------------------------------------- paused VPN

def test_disabled_and_stopped_vpn_is_paused_not_alerted(monkeypatch):
    from core import vpn_control as V
    from core import reachability as R
    monkeypatch.setattr(V, "service_active", lambda n: False)
    monkeypatch.setattr(V, "service_enabled", lambda n: False)
    monkeypatch.setattr(V, "switch_row", lambda n: None)
    st = V.effective("pune_ttml", {"state": "off", "reason": "the VPN service was stopped"})
    assert st["state"] == "off" and st.get("paused") and "paused" in st["reason"]
    msg = R.watchdog_message("pune_ttml", st)
    assert msg.startswith(f"[{R.VPN_OFF}]") or R.VPN_OFF in msg


def test_enabled_but_stopped_vpn_is_still_service_down(monkeypatch):
    from core import vpn_control as V
    monkeypatch.setattr(V, "service_active", lambda n: False)
    monkeypatch.setattr(V, "service_enabled", lambda n: True)
    monkeypatch.setattr(V, "switch_row", lambda n: None)
    assert V.effective("ceat", {"state": "up"})["state"] == "service_down"


# ------------------------------------------------------------- pause / resume

_YAML = """systems:
- name: PS4
  client: '500'
  rfc:
    ashost: 198.51.100.31
# Sarloha: through the tunnel.
- name: SARLOHA_QAS
  client: '500'
  rfc:
    ashost: 127.0.0.2
- name: SARLOHA PRD
  client: '500'
  rfc:
    ashost: 127.0.0.3
"""
_FWD = """# header
127.0.0.2:3300:192.0.2.127:3300     # SARLOHA_QAS  RFC
127.0.0.3:3300:192.0.2.123:3300     # SARLOHA PRD  RFC
"""


def _cfg(tmp_path, monkeypatch, crlf=False):
    conv = (lambda t: t.replace("\n", "\r\n")) if crlf else (lambda t: t)
    (tmp_path / "systems.yaml").write_bytes(conv(_YAML).encode())
    (tmp_path / "tunnel_forwards.txt").write_bytes(conv(_FWD).encode())
    monkeypatch.setattr(P, "LIVE", str(tmp_path / "systems.yaml"))
    monkeypatch.setattr(P, "PAUSED", str(tmp_path / "systems_paused.yaml"))
    monkeypatch.setattr(P, "FORWARDS", str(tmp_path / "tunnel_forwards.txt"))


def test_pause_and_resume_round_trip_is_byte_exact(tmp_path, monkeypatch):
    for crlf in (False, True):
        _cfg(tmp_path, monkeypatch, crlf)
        before = (tmp_path / "systems.yaml").read_bytes()
        fbefore = (tmp_path / "tunnel_forwards.txt").read_bytes()
        assert P.move(["SARLOHA_QAS", "SARLOHA PRD"], resume=False) == 0
        import yaml
        live = yaml.safe_load((tmp_path / "systems.yaml").read_text())
        assert [s["name"] for s in live["systems"]] == ["PS4"]
        fw = (tmp_path / "tunnel_forwards.txt").read_text()
        assert fw.count(P.MARK) == 2
        assert P.move(["SARLOHA_QAS", "SARLOHA PRD"], resume=True) == 0
        assert (tmp_path / "systems.yaml").read_bytes() == before
        assert (tmp_path / "tunnel_forwards.txt").read_bytes() == fbefore
        for f in os.listdir(tmp_path):
            if ".bak-pause-" in f or f == "systems_paused.yaml":
                os.remove(tmp_path / f)


def test_pause_refuses_to_empty_systems_yaml(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    assert P.move(["PS4", "SARLOHA_QAS", "SARLOHA PRD"], resume=False) == 2
    assert (tmp_path / "systems.yaml").read_text() == _YAML


def test_pause_unknown_name_changes_nothing(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    assert P.move(["NOPE"], resume=False) == 1
    assert (tmp_path / "systems.yaml").read_text() == _YAML
    assert not os.path.exists(tmp_path / "systems_paused.yaml")
