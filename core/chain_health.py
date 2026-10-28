"""
Health page for the monitoring chain: everything we checked by hand.

    this dashboard      process role, host, database, disk space
    services            heartbeats of web / collector / jobs (split mode)
    other dashboards    each remote source: does /healthz answer, how fast
    tunnel              each forward in config/tunnel_forwards.txt: is the
                        local port listening, does the far end answer
    VPN                 each VPN the systems depend on: watchdog state,
                        local (openfortivpn) or asked over the tunnel
    peers               Windows VM heartbeats seen by this dashboard
    sweeps              per system: last sweep, its result, parked,
                        maintenance, reachability state
    mail                e-mail switched on, SMTP settings complete, last
                        mail sent / failed, queue backlog
    platform            public URL for mail buttons, on-call, tickets,
                        one-click actions

Every row: level (ok | warn | bad | off), a name, and one sentence.
"""
from __future__ import annotations

import os
import shutil
import socket
import time
from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__)


def _row(group: str, name: str, level: str, text: str, **extra) -> dict:
    return {"group": group, "name": name, "level": level, "text": text, **extra}


def _age_text(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def _since(stamp: str | None) -> float | None:
    from core import ops_db
    t = ops_db.parse(stamp)
    return None if t is None else (datetime.now() - t).total_seconds()


def this_dashboard() -> list[dict]:
    from core import store
    rows = []
    role = (os.getenv("IBO_ROLE", "all") or "all").lower()
    rows.append(_row("dashboard", "This dashboard", "ok", f"running on {socket.gethostname()} (role {role})"))
    try:
        size = os.path.getsize(store.DB_PATH) / 1024 / 1024
        rows.append(_row("dashboard", "Database", "ok", f"{store.DB_PATH} -- {size:.1f} MB"))
    except OSError:
        rows.append(_row("dashboard", "Database", "warn", "database file not found"))
    try:
        free = shutil.disk_usage(os.path.dirname(store.DB_PATH)).free / 1024 ** 3
        rows.append(_row("dashboard", "Disk space", "ok" if free > 5 else "warn" if free > 1 else "bad",
                         f"{free:.1f} GB free on the dashboard's drive"))
    except OSError:
        pass
    return rows


def services() -> list[dict]:
    from core import store
    rows = []
    role = (os.getenv("IBO_ROLE", "all") or "all").lower()
    for h in store.heartbeats():
        if h["service"].startswith(("peer:", "loop:")):
            continue
        age = h.get("age_seconds")
        level = "ok" if age is not None and age < 120 else "warn" if age is not None and age < 600 else "bad"
        if role == "all" and h["service"] in ("collector", "jobs"):
            level = "off" if level != "ok" else level
        rows.append(_row("services", f"Service {h['service']}", level,
                         f"heartbeat {_age_text(age)} on {h['host']} -- {h.get('detail') or ''}".strip(" -")))
    return rows


def loops() -> list[dict]:
    """Supervised background loops (core/supervisor.py), from every process on this machine."""
    from core import store
    rows = []
    for h in store.heartbeats():
        if not h["service"].startswith("loop:"):
            continue
        name = h["service"][len("loop:"):]
        detail = h.get("detail") or ""
        age = h.get("age_seconds")
        if detail.startswith("stopped (returned on purpose)"):
            level = "off"
        elif age is None or age > 180:
            level = "bad"
            detail = f"no sign of life for {_age_text(age)} -- the process running it has stopped? ({detail})"
        elif "restarting" in detail or "last error" in detail:
            level = "warn"
        else:
            level = "ok"
        rows.append(_row("loops", f"Loop {name}", level, f"{detail} (on {h['host']})"))
    return rows


def failover() -> list[dict]:
    try:
        from core import standby
        rows = []
        for st in standby.status():
            level = {"up": "off", "silent": "warn", "never": "warn"}.get(st["peer_state"], "warn")
            text = {"up": f"standby -- {st['peer']} is up and monitors it",
                    "silent": f"ACTIVE here -- {st['peer']} is silent",
                    "never": f"standby, but {st['peer']} has never sent a heartbeat: failover cannot work "
                             f"until it does (token in its remote_sources.yaml?)"}[st["peer_state"]]
            rows.append(_row("failover", f"Standby {st['system']}", level, text, system=st["system"]))
        return rows
    except Exception as exc:  # noqa: BLE001
        return [_row("failover", "Failover", "warn", f"status unavailable: {exc}")]


def error_summary() -> list[dict]:
    try:
        from core import errors
        s = errors.summary(24)
    except Exception as exc:  # noqa: BLE001
        return [_row("errors", "Errors", "warn", f"error list unavailable: {exc}")]
    n = s["open_groups"]
    return [_row("errors", "Unhandled errors (24 h)", "ok" if not n else "warn" if n < 5 else "bad",
                 "none" if not n else f"{n} open kind(s), {s['occurrences']} occurrence(s) -- see the Errors page")]


def remote_dashboards() -> list[dict]:
    rows = []
    try:
        from dashboard.remote_sources import load_sources
        sources = load_sources()
    except Exception:
        sources = []
    import urllib.request
    for src in sources:
        t0 = time.monotonic()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(src["url"] + "/healthz", timeout=6) as resp:
                ms = (time.monotonic() - t0) * 1000
                rows.append(_row("dashboards", f"{src['label']} dashboard", "ok" if resp.status < 300 else "bad",
                                 f"{src['url']} answers in {ms:.0f} ms"))
        except Exception as exc:  # noqa: BLE001
            why = str(getattr(exc, "reason", exc))
            rows.append(_row("dashboards", f"{src['label']} dashboard", "bad",
                             f"{src['url']} does not answer ({why[:120]}) -- tunnel down or dashboard stopped"))
    return rows


def tunnel() -> list[dict]:
    from core import reachability
    rows = []
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "tunnel_forwards.txt")
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split("#", 1)[0].strip().split(":")
            if len(parts) != 4:
                continue
            lh, lp, th, tp = parts
            listening = reachability.tcp_open(lh, int(lp), timeout=1)
            if not listening:
                rows.append(_row("tunnel", f"{lh}:{lp} -> {th}:{tp}", "bad",
                                 "nothing listens locally: the SSH tunnel is down (check the IBOPS Tunnel task)"))
                continue
            far = reachability.tcp_open_far_end(lh, int(lp), wait=1.5)
            rows.append(_row("tunnel", f"{lh}:{lp} -> {th}:{tp}", "ok" if far else "warn",
                             "tunnel up and the SAP system answers through it" if far else
                             "tunnel up, but the far end does not answer (VPN on Ubuntu down, or the system)"))
    return rows


