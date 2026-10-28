"""
Connect / disconnect a client VPN from the Settings page.

Only the VPNs run by the ibops-vpn watchdog (packaging/vpn: openfortivpn for
SSL-VPN, strongSwan for IPsec; service ibops-vpn@<name>) on THIS machine can
be switched here -- on the Ubuntu VM: ceat (IPsec), pune_ttml (Sarloha,
paused). A VPN whose service is stopped AND disabled is "paused": shown off,
never alerted. The Windows dashboard shows the
same VPN through the remote source and forwards the button to Ubuntu.

    Disconnect   sudo systemctl stop ibops-vpn@<name>
                 The systems behind it show "VPN OFF -- disconnected on purpose
                 by <who>" and are NOT alerted (no VPN / DOWN mails) until
                 someone presses Connect. After a reboot the service starts
                 again by itself.
    Connect      sudo ibops-vpn reset <name>   (only when it had given up)
                 sudo systemctl start ibops-vpn@<name>
                 REFUSED while the gateway says the account is in use by
                 another session: we never push someone else off; the
                 watchdog retries by itself (30 min, 1 h, 2 h, ...).

The dashboard user may run exactly these commands without a password after
the one-time setup:  sudo bash packaging/ubuntu/allow_vpn_control.sh
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from utils.logger import get_logger

log = get_logger(__name__)

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_cache: dict[str, tuple[float, bool | None]] = {}
_enabled: dict[str, bool | None] = {}


def _systemctl() -> str:
    for p in ("/usr/bin/systemctl", "/bin/systemctl"):
        if os.path.exists(p):
            return p
    return shutil.which("systemctl") or "systemctl"


def _cli() -> str:
    return "/usr/local/bin/ibops-vpn"


def supported() -> bool:
    return os.name == "posix" and bool(shutil.which("systemctl") or os.path.exists("/usr/bin/systemctl"))


def service_active(name: str) -> bool | None:
    """True / False from systemctl (no sudo needed); None when unknown. Cached 2 s."""
    if not supported() or not _NAME.match(name or ""):
        return None
    if os.getenv("IBO_VPN_SERVICE_CHECK", "1").strip().lower() in ("0", "false", "no", "off"):
        return None         # tests: never ask the machine's real systemd
    hit = _cache.get(name)
    if hit and time.monotonic() - hit[0] < 2:
        return hit[1]
    try:
        # LoadState first: on a machine without the watchdog installed every
        # unknown unit reads "inactive", which must not turn into "off".
        r = subprocess.run([_systemctl(), "show", f"ibops-vpn@{name}", "-p", "LoadState", "-p", "ActiveState",
                            "-p", "UnitFileState"], capture_output=True, text=True, timeout=5)
        props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        if props.get("LoadState") != "loaded":
            val = None
            _enabled[name] = None
        else:
            val = props.get("ActiveState") in ("active", "activating", "reloading")
            ufs = props.get("UnitFileState") or ""
            _enabled[name] = None if not ufs else ufs.startswith("enabled")
    except Exception:
        val = None
        _enabled[name] = None
    _cache[name] = (time.monotonic(), val)
    return val


def service_enabled(name: str) -> bool | None:
    """Starts at boot? From the same systemctl answer as service_active (call that first)."""
    return _enabled.get(name)


def paused(name: str) -> bool:
    """Stopped AND disabled on this machine (sudo systemctl disable --now ibops-vpn@<name>):
    a VPN put aside on purpose (e.g. a customer no longer monitored). Shown as off, never alerted."""
    return service_active(name) is False and service_enabled(name) is False


# ------------------------------------------------- "off on purpose" marker
# One row per VPN switched from the Settings page:
#   mode "off"         Disconnect pressed; prev_state / prev_next = what the
#                      watchdog was doing then (an "in_use" back-off must
#                      survive a Disconnect + Connect, or Connect would push
#                      the other session off; a "stopped" VPN needs a reset)
#   mode "connecting"  Connect pressed; cleared once the service runs, or
#                      after 2 minutes (then the service is reported down)

def _db():
    from core import ops_db
    ops_db.db().execute("CREATE TABLE IF NOT EXISTS vpn_switch (name TEXT PRIMARY KEY, mode TEXT NOT NULL, "
                        "by TEXT NOT NULL, at TEXT NOT NULL, prev_state TEXT, prev_next TEXT)")
    return ops_db


def switch_row(name: str) -> dict | None:
    try:
        return _db().one("SELECT * FROM vpn_switch WHERE name=?", (name,))
    except Exception:
        return None


def manual_off(name: str) -> dict | None:
    row = switch_row(name)
    return row if row and row["mode"] == "off" else None


def _clear(name: str) -> None:
    try:
        _db().execute("DELETE FROM vpn_switch WHERE name=?", (name,))
    except Exception:
        pass


def _age_s(row: dict) -> float:
    from core import ops_db
    import datetime as _dt
    t = ops_db.parse(row.get("at"))
    return (_dt.datetime.now() - t).total_seconds() if t else 1e9


def effective(name: str, st: dict) -> dict:
    """The watchdog status file `st` corrected by what systemd and the Settings page say."""
    active = service_active(name)
    row = switch_row(name)
    if active is None:
        return st                                   # no systemd answer: trust the file
    if active:
        if row and row["mode"] == "off" and (st.get("state") == "off" or _age_s(row) < 30):
            # Just disconnected; this process's 2-second systemd answer is
            # older than the stop. Trust the file + the row.
            return dict(st, state="off", next_attempt=None,
                        reason=f"disconnected on purpose by {row['by']} at {str(row['at'])[:16]}")
        if row and st.get("state") in ("off", "connecting"):
            return dict(st, state="connecting", reason=f"Connect pressed by {row['by']}", next_attempt=None,
                        by_settings=True)
        if row:
            _clear(name)                            # running again (Connect, reboot, by hand)
        if st.get("state") == "off":
            return dict(st, state="connecting", reason="starting", next_attempt=None)
        return st
    # service not running
    if not row and service_enabled(name) is False:
        return dict(st, state="off", next_attempt=None, paused=True,
                    reason=f"paused -- the VPN service is disabled on this machine (to resume: "
                           f"sudo systemctl enable --now ibops-vpn@{name})")
    if row and row["mode"] == "off":
        return dict(st, state="off", next_attempt=None,
                    reason=f"disconnected on purpose by {row['by']} at {str(row['at'])[:16]}")
    if row and row["mode"] == "connecting":
        from core import ops_db
        t = ops_db.parse(row["at"])
        import datetime as _dt
        if t and _dt.datetime.now() - t < _dt.timedelta(minutes=2):
            return dict(st, state="connecting", reason=f"Connect pressed by {row['by']}", next_attempt=None,
                        by_settings=True)
        _clear(name)
    return dict(st, state="service_down", next_attempt=None,
                reason=f"the VPN service ibops-vpn@{name} is not running (stopped or crashed)")


def local_names() -> list[str]:
    """VPN watchdogs this machine runs: from systems.yaml (check: openfortivpn:<name>) and the status folder."""
    names: set[str] = set()
    try:
        from core.config_loader import get_all_systems
        for cfg in get_all_systems():
            net = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}
            check = str(net.get("check") or "")
            if check.lower().startswith("openfortivpn"):
                names.add(check.split(":", 1)[1].strip() if ":" in check else str(net.get("name") or ""))
    except Exception:
        pass
    try:
        from core.reachability import _VPN_STATUS_DIR
        if os.path.isdir(_VPN_STATUS_DIR):
            names |= {f[:-5] for f in os.listdir(_VPN_STATUS_DIR) if f.endswith(".json")}
    except Exception:
        pass
    ipsec_dir = os.getenv("IBO_VPN_IPSEC_DIR", "/etc/ibops-vpn")     # IPsec VPNs (packaging/vpn/install_ipsec_vpn.sh)
    try:
        if os.path.isdir(ipsec_dir):
            names |= {f[:-5] for f in os.listdir(ipsec_dir) if f.endswith(".conf")}
    except Exception:
        pass
    return sorted(n for n in names if _NAME.match(n))


def systems_behind(name: str) -> list[str]:
    out = []
    try:
        from core.config_loader import get_all_systems
        for cfg in get_all_systems():
            net = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}
            check = str(net.get("check") or "").lower()
            if check in (f"openfortivpn:{name}".lower(),) or check.endswith(f"/{name}".lower()):
                out.append(cfg.get("name"))
    except Exception:
        pass
    return [s for s in out if s]


def _sudo(args: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(["sudo", "-n"] + args, capture_output=True, text=True, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    text = (r.stdout + r.stderr).strip()
    if r.returncode != 0 and ("password is required" in text or "not allowed" in text or "sudoers" in text):
        return False, ("this dashboard is not allowed to switch the VPN yet -- run once on this machine: "
                       "sudo bash packaging/ubuntu/allow_vpn_control.sh")
    return r.returncode == 0, text[:300]


def _check(name: str) -> None:
    if not supported():
        raise ValueError("VPN control works only on the Linux machine that runs the VPN.")
    if not _NAME.match(name or "") or name not in local_names():
        raise ValueError(f"No VPN named {name!r} on this machine.")


def _log(name: str, title: str, by: str, severity: str = "info") -> None:
    try:
        from core import timeline
        for s in systems_behind(name) or ["IBOPS"]:
            timeline.add(s, "config", title, f"VPN {name}", severity=severity, actor=by or timeline.actor_name())
    except Exception:
        pass


def disconnect(name: str, by: str) -> dict:
    _check(name)
    from core.reachability import watchdog_status
    before = watchdog_status(name) or {}
    prev = switch_row(name)
    # Remember what the watchdog was doing (see the table note above). A
    # second Disconnect keeps the first one's memory.
    prev_state = (prev or {}).get("prev_state") if (before.get("state") in ("off", None)) else before.get("state")
    prev_next = (prev or {}).get("prev_next") if (before.get("state") in ("off", None)) else before.get("next_attempt")
    ok, out = _sudo([_systemctl(), "stop", f"ibops-vpn@{name}"])
    if not ok:
        raise RuntimeError(f"Could not stop the VPN: {out}")
    db = _db()
    db.execute("INSERT INTO vpn_switch(name, mode, by, at, prev_state, prev_next) VALUES (?,?,?,?,?,?) "
               "ON CONFLICT(name) DO UPDATE SET mode=excluded.mode, by=excluded.by, at=excluded.at, "
               "prev_state=excluded.prev_state, prev_next=excluded.prev_next",
               (name, "off", by or "?", db.now(), prev_state, prev_next))
    _cache.pop(name, None)
    try:
        from core import reachability
        for s in systems_behind(name):
            reachability.forget(s)          # no stale VPN / DOWN state while it is off on purpose
    except Exception:
        pass
    _log(name, f"VPN {name} disconnected on purpose", by, "warning")
    log.warning(f"VPN {name} disconnected from the Settings page by {by}")
    return {"name": name, "state": "off"}


def connect(name: str, by: str) -> dict:
    _check(name)
    from core import ops_db
    from core.reachability import watchdog_status
    st = watchdog_status(name) or {}
    row = switch_row(name) or {}
    state, nxt = st.get("state"), st.get("next_attempt") or ""
    if state == "off" and row.get("prev_state") == "in_use":
        state, nxt = "in_use", row.get("prev_next") or ""
        t = ops_db.parse(nxt)
        import datetime as _dt
        if t is None or t <= _dt.datetime.now():
            state = None                            # the back-off it was waiting for is over
    if state == "in_use":
        raise ValueError(f"The VPN account is in use by another session. Connecting now could push that person "
                         f"off, so it is not done. Try again after {nxt[11:16] or 'the next retry time'} -- "
                         f"or, if it was running, the VPN retries by itself.")
    if st.get("state") == "stopped" or row.get("prev_state") == "stopped":
        ok, out = _sudo([_cli(), "reset", name])
        if not ok:
            raise RuntimeError(f"Could not reset the VPN: {out}")
    ok, out = _sudo([_systemctl(), "start", f"ibops-vpn@{name}"])
    if not ok:
        raise RuntimeError(f"Could not start the VPN: {out}")
    db = _db()
    db.execute("INSERT INTO vpn_switch(name, mode, by, at) VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
               "mode=excluded.mode, by=excluded.by, at=excluded.at, prev_state=NULL, prev_next=NULL",
               (name, "connecting", by or "?", db.now()))
    _cache.pop(name, None)
    _log(name, f"VPN {name} connect requested", by)
    log.info(f"VPN {name} connect requested from the Settings page by {by}")
    return {"name": name, "state": "connecting"}


def status_all() -> list[dict]:
    from core.reachability import watchdog_status
    out = []
    for name in local_names():
        st = watchdog_status(name) or {"state": "unknown"}
        out.append({"name": name, "where": "this machine", "state": st.get("state"),
                    "reason": st.get("reason") or st.get("detail") or "", "since": st.get("since") or st.get("at"),
                    "next_attempt": st.get("next_attempt"), "systems": systems_behind(name),
                    "manual_off": manual_off(name), "controllable": supported(),
                    "paused": bool(st.get("paused"))})
    return out
