"""
Say WHY a system cannot be read, in words an operator can act on.

Before this, every failure looked the same on the wall:
    host 198.51.100.31:3300 not reachable (TimeoutError) -- network/Basis check
which does not tell a VPN that is down from a SAP system that is down.

STATES (the tag starts the message, so logs and mails are greppable)
    [VPN NOT CONNECTED]    the VPN / network link to the system's network is
                           down -- the system itself may be fine
    [SYSTEM DOWN]          the network is up (proved by another host in the
                           same network, or the VPN client says connected)
                           and the system does not answer
    [NOT REACHABLE]        cannot be read, and there is no evidence to tell
                           which of the two it is (configure network: below)
    [CREDENTIALS]          the system answers; the monitoring user's logon is
                           refused -- not DOWN
    [COLLECTOR UNREACHABLE] / [TUNNEL DOWN] / [COLLECTOR NOT ANSWERING]
                           the Ubuntu collector chain (dashboard/remote_sources)

HOW "THE NETWORK IS UP" IS PROVED -- nothing is guessed
Per system in config/systems.yaml (all optional):

    network:
      name: Infrabeat VPN          # systems with the same name share a network
      vpn: true                    # say "VPN not connected" when it is down
      probe: 198.51.100.1:443      # extra always-on host(s) in that network
      check: forticlient:Pune_TTML # ask the FortiClient CLI (Linux collector)

Evidence, in order:
  1. check: forticlient:<profile> -- `forticlient-cli vpn status`. Not
     connected -> VPN NOT CONNECTED.
  2. Another host in the same network answers (a probe:, or any other
     configured system with the same network name) -> the network is up,
     so this system is DOWN. If its dispatcher port 32NN answers but the RFC
     gateway 33NN does not, the message says the SAP gateway is down.
  3. Nothing in that network answers -> VPN NOT CONNECTED when vpn: true,
     otherwise "network link down".
  4. No network: block at all -> systems in the same /24 subnet are used
     as the evidence and the message says that it is the subnet that is
     unreachable, without calling it a VPN.
  5. No evidence of any kind -> NOT REACHABLE, and the message says which
     setting would make it precise.

ALERTS (decided by Infrabeat, Oct 2026; never sent to the client)
    VPN NOT CONNECTED   -> Basis, once per network, repeat every 30 min
    SYSTEM DOWN         -> manager + Basis, repeat every 5 min while down
    NOT REACHABLE       -> Basis, every 30 min
    CREDENTIALS         -> Basis, every 60 min
    collector / tunnel  -> Basis, every 30 min
    back to normal      -> "[RECOVERED]" to whoever got the alert
Repeats go through core/alert_ledger.py. recheck_known_failures(), run every
minute by the jobs service (and the web process in single-process mode),
re-probes anything currently failing so the 5-minute DOWN reminder and the
recovery mail happen even when nobody presses Read live.
IBO_REACH_ALERTS=0 turns the mails off; the wall messages stay.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__)

VPN_DOWN = "VPN NOT CONNECTED"
SYSTEM_DOWN = "SYSTEM DOWN"
VPN_OFF = "VPN OFF"          # switched off on purpose: shown, never alerted
NOT_REACHABLE = "NOT REACHABLE"
CREDENTIALS = "CREDENTIALS"
COLLECTOR_UNREACHABLE = "COLLECTOR UNREACHABLE"
TUNNEL_DOWN = "TUNNEL DOWN"
COLLECTOR_HUNG = "COLLECTOR NOT ANSWERING"
OK = "OK"

_PROBE_TIMEOUT = float(os.getenv("IBO_REACH_PROBE_TIMEOUT", "2") or 2)
_FORTI_CLI = os.getenv("IBO_FORTICLIENT_CLI", "/opt/forticlient/forticlient-cli")
_VPN_STATUS_DIR = os.getenv("IBO_VPN_STATUS_DIR", "/run/ibops-vpn")
_cache_lock = threading.Lock()
_net_cache: dict[str, tuple[float, bool, str]] = {}   # network key -> (at, up, witness)
_NET_CACHE_SECONDS = 30.0


def tag(state: str, text: str) -> str:
    return f"[{state}] {text}"


def state_of(message: str | None) -> str | None:
    """The state tag at the start of a message, or None."""
    m = (message or "").strip()
    if m.startswith("[") and "]" in m:
        return m[1:m.index("]")]
    return None


# ----------------------------------------------------------------- probes

def tcp_open(host: str, port: int, timeout: float | None = None) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout or _PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def tcp_open_far_end(host: str, port: int, wait: float = 2.0) -> bool:
    """
    For a port that is the local end of an SSH tunnel: ssh accepts the local
    connection first and only then tries the real target, closing ours a
    moment later if that fails. So: connect, then see whether the connection
    is dropped within `wait` seconds. Still open (or data) -> far end reached.
    """
    try:
        s = socket.create_connection((host, int(port)), timeout=_PROBE_TIMEOUT)
    except OSError:
        return False
    try:
        s.settimeout(wait)
        try:
            return s.recv(1) != b""
        except socket.timeout:
            return True
        except OSError:
            return False
    finally:
        s.close()


def _split_hostport(text: str, default_port: int = 3300) -> tuple[str, int] | None:
    text = str(text or "").strip()
    if not text:
        return None
    if ":" in text:
        host, _, port = text.rpartition(":")
        try:
            return host.strip(), int(port)
        except ValueError:
            return None
    return text, default_port


def router_first_hop(route) -> tuple[str, int] | None:
    """'/H/127.0.0.20/S/3299/H/198.51.100.213/S/3300' -> ('127.0.0.20', 3299); None without a route."""
    import re as _re
    m = _re.match(r"^/?H/([^/]+)(?:/S/([^/]+))?", str(route or "").strip())
    if not m:
        return None
    port = m.group(2) or "3299"
    return m.group(1), int(port) if port.isdigit() else 3299


def _sap_ports(cfg: dict) -> tuple[str | None, int, int]:
    """(host, gateway port 33NN, dispatcher port 32NN) of a system's RFC config."""
    rfc = cfg.get("rfc") or {}
    host = str(rfc.get("ashost") or "").strip() or None
    sysnr = str(rfc.get("sysnr") or "00").strip()
    nn = int(sysnr) if sysnr.isdigit() else 0
    gw = 3300 + nn
    # RFC through a tunnel gateway (rfc.gwhost / gwserv, ashost = the real
    # server): that local end is what this machine can reach and must test.
    hop = router_first_hop(rfc.get("saprouter"))
    if hop:
        # Through a SAProuter (e.g. on the AG VM, reached through a tunnel):
        # the router's local end is what this machine can reach and must test.
        return hop[0], hop[1], 3200 + nn
    gwhost = str(rfc.get("gwhost") or "").strip()
    if gwhost:
        host = gwhost
        gs = str(rfc.get("gwserv") or "").strip()
        if gs.isdigit():
            gw = int(gs)
    return host, gw, 3200 + nn