def vpns() -> list[dict]:
    from core import reachability
    from core.config_loader import get_all_systems
    rows, seen = [], set()
    for cfg in get_all_systems():
        net = cfg.get("network") or {}
        check = str(net.get("check") or "").strip()
        if not check or check in seen:
            continue
        seen.add(check)
        label = str(net.get("name") or check)
        systems = [s.get("name") for s in get_all_systems() if str((s.get("network") or {}).get("check") or "") == check]
        if check.lower().startswith("remote:"):
            st = reachability.remote_vpn(cfg, fresh=True)
            if st is None:
                rows.append(_row("vpn", label, "bad", f"cannot ask the collector ({check}) -- tunnel or "
                                 f"dashboard down; affects {', '.join(systems)}"))
                continue
            state = st.get("state")
        elif check.lower().startswith("openfortivpn"):
            name = check.split(":", 1)[1].strip()
            st = reachability.watchdog_status(name) or {}
            state = st.get("state")
        elif check.lower().startswith("forticlient"):
            up, detail = reachability.forticlient_status(check.split(":", 1)[1] if ":" in check else None)
            rows.append(_row("vpn", label, "ok" if up else "bad" if up is False else "warn",
                             f"FortiClient: {detail}; affects {', '.join(systems)}"))
            continue
        else:
            continue
        level = {"up": "ok", "retrying": "warn", "connecting": "warn", "in_use": "warn",
                 "off": "off"}.get(state, "bad")
        text = {"up": "connected", "retrying": f"reconnecting ({st.get('reason', '')})",
                "off": f"OFF on purpose -- {st.get('reason', '')} (Connect on the Settings page)",
                "in_use": f"account in use elsewhere -- not pushing them off ({st.get('reason', '')})",
                "stopped": f"automatic reconnect STOPPED: {st.get('reason', '')} -- sudo ibops-vpn reset"}.get(
            state, f"state {state or 'unknown'}")
        rows.append(_row("vpn", label, level, f"{text}; affects {', '.join(systems)}"))
    # A VPN watchdog on THIS machine that no system references (the Ubuntu VM).
    vdir = os.getenv("IBO_VPN_STATUS_DIR", "/run/ibops-vpn")
    if os.path.isdir(vdir):
        for f in sorted(os.listdir(vdir)):
            if not f.endswith(".json"):
                continue
            name = f[:-5]
            if any(name in c for c in seen):
                continue
            st = reachability.watchdog_status(name) or {}
            state = st.get("state")
            rows.append(_row("vpn", f"VPN {name} (local watchdog)",
                             "ok" if state == "up" else "off" if state == "off"
                             else "warn" if state in ("retrying", "in_use", "connecting") else "bad",
                             f"{state or 'unknown'} {st.get('reason') or ''}".strip()))
    # Windows: the VPN keeper (core/vpn_keeper.py) on THIS machine.
    try:
        from core import vpn_keeper
        if vpn_keeper.configured():
            sts = vpn_keeper.status()
            for v in vpn_keeper.load_config():
                st = sts.get(v["name"]) or {}
                state = st.get("state") or "starting"
                level = {"up": "ok", "connecting": "warn", "retrying": "warn", "in_use": "warn",
                         "starting": "warn", "unknown": "warn"}.get(state, "bad")
                text = {"up": "connected" + (f" (reconnected automatically {st['last_reconnect']})"
                                             if st.get("last_reconnect") else ""),
                        "starting": "first check pending"}.get(state, f"{state}: {st.get('reason') or ''}")
                rows.append(_row("vpn", f"VPN {v['name']} (auto-reconnect)", level,
                                 f"{text}; affects {', '.join(v.get('systems') or []) or '-'}",
                                 vpn_keeper=v["name"] if state in ("stopped", "retrying", "in_use") else None))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("vpn", "VPN auto-reconnect", "warn", f"status unavailable: {exc}"))
    return rows


