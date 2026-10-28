"""
Failover: a system marked standby on this dashboard is monitored here only
while the machine that normally monitors it is silent.

On the Ubuntu VM, in config/systems.yaml:
    - name: PS4
      standby_for: GJ-BASISVM          # the Windows VM's heartbeat name
      rfc: {...}                       # must be reachable from Ubuntu

While GJ-BASISVM's heartbeat (core/peer_watch.py) is fresh, PS4 is
"standby": no live reads, no reachability or check alerts, no scheduled
sweeps -- the Windows VM does all of that. When the heartbeat has been
silent for IBO_PEER_SILENT_MINUTES, PS4 becomes active here: live reads,
DOWN / VPN / check alerts and capacity checks run from Ubuntu (screenshots
cannot fail over -- they need SAP GUI on Windows). When the heartbeat
returns, it goes back to standby. Both changes go on the timeline and
are mailed once.

A standby system whose peer has NEVER sent a heartbeat stays standby (so
the two machines do not both alert while the heartbeat is not set up) and
the Health page says so.
"""
from __future__ import annotations

import threading

from utils.logger import get_logger

log = get_logger(__name__)

_state: dict[str, bool] = {}
_lock = threading.Lock()


def peer_of(cfg: dict | None) -> str:
    return str((cfg or {}).get("standby_for") or "").strip()


def peer_state(peer: str) -> str:
    """'up', 'silent' or 'never'."""
    try:
        from core import peer_watch
        for p in peer_watch.peers():
            if p["name"].lower() == peer.lower():
                return "silent" if p["silent"] else "up"
    except Exception:
        return "up"         # cannot tell: do not take over
    return "never"


def active(cfg: dict | None) -> bool:
    """True when this dashboard should monitor the system now."""
    peer = peer_of(cfg)
    if not peer:
        return True
    st = peer_state(peer)
    now_active = st == "silent"
    name = (cfg or {}).get("name", "?")
    with _lock:
        prev = _state.get(name)
        _state[name] = now_active
    if prev is not None and prev != now_active:
        _announce(name, peer, now_active)
    elif prev is None and not now_active:
        # First look after a start: a state recorded while this dashboard was
        # active (before a restart) is stale now -- the peer owns it.
        try:
            from core import reachability
            reachability.forget(name)
        except Exception:
            pass
    return now_active


def _announce(system: str, peer: str, now_active: bool) -> None:
    if not now_active:
        try:
            from core import reachability
            reachability.forget(system)     # the peer owns its state again
        except Exception:
            pass
    try:
        from core import timeline
        timeline.add(system, "failover",
                     f"Failover: monitoring taken over from {peer}" if now_active
                     else f"Failover ended: {peer} is back, this dashboard is standby again",
                     severity="warning" if now_active else "ok")
        log.warning(f"{system}: {'now ACTIVE here' if now_active else 'back to standby'} (peer {peer})")
    except Exception:
        pass
    try:
        from core import recipients
        from core.config_loader import get_smtp_config
        from notifications.mailer import send_html
        to, cc = recipients.basis_and_manager(None)
        send_html(subject=(f"[FAILOVER] InfraBeatOps -- {system} now monitored from the standby"
                           if now_active else f"[FAILOVER ENDED] InfraBeatOps -- {system} back on {peer}"),
                  html=(f"<p>{system} is {'now monitored from this (standby) dashboard because ' + peer + ' went silent' if now_active else 'monitored by ' + peer + ' again'}."
                        f" Screenshot sweeps do not fail over; RFC reads, reachability and check alerts do.</p>"),
                  to=to, cc=cc, smtp_config=get_smtp_config(None, require_recipients=False),
                  system=system, kind="failover", key=f"{peer}:{now_active}", min_interval_s=600,
                  severity="WARNING")
    except Exception:
        pass


def only_active(failures: dict) -> dict:
    """`failures` (system -> anything) without systems the peer monitors now."""
    try:
        from core.config_loader import get_all_systems
        cfgs = {c.get("name"): c for c in get_all_systems()}
        return {k: v for k, v in failures.items() if k not in cfgs or active(cfgs[k])}
    except Exception:
        return failures


def status() -> list[dict]:
    from core.config_loader import get_all_systems
    out = []
    for cfg in get_all_systems():
        peer = peer_of(cfg)
        if peer:
            out.append({"system": cfg.get("name"), "peer": peer, "peer_state": peer_state(peer),
                        "active": active(cfg)})
    return out