def _network(cfg: dict) -> dict:
    net = cfg.get("network")
    if isinstance(net, str):
        return {"name": net}
    return dict(net) if isinstance(net, dict) else {}


def _keeper_vpn(system: str) -> str:
    """Name of the vpn_keeper.yaml VPN that lists this system, else ''."""
    try:
        from core import vpn_keeper
        for v in vpn_keeper.load_config():
            if system in v.get("systems", []):
                return v["name"]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _keeper_note(name: str) -> str:
    """' Auto-reconnect: ...' -- what the VPN keeper is doing about it."""
    if not name:
        return ""
    try:
        from core import vpn_keeper
        st = (vpn_keeper.status() or {}).get(name) or {}
        if not vpn_keeper.find_exe():
            return (" Auto-reconnect is OFF: FortiSSLVPNclient.exe was not found (put it in the tools folder of "
                    "IBOPS and restart the dashboard).")
        state = st.get("state") or "starting"
        return f" Auto-reconnect: {state}{(' -- ' + str(st.get('reason'))) if st.get('reason') else ''}."
    except Exception:  # noqa: BLE001
        return ""


def _subnet(host: str | None) -> str | None:
    try:
        return str(ipaddress.ip_network(f"{host}/24", strict=False))
    except (ValueError, TypeError):
        return None


def forticlient_status(profile: str | None = None) -> tuple[bool | None, str]:
    """
    (connected?, detail) from `forticlient-cli vpn status`. (None, why) when
    the CLI is not available on this machine (e.g. the Windows VM).
    """
    cli = _FORTI_CLI if os.path.exists(_FORTI_CLI) else shutil.which("forticlient-cli")
    if not cli:
        return None, "FortiClient CLI not found on this machine"
    try:
        out = subprocess.run([cli, "vpn", "status"], capture_output=True, text=True, timeout=8).stdout
    except Exception as exc:  # noqa: BLE001
        return None, f"FortiClient CLI failed: {type(exc).__name__}"
    status, name = "", ""
    for line in (out or "").splitlines():
        s = line.strip()
        if s.lower().startswith("status:"):
            status = s.split(":", 1)[1].strip()
        elif s.lower().startswith("vpn name:"):
            name = s.split(":", 1)[1].strip()
    connected = status.lower() == "connected" and (not profile or not name or name == profile)
    detail = f"status '{status or 'unknown'}'" + (f", profile '{name}'" if name else "")
    return connected, detail


def watchdog_status(name: str) -> dict | None:
    """The ibops-vpn watchdog's status file (packaging/vpn), or None.

    Corrected by systemd and the Settings page (core/vpn_control.effective):
    "off" only when switched off on the Settings page; a service that is not
    running for any other reason is "service_down" (alerted)."""
    import json
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", str(name or "")) or ".." in str(name):
        return None                         # a watchdog name, never a path
    try:
        with open(os.path.join(_VPN_STATUS_DIR, f"{name}.json"), encoding="utf-8") as fh:
            st = json.load(fh)
    except Exception:
        return None
    try:
        from core import vpn_control
        st = vpn_control.effective(name, st)
    except Exception:
        pass
    return st


def watchdog_message(name: str, st: dict) -> str | None:
    """VPN NOT CONNECTED text for a watchdog that is not up; None when up/unknown."""
    state = st.get("state")
    reason = st.get("reason") or "the tunnel is down"
    nxt = st.get("next_attempt")
    when = f" Next try at {nxt[11:16]}." if nxt else ""
    if state == "up" or state is None:
        return None
    if state == "off":
        # Not an alert: switched off on purpose (Settings page) -- see report().
        return tag(VPN_OFF, f"VPN {name} is off: {reason}. Press Connect on the Settings page to switch it on.")
    if state == "connecting" and st.get("by_settings"):
        # Connect was just pressed: not an outage yet. If it does not come up
        # within 2 minutes the watchdog / service state is alerted as usual.
        return tag(VPN_OFF, f"VPN {name} is starting ({reason}).")
    if state == "service_down":
        return tag(VPN_DOWN, f"VPN not connected ({name}): {reason}. Start it with Connect on the Settings "
                             f"page, or on the collector: sudo systemctl start ibops-vpn@{name}")
    if state == "stopped":
        return tag(VPN_DOWN, f"VPN not connected ({name}): automatic reconnect stopped after 3 "
                             f"failed attempts -- {reason}. Fix it, then run on the collector: "
                             f"sudo ibops-vpn reset {name}")
    if state == "in_use":
        return tag(VPN_DOWN, f"VPN not connected ({name}): {reason}. Not pushing the other "
                             f"session out.{when}")
    if state == "retrying":
        return tag(VPN_DOWN, f"VPN not connected ({name}): {reason}; reconnect attempt "
                             f"{st.get('attempt') or ''}.{when}")
    return tag(VPN_DOWN, f"VPN not connected ({name}): connecting now.")


# ------------------------------------------------- systems through a tunnel
#
#   network:
#     name: Pune_TTML
#     vpn: true
#     check: remote:ubuntu/pune_ttml      # <remote source>/<VPN watchdog name>
#
# For a system reached through an SSH tunnel to another machine (Sarloha:
# Windows -> tunnel -> Ubuntu -> VPN -> client). Three things can be down,
# and each gets its own message:
#   the tunnel on THIS machine     -> [TUNNEL DOWN]  (nothing listens locally)
#   the VPN on the other machine   -> [VPN NOT CONNECTED] (asked over the tunnel)
#   the SAP system itself          -> [SYSTEM DOWN] (tunnel and VPN both up)