def peers() -> list[dict]:
    from core import peer_watch
    rows = []
    for p in peer_watch.peers():
        d = p.get("detail") or {}
        extra = []
        if d.get("sweeping"):
            extra.append(f"sweeping {d['sweeping']}")
        if d.get("tunnel_ports_listening"):
            extra.append(f"tunnel ports {d['tunnel_ports_listening']}")
        rows.append(_row("peers", f"Peer {p['name']}", "bad" if p["silent"] else "ok",
                         f"last heartbeat {_age_text(p['age_seconds'])}" + (f" ({', '.join(extra)})" if extra else ""),
                         peer=p["name"]))
    return rows


def sweeps() -> list[dict]:
    from core import alert_control
    from core.config_loader import get_all_systems
    from core.status_snapshot import load_snapshot
    try:
        from core.reachability import current_failures
        fails = current_failures()
    except Exception:
        fails = {}
    rows = []
    for cfg in get_all_systems():
        name = cfg.get("name")
        snap = load_snapshot(name) or {}
        stamp = snap.get("cycle_timestamp")
        age = _since(stamp)
        st = alert_control.status(name)
        notes = []
        level = "ok" if age is not None and age < 26 * 3600 else "warn"
        if st["maintenance"]:
            notes.append(f"in maintenance until {st['maintenance']['ends_at'][11:16]}")
            level = "off"
        if st["parked"]:
            notes.append(f"PARKED ({st['parked'].get('reason', '')})")
            level = "warn"
        f = fails.get(name)
        if f:
            notes.append(f"[{f.get('state')}] since {str(f.get('since', ''))[11:16]}")
            level = "bad" if f.get("state") in ("SYSTEM DOWN", "CREDENTIALS") else "warn"
        text = (f"last sweep {stamp} ({_age_text(age)}), {snap.get('overall_status', '?')}" if stamp
                else "no sweep yet")
        rows.append(_row("sweeps", name, level, text + ("; " + "; ".join(notes) if notes else ""), system=name))
    return rows


