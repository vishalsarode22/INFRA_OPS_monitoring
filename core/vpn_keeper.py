"""
Keep the client VPNs on THIS Windows machine connected (FortiClient SSL-VPN).

The free FortiClient VPN has no "Always Up" (a paid / EMS feature), so when
the AG tunnel drops at 02:23 every CARFOUR / CAR / PS4 check fails until
someone reconnects by hand. This watches the SAP hosts behind each VPN and,
when they stop answering, reconnects with Fortinet's command-line client
FortiSSLVPNclient.exe (FortiClientTools package -- no admin rights needed to
run it; FortiClient itself must already be installed).

config/vpn_keeper.yaml:

    vpns:
      - name: AG
        gateway: 203.0.113.74:10443
        username: ${AG_VPN_USERNAME}
        password: ${AG_VPN_PASSWORD}          # put the values in .env
        systems: [CARFOUR QAS, CARFOUR PRD, CARFOUR PRDDUCTION, CAR PRD, PS4]
        # probe: 198.51.100.213:3300         # optional; default = the systems' RFC hosts

    IBO_FORTISSLVPN_EXE   path of FortiSSLVPNclient.exe (default: looked for next
                          to FortiClient and in <project>/tools)

RULES -- the same as the Linux watchdog (packaging/vpn), so the VPN account
is never locked and nobody is pushed off:
  * 2 failed probes in a row (1 min apart) = the tunnel is down.
  * Reconnect; if that fails retry after 2 min, 5 min, 15 min, then STOP and
    mail Basis once (a wrong password retried forever locks the account).
    "Reset" on the Health page (or POST /api/ops/vpn-keeper/<name>/reset)
    starts again.
  * Account in use elsewhere (the tunnel drops within 5 minutes of coming up,
    twice in a row -- someone signed in with the same VPN user): do not
    fight, wait 30 min, 1 h, 2 h, then every 2 h.
Status: runtime/vpn_keeper.json (Health page rows, /api/ops/vpn-keeper).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__)

FAIL_DELAYS = [120, 300, 900]
IN_USE_DELAYS = [1800, 3600, 7200]
QUICK_DROP_S = 300
PROBE_EVERY_S = 60
CONNECT_WAIT_S = 75
_IN_USE = re.compile(r"already (logged|connected)|in use|maximum (number|login)|too many|"
                     r"concurrent|login limit|session limit", re.I)
_AUTH = re.compile(r"authenticat|invalid (user|pass|credential)|login failed|denied|bad (user|pass)", re.I)

_state: dict = {}
_lock = threading.Lock()


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path() -> str:
    from core.config_loader import CONFIG_DIR
    return os.path.join(CONFIG_DIR, "vpn_keeper.yaml")


def status_path() -> str:
    d = os.getenv("IBO_RUNTIME_DIR") or os.path.join(_root(), "runtime")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "vpn_keeper.json")


def find_exe() -> str | None:
    explicit = (os.getenv("IBO_FORTISSLVPN_EXE") or "").strip().strip('"')
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        for sub in (r"Fortinet\FortiClient\FortiSSLVPNclient.exe", r"Fortinet\FortiClient\x86\FortiSSLVPNclient.exe"):
            p = os.path.join(base, sub)
            if os.path.exists(p):
                return p
    for p in (os.path.join(_root(), "tools", "FortiSSLVPNclient.exe"),
              os.path.join(_root(), "tools", "SSLVPNcmdline", "x86", "FortiSSLVPNclient.exe")):
        if os.path.exists(p):
            return p
    return shutil.which("FortiSSLVPNclient.exe")


def load_config() -> list[dict]:
    try:
        import yaml
        with open(config_path(), "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except OSError:
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning(f"vpn_keeper.yaml unreadable: {exc}")
        return []
    try:
        from core.config_loader import _resolve_env_value
    except Exception:  # noqa: BLE001
        def _resolve_env_value(v):
            return v
    out = []
    for v in raw.get("vpns") or []:
        if not isinstance(v, dict) or not v.get("name") or not v.get("gateway"):
            continue
        out.append({"name": str(v["name"]).strip(), "gateway": str(_resolve_env_value(str(v["gateway"]))).strip(),
                    "username": str(_resolve_env_value(str(v.get("username") or ""))).strip(),
                    "password": str(_resolve_env_value(str(v.get("password") or ""))),
                    "systems": [str(s) for s in (v.get("systems") or [])],
                    "probe": v.get("probe") or "", "enabled": v.get("enabled", True) is not False})
    return out


def probes(vpn: dict) -> list[tuple[str, int]]:
    raw = vpn.get("probe")
    items = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw or ""))
    out = []
    for it in items:
        if ":" in str(it):
            h, p = str(it).rsplit(":", 1)
            if p.isdigit():
                out.append((h.strip(), int(p)))
    if out:
        return out
    try:
        from core.config_loader import get_all_systems
        wanted = set(vpn.get("systems") or [])
        for s in get_all_systems():
            if s.get("name") not in wanted:
                continue
            rfc = s.get("rfc") or {}
            host = str(rfc.get("ashost") or "").strip()
            nr = str(rfc.get("sysnr") or s.get("sap_instance_nr") or "00").strip().zfill(2)[-2:]
            if host:
                out.append((host, 3300 + int(nr) if nr.isdigit() else 3300))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"vpn keeper probes: {exc}")
    return list(dict.fromkeys(out))


def reachable(targets: list[tuple[str, int]], timeout: float = 3.0) -> bool | None:
    """True when any target answers; None when there is nothing to probe."""
    if not targets:
        return None
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _save() -> None:
    try:
        with open(status_path(), "w", encoding="utf-8") as fh:
            json.dump(_state, fh, indent=1, default=str)
    except OSError as exc:
        log.debug(f"vpn keeper status not written: {exc}")


def status() -> dict:
    with _lock:
        if _state:
            return json.loads(json.dumps(_state, default=str))
    try:
        with open(status_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def reset(name: str) -> bool:
    with _lock:
        st = _state.get(name)
        if not st:
            return False
        st.update(state="retrying", attempts=0, in_use_n=0, next_try=0, reason="reset by a person")
        _save()
    log.info(f"VPN keeper {name}: reset -- reconnecting on the next check")
    return True


def _run(exe: str, args: list[str], timeout: float = 30) -> tuple[int | None, str]:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "still running (the client stays up while the tunnel is up)"
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def _connect(vpn: dict, exe: str) -> tuple[bool, str]:
    """Disconnect what is left, connect, and wait until a probe answers."""
    _run(exe, ["disconnect"], timeout=20)
    user = vpn["username"] + (f":{vpn['password']}" if vpn.get("password") else "")
    args = ["connect", "-h", vpn["gateway"], "-u", user, "-i", "-q", "-m"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # The client keeps running while the tunnel is up, so its output goes to a
    # file (a pipe nobody reads would fill up and freeze it hours later).
    out_path = os.path.join(os.path.dirname(status_path()), f"vpn_{re.sub(r'[^A-Za-z0-9_-]', '_', vpn['name'])}.log")
    try:
        out_f = open(out_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen([exe] + args, stdout=out_f, stderr=subprocess.STDOUT, creationflags=flags)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not start FortiSSLVPNclient: {type(exc).__name__}: {exc}"
    finally:
        try:
            out_f.close()
        except Exception:  # noqa: BLE001
            pass

    def _said() -> str:
        try:
            text = open(out_path, encoding="utf-8", errors="replace").read().strip()[-300:]
        except OSError:
            text = ""
        pw = str(vpn.get("password") or "")
        return text.replace(pw, "****") if pw else text

    deadline = time.monotonic() + CONNECT_WAIT_S
    targets = probes(vpn)
    while time.monotonic() < deadline:
        if reachable(targets, timeout=2.0):
            return True, "connected"
        if proc.poll() is not None:
            if reachable(targets, timeout=2.0):
                return True, "connected"
            return False, (_said() or f"FortiSSLVPNclient ended with code {proc.returncode}")
        time.sleep(3)
    return False, "the VPN did not come up within 75 s"


def _notify_stopped(vpn: dict, reason: str) -> None:
    try:
        from core.config_loader import get_all_systems, get_smtp_config
        from core import recipients
        from notifications import mailer
        cfg = next((s for s in get_all_systems() if s.get("name") in (vpn.get("systems") or [])), None) or {}
        to = recipients.basis_internal(cfg)
        if not to:
            return
        html = (f"<p><b>Automatic reconnect of the VPN {vpn['name']} has STOPPED</b> after 3 failed tries: "
                f"{reason}</p><p>Affected: {', '.join(vpn.get('systems') or [])}.</p>"
                f"<p>It stops so a wrong password is not retried until the account is locked. Fix the cause "
                f"(password in .env, gateway, the VPN user signed in elsewhere), then press <b>Reset</b> on the "
                f"Health page.</p>")
        mailer.send_html_now(f"[VPN] {vpn['name']}: automatic reconnect stopped", html, to,
                             get_smtp_config(cfg or None, require_recipients=False),
                             system=cfg.get("name", ""), kind="vpn_down", key=f"keeper:{vpn['name']}",
                             min_interval_s=6 * 3600, severity="CRITICAL")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"VPN keeper {vpn['name']}: could not mail Basis: {exc}")


def check_once(vpn: dict, exe: str | None, now: float | None = None) -> dict:
    """One check of one VPN (every PROBE_EVERY_S). Returns its state record."""
    now = time.time() if now is None else now
    name = vpn["name"]
    with _lock:
        st = _state.setdefault(name, {"name": name, "state": "unknown", "fails": 0, "attempts": 0,
                                      "in_use_n": 0, "next_try": 0, "up_since": None, "reason": "",
                                      "systems": vpn.get("systems") or []})
    up = reachable(probes(vpn))
    st["checked_at"] = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    if up is None:
        st.update(state="no-probe", reason="no SAP host to test: list the systems or a probe host:port")
        return st
    if up:
        if st["state"] != "up":
            st["up_since"] = now
            log.info(f"VPN keeper {name}: up")
        st.update(state="up", fails=0, attempts=0, reason="")
        return st
    st["fails"] = int(st.get("fails") or 0) + 1
    if st["state"] == "up":
        quick = st.get("up_since") is not None and now - float(st["up_since"]) < QUICK_DROP_S
        st["in_use_n"] = int(st.get("in_use_n") or 0) + 1 if quick else 0
        st["state"] = "down"
    if st["fails"] < 2 or st["state"] == "stopped":
        return st
    if not exe:
        st.update(state="no-client", reason="FortiSSLVPNclient.exe not found: automatic reconnect is off "
                                            "(see config/vpn_keeper.yaml)")
        return st
    if not vpn.get("username"):
        st.update(state="no-client", reason="no VPN username in config/vpn_keeper.yaml")
        return st
    if now < float(st.get("next_try") or 0):
        return st
    if int(st.get("in_use_n") or 0) >= 2:
        n = int(st.get("in_use_n")) - 2
        delay = IN_USE_DELAYS[min(n, len(IN_USE_DELAYS) - 1)]
        st.update(state="in_use", next_try=now + delay,
                  reason=f"the VPN user is signed in elsewhere (dropped twice within 5 min) -- not pushing "
                         f"them off; next try in {delay // 60} min")
        st["in_use_n"] = int(st["in_use_n"]) + 1
        log.warning(f"VPN keeper {name}: {st['reason']}")
        return st
    st["state"] = "connecting"
    log.warning(f"VPN keeper {name}: SAP hosts behind it stopped answering -- reconnecting")
    ok, why = _connect(vpn, exe)
    if ok:
        st.update(state="up", fails=0, attempts=0, up_since=now, reason="reconnected automatically",
                  last_reconnect=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        log.warning(f"VPN keeper {name}: reconnected")
        return st
    st["attempts"] = int(st.get("attempts") or 0) + 1
    if _IN_USE.search(why or ""):
        st["in_use_n"] = max(2, int(st.get("in_use_n") or 0))
    if st["attempts"] > len(FAIL_DELAYS) or (_AUTH.search(why or "") and st["attempts"] >= 2):
        st.update(state="stopped", reason=f"stopped after {st['attempts']} failed tries: {why}")
        log.error(f"VPN keeper {name}: {st['reason']}")
        _notify_stopped(vpn, why)
        return st
    delay = FAIL_DELAYS[st["attempts"] - 1]
    st.update(state="retrying", next_try=now + delay,
              reason=f"try {st['attempts']} failed ({why}); next in {delay // 60} min")
    log.warning(f"VPN keeper {name}: {st['reason']}")
    return st


def loop() -> None:
    """Runs in the dashboard (one per machine), only when vpn_keeper.yaml lists a VPN."""
    from core import instance_lock
    while not instance_lock.acquire("vpn-keeper"):
        time.sleep(120)
    exe = find_exe()
    vpns = [v for v in load_config() if v["enabled"]]
    log.info(f"VPN keeper: watching {', '.join(v['name'] for v in vpns) or 'nothing'}; "
             f"reconnect with {exe or 'NOTHING (FortiSSLVPNclient.exe not found -- watching only)'}")
    while True:
        vpns = [v for v in load_config() if v["enabled"]] or vpns
        for v in vpns:
            try:
                check_once(v, exe)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"VPN keeper {v['name']}: check failed: {type(exc).__name__}: {exc}")
        with _lock:
            _save()
        time.sleep(PROBE_EVERY_S)


def configured() -> bool:
    return bool(load_config())