def _remote_check(cfg: dict) -> tuple[str, str] | None:
    check = str(_network(cfg).get("check") or "").strip()
    if not check.lower().startswith("remote:"):
        return None
    spec = check.split(":", 1)[1]
    source, _, vpn = spec.partition("/")
    return source.strip(), vpn.strip()


def _is_loopback(host: str | None) -> bool:
    return str(host or "").startswith("127.") or host in ("localhost", "::1")


def remote_vpn(cfg: dict, fresh: bool = False) -> dict | None:
    rc = _remote_check(cfg)
    if not rc:
        return None
    try:
        from dashboard.remote_sources import remote_vpn_status
        return remote_vpn_status(rc[0], rc[1], max_age=0 if fresh else 30.0)
    except Exception:  # noqa: BLE001
        return None


def tunnel_target(local_host: str, local_port: int) -> tuple[str, int] | None:
    """
    The real target behind a local tunnel address, from
    config/tunnel_forwards.txt (the same file the tunnel is built from).
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "tunnel_forwards.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split("#", 1)[0].strip().split(":")
                if len(parts) == 4 and parts[0] == local_host and parts[1] == str(local_port):
                    return parts[2], int(parts[3])
    except (OSError, ValueError):
        pass
    return None


def remote_reach(cfg: dict, host: str, port: int) -> bool | None:
    rc = _remote_check(cfg)
    if not rc:
        return None
    try:
        from dashboard.remote_sources import remote_reach as _rr
        return _rr(rc[0], host, port)
    except Exception:  # noqa: BLE001
        return None


def precheck(system: str, cfg: dict) -> str | None:
    """
    Before connecting: if the remote VPN is known to be down, say so at once.
    Needed because through a tunnel the local port ANSWERS even when the VPN
    behind it is down -- the TCP probe alone cannot see that.
    """
    rc = _remote_check(cfg)
    if not rc:
        return None
    st = remote_vpn(cfg)
    if not st or st.get("state") in (None, "up", "unknown-collector"):
        return None
    msg = watchdog_message(rc[1], st)
    return msg.replace("VPN not connected (", f"VPN not connected on {rc[0]} (", 1) if msg else None


def diagnose_connect_error(system: str, cfg: dict, error_text: str) -> str | None:
    """
    The local tunnel port answered, but the RFC logon could not reach SAP
    ("partner not reached", connection closed). With the VPN up that is the
    system; with the VPN down, the VPN. None when not a tunnelled system or
    when the error is something else (credentials etc.).
    """
    rc = _remote_check(cfg)
    host = _sap_ports(cfg)[0]                    # the tunnel gateway when rfc.gwhost is set
    if not rc or not _is_loopback(host):
        return None
    low = (error_text or "").lower()
    if not any(t in low for t in ("partner", "connection", "closed", "reset", "not reached",
                                  "communication")):
        return None
    # Ask fresh -- a cached "VPN up" from before a VPN restart blamed the system.
    st = remote_vpn(cfg, fresh=True) or {}
    if st.get("state") not in (None, "up", "unknown-collector"):
        msg = watchdog_message(rc[1], st)
        return msg.replace("VPN not connected (", f"VPN not connected on {rc[0]} (", 1) if msg else None
    if st.get("state") == "up":
        port = int((cfg.get("rfc") or {}).get("sysnr") or 0) + 3300 if str(
            (cfg.get("rfc") or {}).get("sysnr") or "0").isdigit() else 3300
        target = tunnel_target(str(host), port)
        if target:
            reach = remote_reach(cfg, target[0], target[1])
            if reach is True:
                # The far machine reaches the system, so the system is up and
                # the tunnel refused to pass the connection on.
                return tag(TUNNEL_DOWN, f"The tunnel does not pass {system} through: {rc[0]} reaches "
                                        f"{target[0]}:{target[1]} itself, but refused to forward to it "
                                        f"(\"administratively prohibited\"). The system is up. Fix: on "
                                        f"{rc[0]} run packaging/ubuntu/allow_tunnel_targets.sh with this "
                                        f"target, then restart the 'IBOPS Tunnel' task.")
            if reach is False:
                return tag(SYSTEM_DOWN, f"{system} is DOWN: the VPN on {rc[0]} is up, but {rc[0]} cannot "
                                        f"reach {target[0]}:{target[1]} either.")
        return tag(NOT_REACHABLE, f"{system} could not be reached through the tunnel. The VPN on "
                                  f"{rc[0]} is up, but whether the system itself answers could not "
                                  f"be checked ({str(error_text).strip()[:160]}).")
    return tag(NOT_REACHABLE, f"{system} cannot be reached through the tunnel and {rc[0]} could not "
                              f"be asked about its VPN ({str(error_text).strip()[:160]}).")


# -------------------------------------------------------------- diagnosis

def _all_systems() -> list[dict]:
    try:
        from core.config_loader import get_all_systems
        return get_all_systems()
    except Exception:
        try:
            from core.config_loader import get_systems
            return get_systems()
        except Exception:
            return []


def _network_evidence(system: str, cfg: dict, host: str) -> tuple[bool | None, str, list[str]]:
    """
    Is the network this system lives in up? (True/False/None, witness, checked)
    True  -> some other host in it answered (witness names it)
    False -> hosts were tried and none answered
    None  -> nothing to try
    """
    net = _network(cfg)
    name = str(net.get("name") or "").strip()
    probes: list[tuple[str, int, str]] = []
    for p in (net.get("probe") if isinstance(net.get("probe"), list) else [net.get("probe")]):
        hp = _split_hostport(p, 443) if p else None
        if hp:
            probes.append((hp[0], hp[1], f"probe {hp[0]}:{hp[1]}"))

    subnet = _subnet(host)
    for other in _all_systems():
        oname = other.get("name")
        if not oname or oname == system:
            continue
        ohost, ogw, _ = _sap_ports(other)
        if not ohost or ohost == host:
            continue
        same = (name and str(_network(other).get("name") or "").strip() == name) or \
               (not name and subnet and _subnet(ohost) == subnet)
        if same:
            probes.append((ohost, ogw, f"{oname} ({ohost})"))

    if not probes:
        return None, "", []

    key = name or subnet or host
    with _cache_lock:
        hit = _net_cache.get(key)
    if hit and time.monotonic() - hit[0] < _NET_CACHE_SECONDS:
        return hit[1], hit[2], [p[2] for p in probes[:4]]

    checked = []
    for h, port, label in probes[:4]:          # at most 4 x 2 s
        checked.append(label)
        if tcp_open(h, port):
            with _cache_lock:
                _net_cache[key] = (time.monotonic(), True, label)
            return True, label, checked
    with _cache_lock:
        _net_cache[key] = (time.monotonic(), False, "")
    return False, "", checked


def diagnose_unreachable(system: str, cfg: dict, host: str, port: int,
                         exc_name: str = "TimeoutError") -> str:
    """The tagged message for 'host:port did not answer'."""
    net = _network(cfg)
    netname = str(net.get("name") or "").strip()
    vpn_flag = bool(net.get("vpn"))
    keeper = _keeper_vpn(system)
    if keeper and not vpn_flag:
        # Listed in config/vpn_keeper.yaml: it lives behind a VPN, so an outage
        # of its whole network is a VPN outage (one mail per VPN, not per system).
        vpn_flag, netname = True, netname or keeper
    check = str(net.get("check") or "").strip()
    _, gw_port, disp_port = _sap_ports(cfg)
    where = f"{system} ({host}:{port}, {exc_name})"

    # 0. Tunnelled system: a refused LOCAL port means the tunnel here is down.
    rc = _remote_check(cfg)
    if rc and _is_loopback(host):
        pre = precheck(system, cfg)
        if pre:
            return pre
        return tag(TUNNEL_DOWN, f"The SSH tunnel carrying {system} is down on this machine: nothing "
                                f"listens on {host}:{port}. Restart it (scheduled task 'IBOPS Tunnel', "
                                f"or the ssh -L command). The system itself may be fine.")

    # 1. Ask the VPN client, when one is configured.
    vpn_known_up = False
    if check.lower().startswith("openfortivpn"):
        wname = check.split(":", 1)[1].strip() if ":" in check else netname
        st = watchdog_status(wname)
        if st is not None:
            msg = watchdog_message(wname, st)
            if msg:
                return msg
            vpn_known_up = st.get("state") == "up"
    elif check.lower().startswith("forticlient"):
        profile = check.split(":", 1)[1].strip() if ":" in check else None
        connected, detail = forticlient_status(profile)
        if connected is False:
            return tag(VPN_DOWN, f"VPN not connected: FortiClient {('profile ' + profile + ' ') if profile else ''}"
                                 f"is down ({detail}). {system} cannot be checked until the VPN is "
                                 f"connected; the system itself may be fine.")
        vpn_known_up = connected is True

    # 2-3. Does anything else in the same network answer?
    up, witness, checked = _network_evidence(system, cfg, host)
    if up:
        if port != disp_port and tcp_open(host, disp_port):
            return tag(SYSTEM_DOWN, f"{system} is DOWN: the server {host} answers (dispatcher {disp_port}) "
                                    f"but the SAP RFC gateway {port} does not. Network/VPN is fine "
                                    f"({witness} answers).")
        return tag(SYSTEM_DOWN, f"{system} is DOWN: the network/VPN is up ({witness} answers) but "
                                f"{where} does not answer.")
    if up is False:
        if vpn_known_up:
            return tag(VPN_DOWN, f"VPN reports connected but passes no traffic: none of "
                                 f"{', '.join(checked)} answer, nor {where}. Reconnect the VPN.")
        if vpn_flag:
            return tag(VPN_DOWN, f"VPN not connected{(' (' + netname + ')') if netname else ''}: "
                                 f"{where} does not answer and neither does "
                                 f"{', '.join(checked)} in the same network. The systems themselves "
                                 f"may be fine.{_keeper_note(keeper)}")
        label = netname or (f"subnet {_subnet(host)}" if _subnet(host) else "its network")
        return tag(NOT_REACHABLE, f"Network link to {label} is down: {where} does not answer and "
                                  f"neither does {', '.join(checked)}. Not a SAP problem.")

    # 4-5. Nothing to compare with.
    if vpn_known_up:
        return tag(SYSTEM_DOWN, f"{system} is DOWN: the VPN is connected but {where} does not answer.")
    if net and not vpn_flag:
        # On the company network (no VPN): say so, no VPN talk (CENTOR_QAS, 28.10.2026).
        return tag(NOT_REACHABLE, f"{where} does not answer. It is on the company network (no VPN), "
                                  f"so the SAP server is probably down or stopped, or the network path "
                                  f"to it is blocked. Check that the server is up.")
    return tag(NOT_REACHABLE, f"{where} does not answer: the SAP server may be down, or the network "
                              f"path to it is blocked. To let IBOPS tell these apart, give {system} the "
                              f"same Network name as another system at this customer (Systems page).")


def credentials_message(system: str, error_text: str) -> str:
    return tag(CREDENTIALS, f"{system} is UP, but the monitoring user's logon is refused: "
                            f"{str(error_text).strip()[:200]}. Fix the user/password; this is not "
                            f"a system outage.")


# ------------------------------------------------------------------ alerts

_state_lock = threading.Lock()
_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "dashboard", "reach_state.json")

# Owner's rules (Oct 2026): SYSTEM DOWN (unreachable) -> manager + Basis,
# every 5 minutes while down, never the client, only after
# down_after() consecutive failed polls; VPN failed -> Basis only;
# reachable but logon refused -> CREDENTIALS to Basis (not DOWN).
# NOT REACHABLE (cannot tell VPN from system) is treated as down.
_RULES = {
    VPN_DOWN:              ("vpn_down", "basis", 1800, "VPN NOT CONNECTED"),
    SYSTEM_DOWN:           ("system_down", "manager", 300, "SYSTEM DOWN"),
    NOT_REACHABLE:         ("not_reachable", "manager", 300, "NOT REACHABLE"),
    CREDENTIALS:           ("credentials", "basis", 3600, "CREDENTIALS"),
    COLLECTOR_UNREACHABLE: ("collector_down", "basis", 1800, "COLLECTOR UNREACHABLE"),
    TUNNEL_DOWN:           ("collector_down", "basis", 1800, "TUNNEL DOWN"),
    COLLECTOR_HUNG:        ("collector_down", "basis", 1800, "COLLECTOR NOT ANSWERING"),
}


def _repeat_seconds(system, state: str, default_s: int) -> int:
    """Reminder interval for a reachability mail: the system's alert_timing
    (down_repeat_min / access_repeat_min), else IBO_DOWN_REPEAT_MIN /
    IBO_ACCESS_REPEAT_MIN when set, else the built-in rule."""
    from core import alert_timing
    key = "down_repeat_min" if state in (SYSTEM_DOWN, NOT_REACHABLE) else "access_repeat_min"
    own = alert_timing.system_value(system, key)
    if own is None:
        env = (os.getenv(alert_timing.KEYS[key][1]) or "").strip()
        own = alert_timing.env_value(key) if env else None
    return int(own) * 60 if own else int(default_s)


def _who_gets(cfg: dict, state: str, who: str) -> tuple[list, list]:
    """(to, cc) for a reachability mail: the system's own list for this kind of
    event (down_emails / access_emails) when set, else the normal rule."""
    from core import recipients
    own = recipients.special(cfg, "down" if state in (SYSTEM_DOWN, NOT_REACHABLE) else "access")
    if own:
        return own, []
    if who == "manager":
        return recipients.basis_and_manager(cfg)
    return recipients.basis_internal(cfg), []


def alerts_enabled() -> bool:
    return str(os.getenv("IBO_REACH_ALERTS", "1")).strip().lower() not in ("0", "false", "no", "off")


_DOWN_STATES = (SYSTEM_DOWN, NOT_REACHABLE)


def down_after(system=None) -> int:
    """
    The system's own setting (Systems page, alert_timing.down_after) first.
    Consecutive failed polls before a DOWN / NOT REACHABLE mail:
    IBO_DOWN_CONSECUTIVE_FAILURES, else consecutive_failures of the
    SAP_SYSTEM_UNREACHABLE rule in config/correlation_rules.yaml, else 2.
    """
    if system:
        from core import alert_timing
        own = alert_timing.system_value(system, "down_after")
        if own is not None:
            return max(1, own)
    raw = (os.getenv("IBO_DOWN_CONSECUTIVE_FAILURES") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    try:
        from core.correlation import load_correlation_config
        rule = load_correlation_config().get("SAP_SYSTEM_UNREACHABLE") or {}
        return max(1, int((rule.get("thresholds") or {}).get("consecutive_failures") or 2))
    except Exception:  # noqa: BLE001
        return 2


_CONN_WORDS = ("partner not reached", "timed out", "timeout", "wsaetimedout", "connection refused",
               "communication_failure", "communication failure", "not reachable", "connection closed",
               "connection reset", "no route to host", "unreachable", "niping", "nihostbyname",
               "cpic", "network")
_CRED_WORDS = ("name or password", "password is incorrect", "password logon", "user is locked",
               "rfc_logon_failure", "logon failed", "not authorized")


def _tag_untagged(system: str, message: str) -> str | None:
    """
    An RFC error without a state tag (a direct, non-tunnelled connection that
    failed) used to be no alert at all: the wall showed UNKNOWN and nobody
    was told. Classify it: logon refused -> CREDENTIALS; connection failure
    -> NOT REACHABLE. Anything else (missing settings, busy) stays untagged.
    """
    low = (message or "").lower()
    if any(w in low for w in _CRED_WORDS):
        return credentials_message(system, message)
    if any(w in low for w in _CONN_WORDS):
        return tag(NOT_REACHABLE, f"{system} did not answer over RFC: {str(message).strip()[:200]}")
    return None


def _load_state() -> dict:
    try:
        from utils.atomic import read_json
        data = read_json(_STATE_PATH, {})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    try:
        from utils.atomic import atomic_write_json
        atomic_write_json(_STATE_PATH, data)
    except Exception as exc:
        log.debug(f"reach state not saved: {exc}")


def current_failures() -> dict:
    with _state_lock:
        return {k: v for k, v in _load_state().items() if v.get("state") not in (None, OK)}


def forget(system: str) -> bool:
    """Drop the recorded state of `system` without a [RECOVERED] mail.

    Used when a standby system is handed back to its peer (core/standby.py):
    this dashboard stops checking it, so a recorded SYSTEM DOWN would
    otherwise stay "open" here for ever and feed escalation, correlation,
    the digest and Ask with a stale problem."""
    with _state_lock:
        data = _load_state()
        if system not in data:
            return False
        data.pop(system, None)
        _save_state(data)
    return True


def _cfg_for(system: str) -> dict | None:
    return next((s for s in _all_systems() if s.get("name") == system), None)


def _send(subject: str, html: str, to: list[str], cc: list[str], cfg: dict | None,
          system: str, kind: str, key, interval: int, severity: str) -> None:
    def _go():
        try:
            from core.config_loader import get_smtp_config
            from notifications.mailer import send_html
            if not to:
                log.warning(f"{system}: '{subject}' not mailed -- no recipients. Set "
                            f"IBO_BASIS_EMAILS (and ALERT_MANAGER_EMAILS) in .env.")
                return
            send_html(subject=subject, html=html, to=to, cc=cc,
                      smtp_config=get_smtp_config(cfg, require_recipients=False),
                      system=system, kind=kind, key=key, min_interval_s=interval, severity=severity)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"{system}: reachability mail failed: {exc}")
    threading.Thread(target=_go, daemon=True, name=f"reach-mail-{system}").start()


def report(system: str, message: str, cfg: dict | None = None) -> None:
    """Record the state for `system` and send the alert it calls for. Never raises."""
    try:
        state = state_of(message)
        if state == VPN_OFF:
            forget(system)          # off on purpose: no open problem, no mails, no escalation
            return
        if state is None:
            tagged = _tag_untagged(system, message)
            if tagged:
                message, state = tagged, state_of(tagged)
        if state not in _RULES:
            return
        cfg = cfg if cfg is not None else (_cfg_for(system) or {})
        try:
            from core import standby
            if not standby.active(cfg):
                return          # the peer monitors it (core/standby.py)
        except Exception:
            pass
        with _state_lock:
            data = _load_state()
            prev = data.get(system) or {}
            # DOWN and NOT REACHABLE count together: the diagnosis of one
            # outage can alternate between them from poll to poll.
            same = prev.get("state") == state or (prev.get("state") in _DOWN_STATES
                                                  and state in _DOWN_STATES)
            fails = int(prev.get("fails") or 0) + 1 if same else 1
            data[system] = {"state": state, "message": message,
                            "since": prev.get("since") if same
                            else datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "last": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "fails": fails, "alerted": bool(prev.get("alerted")) if same else False}
            _save_state(data)
        if prev.get("state") != state:
            _on_state_change(system, state, message)
        if not alerts_enabled():
            return
        if state in _DOWN_STATES and fails < down_after(system):
            # One failed poll is a blip; the owner's rule is DOWN after
            # down_after() consecutive failures (recheck_known_failures
            # re-probes every minute, so the second one comes quickly).
            log.info(f"{system}: [{state}] poll {fails}/{down_after(system)} failed -- not alerted yet")
            return
        from core import recipients
        kind, who, interval, label = _RULES[state]
        interval = _repeat_seconds(cfg or system, state, interval)
        to, cc = _who_gets(cfg, state, who)
        if not data[system].get("alerted"):
            with _state_lock:
                d2 = _load_state()
                if system in d2:
                    d2[system]["alerted"] = True
                    _save_state(d2)
        # One VPN mail per network, not one per system behind it. A change
        # of VPN situation (retrying -> stopped, or account in use) is a new
        # key, so that mail is not held back by the 30-minute repeat rule.
        key = (str(_network(cfg).get("name") or _subnet(_sap_ports(cfg)[0]) or system)
               if state == VPN_DOWN else system)
        if state == VPN_DOWN:
            if "reconnect stopped" in message:
                key += ":stopped"
            elif "in use by another session" in message or "Not pushing" in message:
                key += ":in_use"
        since = data[system]["since"]
        body = plain_down_mail(system, state, message, since, interval,
                               net=key.split(":", 1)[0] if state == VPN_DOWN else "")
        # The ledger de-duplicates on (system, kind, key). For a VPN outage the
        # "system" is the network, so PS4 and CARFOUR behind the same VPN
        # produce ONE mail, not one each.
        ledger_system = f"network:{key}" if state == VPN_DOWN else system
        net_label = key.split(":", 1)[0]
        extra = (" -- account in use elsewhere" if key.endswith(":in_use") else
                 " -- auto-reconnect stopped" if key.endswith(":stopped") else "")
        subject_name = (f"{net_label}{extra} (affects {system}{' and others' if _siblings(cfg, system) else ''})"
                        if state == VPN_DOWN else system)
        _send(f"[{label}] InfraBeatOps -- {subject_name}", body, to, cc, cfg, ledger_system,
              kind, key, interval, "CRITICAL" if state in _DOWN_STATES else "WARNING")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"{system}: reachability report failed: {exc}")


_PARK_STATE_NAMES = {CREDENTIALS: "credentials", SYSTEM_DOWN: "system_down", VPN_DOWN: "vpn_down",
                     NOT_REACHABLE: "not_reachable"}
_STATE_SEVERITY = {SYSTEM_DOWN: "critical", VPN_DOWN: "warning", CREDENTIALS: "warning",
                   NOT_REACHABLE: "warning", COLLECTOR_UNREACHABLE: "warning", TUNNEL_DOWN: "warning",
                   COLLECTOR_HUNG: "warning"}


def _on_state_change(system: str, state: str, message: str) -> None:
    """Timeline line, and park scheduled sweeps where IBO_AUTO_PARK says so. Never raises."""
    try:
        from core import timeline
        timeline.add(system, "vpn" if state == VPN_DOWN else "state", f"[{state}]", message,
                     severity=_STATE_SEVERITY.get(state, "warning"))
    except Exception:
        pass
    try:
        from core import alert_control
        if _PARK_STATE_NAMES.get(state) in alert_control.auto_park_states():
            alert_control.park(system, reason=f"automatic: {state}", by="system", auto=True)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"{system}: auto-park failed: {exc}")


def _on_recovered(system: str, prev: dict) -> None:
    try:
        from core import timeline
        timeline.add(system, "state", "Reachable again",
                     f"was {prev.get('state')} since {prev.get('since')}", severity="ok")
    except Exception:
        pass
    try:
        from core import alert_control
        kinds = {r[0] for r in _RULES.values()}
        alert_control.clear_until_fixed(system, kinds)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"{system}: clearing acknowledgements failed: {exc}")
    try:
        from core import tickets
        tickets.note(system, {r[0] for r in _RULES.values()},
                     f"InfraBeatOps: {system} reachable again at {datetime.now():%Y-%m-%d %H:%M} "
                     f"(was {prev.get('state')} since {prev.get('since')}).")
    except Exception:
        pass


def _siblings(cfg: dict | None, system: str) -> bool:
    name = str(_network(cfg or {}).get("name") or "").strip()
    if not name:
        return False
    return any(s.get("name") != system and str(_network(s).get("name") or "").strip() == name
               for s in _all_systems())


def auto_unpark_after_logon(system: str) -> None:
    """
    A park the tool set by itself because a logon was REFUSED ("automatic:
    credentials") ends by itself once an RFC logon works again. CEAT S4P stayed
    parked for 10 days after its password was fixed, and every 5-minute alert
    check said "not checked: parked". A park a person set is never touched.
    IBO_AUTO_UNPARK=0 switches this off.
    """
    if os.getenv("IBO_AUTO_UNPARK", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        from core import alert_control
        p = alert_control.parked(system)
        if not p or not int(p.get("auto") or 0):
            return
        if "credential" not in str(p.get("reason") or "").lower():
            return
        if alert_control.unpark(system, by="system"):
            log.info(f"{system}: RFC logon works again -- automatic credentials park cleared.")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"{system}: auto-unpark skipped: {exc}")


def report_ok(system: str, cfg: dict | None = None) -> None:
    """The system answered. Send [RECOVERED] if an alert state was recorded. Never raises."""
    try:
        with _state_lock:
            data = _load_state()
            prev = data.get(system) or {}
            if prev.get("state") in (None, OK):
                return
            data[system] = {"state": OK, "since": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            _save_state(data)
        _on_recovered(system, prev)
        if not alerts_enabled():
            return
        if prev.get("state") in _DOWN_STATES and "alerted" in prev and not prev.get("alerted"):
            # One failed poll that never became a DOWN mail: no RECOVERED mail either.
            return
        cfg = cfg if cfg is not None else (_cfg_for(system) or {})
        from core import recipients
        _, who, _, label = _RULES.get(prev["state"], ("", "basis", 0, prev["state"]))
        to, cc = _who_gets(cfg, prev["state"], who)
        is_parked = False
        try:
            from core.alert_control import parked
            is_parked = bool(parked(system))
        except Exception:
            pass
        _send(f"[RECOVERED] InfraBeatOps -- {system}", plain_recovered_mail(system, prev, is_parked),
              to, cc, cfg, system, "recovered", system, 60, "INFO")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"{system}: recovery report failed: {exc}")


# ------------------------------------------------------------ plain mails

_PLAIN = {
    VPN_DOWN: ("{s} can't be reached -- the VPN is down",
               "IBOPS cannot reach <b>{s}</b>, and it cannot reach another server in the same network either. "
               "So the <b>VPN connection</b> from the IBOPS machine is down -- <b>the SAP system itself is "
               "probably fine</b>.",
               "Reconnect the VPN (FortiClient) on the IBOPS machine {host}. If the VPN keeper is set up, it "
               "tries this by itself first.",
               "not reachable because the VPN was down"),
    SYSTEM_DOWN: ("{s} is DOWN",
                  "IBOPS reaches the server of <b>{s}</b>, but SAP is <b>not answering</b> on its ports. SAP may "
                  "be stopped or may have crashed.",
                  "Check on the host whether SAP is running (or ask whoever runs the server), and look at the "
                  "work directory / dev_disp logs for why it stopped.",
                  "down"),
    NOT_REACHABLE: ("{s} is not reachable",
                    "IBOPS cannot connect to <b>{s}</b>. The network, the server or SAP itself may be down -- "
                    "from here IBOPS cannot tell which.",
                    "Try logging on to {s} with SAP GUI. If that fails too, check the server and the network "
                    "with the hosting team.",
                    "not reachable"),
    CREDENTIALS: ("IBOPS can't log on to {s}",
                  "SAP <b>refused IBOPS's logon</b> to <b>{s}</b> (user or password). The system may be fine, "
                  "but IBOPS cannot monitor it until the logon works.",
                  "In SU01, check the monitoring user (locked? password expired?). If the password changed, "
                  "update it for this system on the Systems page.",
                  "refusing IBOPS's logon"),
    COLLECTOR_UNREACHABLE: ("IBOPS's collector for {s} is not answering",
                            "IBOPS reaches <b>{s}</b> through a helper machine (collector), and that helper is "
                            "not answering. <b>SAP itself is probably fine.</b>",
                            "Check that the collector machine is on and its IBOPS agent is running.",
                            "not reachable through the collector"),
    TUNNEL_DOWN: ("The tunnel to {s} is down",
                  "IBOPS reaches <b>{s}</b> through a tunnel, and the tunnel is down. <b>SAP itself is probably "
                  "fine.</b>",
                  "Restart the tunnel on the IBOPS machine (or check the machine it connects to).",
                  "not reachable because the tunnel was down"),
    COLLECTOR_HUNG: ("IBOPS's collector for {s} is stuck",
                     "The helper machine (collector) that reads <b>{s}</b> for IBOPS is connected but not "
                     "answering requests. <b>SAP itself is probably fine.</b>",
                     "Restart the IBOPS agent on the collector machine.",
                     "not reachable through the collector"),
}


def _ago(since) -> str:
    try:
        from notifications.mail_style import duration
        t = datetime.strptime(str(since)[:19], "%Y-%m-%d %H:%M:%S")
        return duration((datetime.now() - t).total_seconds())
    except Exception:  # noqa: BLE001
        return ""


def plain_down_mail(system: str, state: str, message: str, since, interval_s: int, net: str = "") -> str:
    from notifications import mail_style as ms
    title, lead, todo, _past = _PLAIN.get(state, ("{s}: " + state, "{s}: " + _html(message), "", state))
    host = socket.gethostname()
    s_ = ms.esc(system)
    lead = lead.format(s=s_, host=ms.esc(host))
    if state == VPN_DOWN and net:
        lead += f" Network: <b>{ms.esc(net)}</b>."
    msg = str(message or "")
    if state == NOT_REACHABLE and "Network link to" in msg:
        title = "{s} can't be reached -- its network link is down"
        lead = (f"IBOPS cannot reach <b>{s_}</b>, and no other server in the same network answers either. So the "
                f"<b>network link (VPN or line)</b> to that network is down -- <b>SAP itself is probably fine</b>.")
        todo = ("Check the VPN / network connection from the IBOPS machine {host}. To get one mail per VPN instead "
                "of one per system, list these systems in config/vpn_keeper.yaml.")
    if "Auto-reconnect is OFF" in msg:
        todo = ("Reconnect the VPN (FortiClient) on {host} by hand. <b>Automatic reconnect is off</b>: "
                "FortiSSLVPNclient.exe is not in the IBOPS tools folder yet.")
    elif "Auto-reconnect: " in msg and "reconnect stopped" not in msg:
        todo = ("The VPN keeper is reconnecting by itself (see the Health page). If this mail keeps coming, "
                "reconnect FortiClient on {host} by hand.")
    if "reconnect stopped" in msg:
        todo = ("The VPN keeper tried to reconnect several times and has stopped, so a wrong password is not "
                "retried until the account locks. Fix the cause, then press <b>Reset</b> on the Health page.")
    elif "in use by another session" in msg or "Not pushing" in msg:
        todo = ("The VPN user is signed in somewhere else, so the VPN keeper is not pushing that session off. "
                "Ask who is using that VPN account, or use a dedicated account for IBOPS.")
    rows = [("Since", f"{ms.ist(since)}" + (f" ({_ago(since)} ago)" if _ago(since) else "")),
            ("Checked from", host)]
    blocks = []
    if todo:
        blocks.append(ms.section("What to do", f"<div style='font:14px/1.5 {ms.FONT}'>{todo.format(s=s_, host=ms.esc(host))}</div>"))
    blocks.append(ms.section("Details", ms.facts(rows)))
    blocks.append(ms.section("Technical detail", f"<div style='font:12px/1.5 Consolas,monospace;color:#475569;"
                                                 f"background:#f8fafc;padding:8px 10px;border-radius:6px'>"
                                                 f"{ms.esc(msg)}</div>"))
    foot = ""
    if state in _DOWN_STATES:
        foot = (f"IBOPS checks again every minute and repeats this mail every {max(1, interval_s // 60)} minutes "
                f"while it stays down. Snooze or 'Ignore until fixed' (buttons below, or the wall) stops it. ")
    foot += "Times are India time (IST)."
    level = "CRITICAL" if state in _DOWN_STATES else "WARNING"
    eyebrow = f"SYSTEM CHECK &nbsp;{ms.badge(level)} &nbsp;{s_}"
    return ms.page(level, eyebrow, title.format(s=system), lead, blocks, foot)


def plain_recovered_mail(system: str, prev: dict, parked: bool) -> str:
    from notifications import mail_style as ms
    state = prev.get("state")
    past = (_PLAIN.get(state) or ("", "", "", str(state or "").lower()))[3]
    since = prev.get("since")
    took = _ago(since)
    lead = (f"<b>{ms.esc(system)}</b> answers again. It was {ms.esc(past)} from {ms.esc(ms.ist(since))} until now"
            + (f" (<b>{ms.esc(took)}</b>)" if took else "") + ". <b>Nothing to do.</b>")
    blocks = []
    if parked:
        blocks.append(ms.callout("<b>Scheduled monitoring for this system is still paused (parked).</b> Clear the "
                                 "flag on the wall, or with the button below, to start it again.", "WARNING"))
    if prev.get("message"):
        blocks.append(ms.section("What it was", f"<div style='font:12px/1.5 Consolas,monospace;color:#475569;"
                                                f"background:#f8fafc;padding:8px 10px;border-radius:6px'>"
                                                f"{ms.esc(prev.get('message'))}</div>"))
    return ms.page("OK", f"SYSTEM CHECK &nbsp;{ms.badge('OK')} &nbsp;{ms.esc(system)}",
                   f"{system} is reachable again", lead, blocks, "Times are India time (IST).")


def _html(text: str) -> str:
    import html
    return html.escape(str(text or ""))


def check_vpn_watchdogs() -> int:
    """
    Turn the ibops-vpn watchdog's state into wall state and mail even when
    nobody presses Read live: a VPN that drops at night is reported within
    a minute. One report per network; the ledger keeps it to one mail.
    """
    seen = set()
    n = 0
    for cfg in _all_systems():
        try:
            from core import standby
            if not standby.active(cfg):
                continue
        except Exception:
            pass
        # A VPN on another machine (remote: check) is reported the same way.
        if _remote_check(cfg):
            msg = precheck(cfg.get("name"), cfg)
            if msg:
                report(cfg.get("name"), msg, cfg)
                n += 1
            continue
        check = str(_network(cfg).get("check") or "")
        if not check.lower().startswith("openfortivpn"):
            continue
        wname = check.split(":", 1)[1].strip() if ":" in check else str(_network(cfg).get("name"))
        st = watchdog_status(wname)
        if st is None:
            continue
        msg = watchdog_message(wname, st)
        system = cfg.get("name")
        if msg:
            report(system, msg, cfg)
            n += 1
        elif st.get("state") == "up" and wname not in seen:
            seen.add(wname)
    return n


def recheck_known_failures() -> int:
    """
    Re-probe every system currently recorded as failing (not credentials --
    those need a person) and report again or report recovery. Cheap: a TCP
    connect per system. Returns how many were checked.
    """
    try:
        check_vpn_watchdogs()
    except Exception as exc:  # noqa: BLE001
        log.debug(f"vpn watchdog check failed: {exc}")
    n = 0
    for system, rec in current_failures().items():
        if rec.get("state") in (CREDENTIALS, COLLECTOR_UNREACHABLE, TUNNEL_DOWN, COLLECTOR_HUNG):
            continue
        cfg = _cfg_for(system)
        if not cfg:
            continue
        try:
            from core import standby
            if not standby.active(cfg):
                continue
        except Exception:
            pass
        host, gw, _ = _sap_ports(cfg)
        if not host:
            continue
        n += 1
        # Through a tunnel the local port answers even while the far-end VPN
        # is down, so "port open" alone must not count as recovered.
        pre = precheck(system, cfg)
        tunnelled = bool(_remote_check(cfg)) and _is_loopback(host)
        if pre:
            report(system, pre, cfg)
        elif (tcp_open_far_end(host, gw) if tunnelled else tcp_open(host, gw)):
            report_ok(system, cfg)
        elif tunnelled and tcp_open(host, gw):
            # Local tunnel port answers but the far end does not: same
            # diagnosis as a broken RFC connection (refused forward vs down).
            report(system, diagnose_connect_error(system, cfg, "connection broken")
                   or diagnose_unreachable(system, cfg, host, gw), cfg)
        else:
            report(system, diagnose_unreachable(system, cfg, host, gw), cfg)
    return n