def mail() -> list[dict]:
    from core import ops_db
    from notifications.mailer import email_enabled, mail_async
    rows = []
    if not email_enabled():
        rows.append(_row("mail", "E-mail", "bad", "IBO_ENABLE_EMAIL=0 -- no alert or report goes out"))
    try:
        from core.config_loader import get_smtp_config
        smtp = get_smtp_config(None, require_recipients=False)
        rows.append(_row("mail", "SMTP settings", "ok", f"{smtp['host']}:{smtp['port']} as {smtp['from_email']}"))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("mail", "SMTP settings", "bad", str(exc)[:200]))
    hold_prefixes = ("same ", "daily cap", "in maintenance", "snoozed", "snooze by", "acknowledged", "ack by",
                     "ignored", "until_fixed by", "reserved")
    last_ok = last_bad = None
    try:
        last_ok = ops_db.one("SELECT sent_at, subject FROM alerts WHERE suppressed=0 AND reason='' "
                             "ORDER BY id DESC LIMIT 1")
        for r in ops_db.rows("SELECT sent_at, subject, reason FROM alerts WHERE suppressed=1 "
                             "ORDER BY id DESC LIMIT 300"):
            if not str(r["reason"] or "").lower().startswith(hold_prefixes):
                last_bad = r
                break
    except Exception:
        pass
    if last_ok:
        rows.append(_row("mail", "Last alert delivered", "ok", f"{last_ok['sent_at']}: {last_ok['subject'][:90]}"))
    if last_bad and (not last_ok or last_bad["sent_at"] >= last_ok["sent_at"]):
        rows.append(_row("mail", "Last alert FAILED", "bad",
                         f"{last_bad['sent_at']}: {last_bad['subject'][:60]} -- {last_bad['reason'][:120]}"))
    if mail_async():
        try:
            q = ops_db.one("SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM jobs WHERE kind='mail' AND "
                           "status IN ('queued','running')")
            f = ops_db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='mail' AND status='failed' AND created_at>=?",
                           (datetime.now().strftime("%Y-%m-%d 00:00:00"),))
            age = _since(q["oldest"]) if q and q["oldest"] else None
            level = "ok" if not q["n"] or (age or 0) < 300 else "bad"
            rows.append(_row("mail", "Mail queue", level,
                             f"{q['n']} waiting" + (f", oldest {_age_text(age)}" if age else "")
                             + (f"; {f['n']} failed today" if f and f["n"] else "")))
        except Exception:
            pass
    return rows


