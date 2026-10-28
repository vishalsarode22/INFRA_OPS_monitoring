"""
Cross-system correlation: several systems in trouble at the same time point
at what they share, not at SAP.

Every 5 minutes, the systems currently in trouble are collected:
    * not reachable (any reachability state), or
    * last sweep / live grade CRITICAL in the last 30 minutes, or
    * a value unusual for the time (learned baselines) in the last 30 minutes,
and grouped by what they share:
    network   the same `network: name:` (VPN)
    subnet    the same /24 of the RFC host
    db_host   the same database host (from change tracking's RFC_SYSTEM_INFO)
    customer  the same customer
A group with 2 or more troubled systems out of the ones sharing it gets one
mail to Basis ("[CORRELATED] PS4, CARFOUR QAS on Infrabeat VPN at once")
and a timeline line on each system -- once per group per 2 hours.

A whole VPN being down is already one mail (core/reachability.py); this is
for the cases where the systems answer but are all slow or failing.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)


def _troubled() -> dict[str, str]:
    from core.config_loader import get_all_systems
    from core.ops_db import parse
    out: dict[str, str] = {}
    try:
        from core.reachability import current_failures
        for k, v in current_failures().items():
            # VPN / tunnel / collector problems are already one mail per
            # network (core/reachability.py); correlating them again adds noise.
            if v.get("state") in ("SYSTEM DOWN", "NOT REACHABLE", "CREDENTIALS"):
                out[k] = f"[{v.get('state')}]"
    except Exception:
        pass
    cutoff = datetime.now() - timedelta(minutes=30)
    for cfg in get_all_systems():
        name = cfg.get("name")
        if not name or name in out:
            continue
        try:
            from core.status_snapshot import load_snapshot
            snap = load_snapshot(name) or {}
            t = parse(snap.get("cycle_timestamp"))
            if t and t >= cutoff and snap.get("overall_status") == "CRITICAL":
                crit = [m.get("name") for m in snap.get("metrics") or [] if m.get("status") == "CRITICAL"]
                out[name] = "critical: " + ", ".join(crit[:3])
                continue
        except Exception:
            pass
        try:
            from core.learned_baseline import flags
            f = flags(name)
            t = parse(f.get("at"))
            if t and t >= cutoff and f.get("unusual"):
                out[name] = "unusual: " + ", ".join(u["metric"] for u in f["unusual"][:3])
        except Exception:
            pass
    try:
        from core import standby
        cfgs = {c.get("name"): c for c in get_all_systems()}
        out = {k: v for k, v in out.items() if k not in cfgs or standby.active(cfgs[k])}
    except Exception:
        pass
    return out


def _groups(cfgs: list[dict]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}

    def add(key, name):
        groups.setdefault(key, [])
        if name not in groups[key]:
            groups[key].append(name)
    for cfg in cfgs:
        name = cfg.get("name")
        net = (cfg.get("network") or {}).get("name") if isinstance(cfg.get("network"), dict) else None
        if net:
            add(f"network {net}", name)
        host = str((cfg.get("rfc") or {}).get("ashost") or "")
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_loopback:
                add(f"subnet {ipaddress.ip_network(f'{host}/24', strict=False)}", name)
        except ValueError:
            pass
        try:
            from core.change_tracking import load
            db = (load(name).get("kernel") or {}).get("db_host")
            if db:
                add(f"database host {db}", name)
        except Exception:
            pass
        if cfg.get("customer"):
            add(f"customer {cfg['customer']}", name)
    return groups


def run() -> int:
    """One pass; returns the number of correlated groups found."""
    from core.config_loader import get_all_systems
    cfgs = [c for c in get_all_systems() if c.get("name")]
    troubled = _troubled()
    if len(troubled) < 2:
        return 0
    found = 0
    reported: set[frozenset] = set()
    for key, members in sorted(_groups(cfgs).items(), key=lambda kv: -len(kv[1])):
        hit = [m for m in members if m in troubled]
        if len(hit) < 2 or frozenset(hit) in reported:
            continue
        reported.add(frozenset(hit))
        found += 1
        _report(key, hit, members, troubled)
    return found


def _report(key: str, hit: list[str], members: list[str], troubled: dict[str, str]) -> None:
    import html
    from core import recipients, timeline
    from core.config_loader import get_smtp_config
    from notifications.mailer import send_html
    names = ", ".join(sorted(hit))
    body = (f"<p><b>{len(hit)} of the {len(members)} systems on {html.escape(key)} are in trouble at the same "
            f"time.</b> Look at what they share before looking at each SAP system.</p><ul>"
            + "".join(f"<li>{html.escape(s)}: {html.escape(troubled[s])}</li>" for s in sorted(hit)) + "</ul>")
    sent = send_html(subject=f"[CORRELATED] InfraBeatOps -- {names} on {key} at once", html=body,
                     to=recipients.basis(None), smtp_config=get_smtp_config(None, require_recipients=False),
                     system="IBOPS", kind="correlation", key=f"{key}|{names}", min_interval_s=7200,
                     severity="WARNING")
    if sent:
        for s in hit:
            timeline.add(s, "correlation", f"Same time as {', '.join(x for x in hit if x != s)} ({key})",
                         troubled[s], severity="warning")