def platform() -> list[dict]:
    rows = []
    from core.action_links import public_url
    url = public_url()
    rows.append(_row("platform", "Mail buttons (snooze / acknowledge)", "ok" if url else "warn",
                     f"links point to {url}" if url else
                     "IBO_PUBLIC_URL is not set -- alert mails go out without the buttons"))
    try:
        from core import oncall
        cur = oncall.current()
        if cur["emails"]:
            rows.append(_row("platform", "On call", "warn" if cur.get("shift_source_error") else "ok",
                             f"{', '.join(cur['names'])} ({cur['source']}, until {cur.get('until') or '?'})"
                             + (f"; shift tool error: {cur['shift_source_error']}" if cur.get("shift_source_error") else "")))
        else:
            rows.append(_row("platform", "On call", "off", "no rotation (config/oncall.yaml) -- IBO_BASIS_EMAILS is used"))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("platform", "On call", "bad", str(exc)[:200]))
    try:
        from core import tickets
        p = tickets.provider()
        if p == "off":
            rows.append(_row("platform", "Tickets", "off", "IBO_TICKETS is off"))
        else:
            from core import ops_db
            bad = ops_db.one("SELECT COUNT(*) AS n FROM tickets WHERE status='failed' OR (status!='closed' AND error!='')")
            rows.append(_row("platform", "Tickets", "warn" if bad and bad["n"] else "ok",
                             f"{p}" + (f"; {bad['n']} with errors (Operations -> Tickets)" if bad and bad["n"] else "")))
    except Exception:
        pass
    try:
        from core import sap_actions
        rows.append(_row("platform", "One-click actions", "ok" if sap_actions.globally_enabled() else "off",
                         ("enabled" + ("; two-person approval" if sap_actions.two_person() else "; confirm dialog"))
                         if sap_actions.globally_enabled() else "IBO_ACTIONS_ENABLED is off"))
    except Exception:
        pass
    try:
        from core import escalation_time
        from notifications import sms
        cfg = escalation_time.config()
        on = bool(cfg) and cfg.get("enabled") is not False
        rows.append(_row("platform", "Escalation by time", "ok" if on else "off",
                         ("on; " if on else "off (config/escalation.yaml); ")
                         + f"MSG91 SMS {'configured' if sms.sms_configured() else 'not configured'}, "
                         f"voice {'configured' if sms.voice_configured() else 'not configured'}"))
    except Exception:
        pass
    try:
        from notifications import whatsapp
        en = whatsapp._enabled()
        rows.append(_row("platform", "WhatsApp alerts", "ok" if en and (os.getenv("WHATSAPP_ALERT_TEMPLATE") or
                                                                        os.getenv("WHATSAPP_MODE") == "webhook")
                         else "warn" if en else "off",
                         ("enabled, kinds: " + ", ".join(sorted(whatsapp._alert_kinds()))) if en else
                         "WHATSAPP_ENABLED is off"))
    except Exception:
        pass
    try:
        from core import sso
        why = sso.problem()
        rows.append(_row("platform", "Microsoft sign-in", "ok" if not why else
                         ("off" if why == "not configured" else "warn"),
                         "configured" if not why else
                         ("not configured (IBO_ENTRA_* in .env)" if why == "not configured" else why)))
    except Exception:
        pass
    try:
        from core import synthetic
        st = synthetic.status()
        bad = [f"{k}: {v['error'][:60]}" for k, v in st["results"].items() if not v.get("ok")]
        stopped = list(st["stopped"])
        res = [f"{k} {v['total_ms']} ms" for k, v in st["results"].items() if v.get("ok")]
        level = "warn" if bad or stopped else "ok" if res else "off"
        rows.append(_row("platform", "Synthetic transaction", level,
                         "; ".join(res + bad + [f"{s} stopped (logon refused)" for s in stopped]) or
                         "off (IBO_SYNTHETIC=1 or synthetic: true per system)"))
    except Exception:
        pass
    try:
        from collectors import extra_checks
        rows.append(_row("platform", "Extra SAP checks", "ok" if extra_checks.enabled() else "off",
                         f"every {int(extra_checks.ttl_seconds() // 60)} min; scripts/probe_checks.py shows what "
                         f"each system supports" if extra_checks.enabled() else "IBO_EXTRA_CHECKS=0"))
    except Exception:
        pass
    return rows


def report() -> dict:
    groups = [("dashboard", "This dashboard", this_dashboard), ("errors", "Errors", error_summary),
              ("services", "Services", services), ("loops", "Background loops", loops),
              ("dashboards", "Other dashboards", remote_dashboards), ("tunnel", "SSH tunnel", tunnel),
              ("vpn", "VPN", vpns), ("peers", "Watched machines", peers), ("failover", "Failover", failover), ("sweeps", "Last sweep per system", sweeps),
              ("mail", "Mail delivery", mail), ("platform", "Platform features", platform)]
    out, rows = [], []
    for key, title, fn in groups:
        try:
            items = fn()
        except Exception as exc:  # noqa: BLE001
            items = [_row(key, title, "bad", f"check failed: {type(exc).__name__}: {exc}")]
        if items:
            out.append({"key": key, "title": title, "rows": items})
            rows += items
    worst = "bad" if any(r["level"] == "bad" for r in rows) else "warn" if any(r["level"] == "warn" for r in rows) else "ok"
    return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "host": socket.gethostname(),
            "overall": worst, "groups": out}
