"""
Maintenance windows, snooze / ignore-until-fixed / acknowledge, and parked
sweeps -- and the one gate every alert passes through.

MAINTENANCE WINDOW
    "PS4 kernel upgrade, 22:00-02:00". Inside the window:
      * no alert mail of any kind for the system (DOWN, VPN, critical,
        sweep failure, T-code failure, recovery ...), held with the reason
        "in maintenance until 02:00" so the ledger shows why;
      * scheduled sweeps are skipped (a manual "Monitor this system" still
        runs -- a person asked for it);
      * the wall shows the card in grey with the reason and end time.
    When the window ends nothing special happens: the next check that still
    finds a problem alerts as usual, because the last mail predates it.
    system '*' means every monitored system (not the platform's own
    MONITOR DOWN / Windows-VM alerts, which stay on).

SILENCES (from the wall, or the links in the alert mail)
    snooze        no repeats of this alert until a time (1 h, 4 h, ...)
    until_fixed   "Ignore until fixed": no repeats until the problem
                  clears (reachability recovers, the metric is no longer
                  critical, the next sweep succeeds) -- also parks sweeps
    ack           "Acknowledge": same as until_fixed, and the wall shows who
                  is on it
    kind '*' covers every alert kind for the system. A RECOVERED mail is
    never silenced by a snooze or an acknowledgement (people want to know
    it is back); it is held only by a maintenance window.

    PER-VALUE SILENCES (the buttons in a per-value alert mail)
    kind 'metric:<key>' or 'metric:<key>@<instance>' (one user, one table)
    covers that one value only -- never the system's other values, and an
    "Ignore" of one value does not park the system's sweeps. Acknowledge and
    Ignore end by themselves when the value is back to normal. A silence set
    from a WARNING mail does not hold a rise to CRITICAL.

PARKED SWEEPS
    A parked system's scheduled sweeps are skipped until someone clears
    the flag on the wall (or the mail link). Parked automatically when:
      * the monitoring user's logon is refused (CREDENTIALS) -- repeated
        failed logons lock the SAP user, so we stop trying;
      * someone presses "Ignore until fixed".
    IBO_AUTO_PARK lists the states that park automatically
    (default "credentials"; add "system_down" or "vpn_down" if wanted).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from core import ops_db, timeline
from utils.logger import get_logger

log = get_logger(__name__)

PLATFORM_SYSTEMS = ("IBOPS",)            # the platform's own alerts
PLATFORM_KINDS = ("monitor_down", "monitor_recovered", "peer_silent", "peer_recovered")
RECOVERY_KINDS = ("recovered", "monitor_recovered", "peer_recovered")
MODES = ("snooze", "until_fixed", "ack")


def _max_hours() -> float:
    try:
        return float(os.getenv("IBO_MAINT_MAX_HOURS", "168") or 168)
    except ValueError:
        return 168.0


# ------------------------------------------------------------- maintenance

def create_window(system: str, starts_at, ends_at, reason: str = "", by: str = "") -> dict:
    """Validate and store a window. Raises ValueError with a sentence to show."""
    system = str(system or "").strip()
    if not system:
        raise ValueError("Choose a system (or * for all systems).")
    start = starts_at if isinstance(starts_at, datetime) else ops_db.parse(starts_at)
    end = ends_at if isinstance(ends_at, datetime) else ops_db.parse(ends_at)
    if start is None:
        start = datetime.now()
    if end is None:
        raise ValueError("Give an end time for the window.")
    if end <= start:
        raise ValueError("The window must end after it starts.")
    if end <= datetime.now():
        raise ValueError("That window is already over.")
    if (end - start).total_seconds() > _max_hours() * 3600:
        raise ValueError(f"A window can be at most {int(_max_hours())} hours "
                         f"(IBO_MAINT_MAX_HOURS). Split a longer outage into parts.")
    if not str(reason or "").strip():
        raise ValueError("Say why (kernel upgrade, restart, DB maintenance ...) -- it is shown "
                         "on the wall and in the timeline.")
    by = by or timeline.actor_name()
    wid = ops_db.execute(
        "INSERT INTO maintenance(system, starts_at, ends_at, reason, created_by, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (system, ops_db.fmt(start), ops_db.fmt(end), reason.strip()[:300], by, ops_db.now()))
    for s in _expand(system):
        timeline.add(s, "maintenance", f"Maintenance window set: {start:%d %b %H:%M} - {end:%d %b %H:%M}",
                     reason.strip(), actor=by, ref=f"maintenance:{wid}")
    log.info(f"maintenance window {wid} for {system}: {start} - {end} ({reason}) by {by}")
    return get_window(wid)


def get_window(wid: int) -> dict | None:
    return ops_db.one("SELECT * FROM maintenance WHERE id=?", (int(wid),))


def cancel_window(wid: int, by: str = "") -> dict:
    w = get_window(wid)
    if not w:
        raise ValueError("No such maintenance window.")
    if w.get("cancelled_at"):
        return w
    by = by or timeline.actor_name()
    now = ops_db.now()
    # Ending a running window early keeps its start; a future one is cancelled.
    ops_db.execute("UPDATE maintenance SET cancelled_at=?, cancelled_by=? WHERE id=?", (now, by, int(wid)))
    for s in _expand(w["system"]):
        timeline.add(s, "maintenance", "Maintenance window ended early" if w["starts_at"] <= now
                     else "Maintenance window cancelled", w.get("reason", ""), actor=by,
                     ref=f"maintenance:{wid}")
    return get_window(wid)


def windows(include_past_days: int = 7) -> list[dict]:
    cutoff = ops_db.fmt(datetime.now() - timedelta(days=include_past_days))
    out = ops_db.rows("SELECT * FROM maintenance WHERE ends_at>=? ORDER BY starts_at", (cutoff,))
    now = ops_db.now()
    for w in out:
        w["state"] = ("cancelled" if w.get("cancelled_at") else
                      "active" if w["starts_at"] <= now < w["ends_at"] else
                      "planned" if w["starts_at"] > now else "ended")
    return out


def active_window(system: str, at: datetime | None = None) -> dict | None:
    """The window covering `system` now (or at `at`), or None."""
    if not system:
        return None
    when = ops_db.fmt(at or datetime.now())
    try:
        if system in PLATFORM_SYSTEMS:
            args, where = (system,), "system=?"
        else:
            args, where = (system,), "(system=? OR system='*')"
        return ops_db.one(
            f"SELECT * FROM maintenance WHERE {where} AND cancelled_at IS NULL AND starts_at<=? "
            f"AND ends_at>? ORDER BY ends_at DESC LIMIT 1", (*args, when, when))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"maintenance lookup failed: {exc}")
        return None


def in_maintenance(system: str) -> bool:
    return active_window(system) is not None


def windows_ended_since(stamp: str) -> list[dict]:
    """Windows whose end (or cancellation) fell after `stamp` and is now past."""
    now = ops_db.now()
    return ops_db.rows(
        "SELECT * FROM maintenance WHERE (cancelled_at IS NULL AND ends_at>? AND ends_at<=?) "
        "OR (cancelled_at>? AND cancelled_at<=? AND starts_at<=cancelled_at)",
        (stamp, now, stamp, now))


# ---------------------------------------------------------------- silences

def is_value_kind(kind) -> bool:
    return str(kind or "").startswith("metric:")


def silence(system: str, kind: str = "*", mode: str = "snooze", minutes: float | None = None,
            reason: str = "", by: str = "", level: str = "", label: str = "") -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    system = str(system or "").strip()
    if not system:
        raise ValueError("system is required")
    kind = str(kind or "*").strip() or "*"
    by = by or timeline.actor_name()
    until = None
    if mode == "snooze":
        try:
            minutes = float(minutes if minutes is not None else 60)
        except (TypeError, ValueError):
            raise ValueError("minutes must be a number")
        if minutes <= 0 or minutes > 7 * 24 * 60:
            raise ValueError("Snooze between 1 minute and 7 days.")
        until = ops_db.fmt(datetime.now() + timedelta(minutes=minutes))
    level = str(level or "").upper()
    if level not in ("WARNING", "CRITICAL"):
        level = ""
    label = str(label or "")[:120]
    sid = ops_db.execute(
        "INSERT INTO silences(system, kind, mode, until, reason, created_by, created_at, level, label) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (system, kind, mode, until, str(reason or "")[:300], by, ops_db.now(), level, label))
    title = {"snooze": f"Snoozed until {until[11:16] if until else ''}",
             "until_fixed": "Ignored until back to normal" if is_value_kind(kind) else "Ignored until fixed",
             "ack": "Acknowledged"}[mode]
    what = ("all alerts" if kind == "*" else f"{label or kind[len('metric:'):]} alert" if is_value_kind(kind)
            else f"{kind} alerts")
    timeline.add(system, "silence", f"{title} ({what})", reason, actor=by, ref=f"silence:{sid}")
    if mode == "until_fixed" and not is_value_kind(kind):
        park(system, reason=f"ignored until fixed by {by}", by=by)
    return ops_db.one("SELECT * FROM silences WHERE id=?", (sid,))


def active_silences(system: str | None = None) -> list[dict]:
    now = ops_db.now()
    sql = ("SELECT * FROM silences WHERE cleared_at IS NULL AND (mode!='snooze' OR until>?)")
    args: list = [now]
    if system:
        sql += " AND system=?"
        args.append(system)
    return ops_db.rows(sql + " ORDER BY id DESC", args)


def silenced(system: str, kind: str) -> dict | None:
    for s in active_silences(system):
        if s["kind"] in ("*", kind):
            return s
    return None


def value_kinds(key) -> tuple:
    """The silence kinds that cover one value: 'metric:<key>' and, per instance, 'metric:<key>@<inst>'."""
    key = str(key or "")
    if not key:
        return ()
    base = key.split("@", 1)[0]
    return tuple(dict.fromkeys((f"metric:{base}", f"metric:{key}")))


def _value_silence(system: str, key, level: str | None) -> dict | None:
    """The silence holding this one value at this level, or None."""
    kinds = value_kinds(key)
    lvl = str(level or "").upper()
    for s in active_silences(system):
        if s["kind"] not in kinds:
            continue
        # set from a WARNING mail: a rise to CRITICAL still goes out
        if lvl == "CRITICAL" and str(s.get("level") or "").upper() == "WARNING":
            continue
        return s
    return None


def clear_silence(sid: int, by: str = "") -> None:
    s = ops_db.one("SELECT * FROM silences WHERE id=?", (int(sid),))
    if not s or s.get("cleared_at"):
        return
    by = by or timeline.actor_name()
    ops_db.execute("UPDATE silences SET cleared_at=?, cleared_by=? WHERE id=?", (ops_db.now(), by, int(sid)))
    timeline.add(s["system"], "silence", f"Silence cleared ({s['mode']}, {s['kind']})", actor=by,
                 ref=f"silence:{sid}")


def clear_until_fixed(system: str, kinds=None, by: str = "auto: problem cleared") -> int:
    """
    The problem behind an ignore/ack is gone: drop those silences so the
    next occurrence alerts again. Snoozes are time-bound and stay.
    """
    n = 0
    try:
        for s in active_silences(system):
            if s["mode"] == "snooze":
                continue
            if kinds is not None and (s["kind"] == "*" or s["kind"] not in kinds):
                # A wall-level ack ('*') is cleared only by an explicit full
                # clear (ticket closed, kinds=None): one metric going green does
                # not mean the person is done with the system.
                continue
            ops_db.execute("UPDATE silences SET cleared_at=?, cleared_by=? WHERE id=?",
                           (ops_db.now(), by, s["id"]))
            n += 1
        if n:
            timeline.add(system, "silence", f"{n} acknowledgement(s) cleared -- problem fixed", actor=by,
                         severity="ok")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"clear_until_fixed failed: {exc}")
    return n


# ------------------------------------------------------------------ parking

def park(system: str, reason: str = "", by: str = "", auto: bool = False) -> None:
    by = by or timeline.actor_name()
    existing = ops_db.one("SELECT * FROM parked WHERE system=?", (system,))
    if existing:
        return
    ops_db.execute("INSERT OR REPLACE INTO parked(system, reason, parked_by, parked_at, auto) "
                   "VALUES (?,?,?,?,?)", (system, str(reason or "")[:300], by, ops_db.now(), int(auto)))
    timeline.add(system, "park", "Sweeps parked", reason, actor=by, severity="warning")
    log.info(f"{system}: scheduled sweeps parked ({reason})")


def unpark(system: str, by: str = "") -> bool:
    by = by or timeline.actor_name()
    n = ops_db.execute("DELETE FROM parked WHERE system=?", (system,))
    if n:
        timeline.add(system, "park", "Sweeps resumed (parked flag cleared)", actor=by, severity="ok")
        try:   # a cleared flag after a credential fix deserves an immediate retry
            from collectors.rfc_collector import clear_cooldown
            clear_cooldown(system)
        except Exception:
            pass
    return bool(n)


def parked(system: str) -> dict | None:
    try:
        return ops_db.one("SELECT * FROM parked WHERE system=?", (system,))
    except Exception:
        return None


def all_parked() -> list[dict]:
    return ops_db.rows("SELECT * FROM parked ORDER BY parked_at")


def auto_park_states() -> set[str]:
    raw = os.getenv("IBO_AUTO_PARK", "credentials")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def sweep_block_reason(system: str) -> str | None:
    """Why a SCHEDULED sweep of `system` must not run now, or None."""
    try:
        from core import standby
        from core.config_loader import get_all_systems
        cfg = next((c for c in get_all_systems() if c.get("name") == system), None)
        if cfg is not None and not standby.active(cfg):
            return f"standby -- monitored by {standby.peer_of(cfg)} while it is up"
    except Exception:
        pass
    try:
        # VPN switched off on purpose (Settings page): no sweep, no failure mails.
        net = (cfg or {}).get("network") if isinstance((cfg or {}).get("network"), dict) else {}
        check = str(net.get("check") or "")
        if check:
            from core import reachability
            if check.lower().startswith("openfortivpn"):
                vname = check.split(":", 1)[1].strip() if ":" in check else str(net.get("name") or "")
                st = reachability.watchdog_status(vname)
                pre = reachability.watchdog_message(vname, st) if st else None
            else:
                pre = reachability.precheck(system, cfg)
            if reachability.state_of(pre) == reachability.VPN_OFF:
                return pre[len(reachability.VPN_OFF) + 3:] or "VPN switched off on purpose"
    except Exception:
        pass
    w = active_window(system)
    if w:
        return f"in maintenance until {w['ends_at'][:16]} ({w.get('reason', '')})"
    p = parked(system)
    if p:
        return f"parked since {p['parked_at'][:16]} ({p.get('reason', '')}) -- clear the flag on the wall"
    return None


# --------------------------------------------------------------------- gate

def _network_members(ledger_system: str) -> list[str]:
    """'network:Infrabeat VPN:stopped' -> the systems behind that network."""
    name = ledger_system[len("network:"):].split(":", 1)[0].strip()
    try:
        from core.config_loader import get_all_systems
        out = []
        for cfg in get_all_systems():
            net = cfg.get("network") or {}
            if isinstance(net, dict) and str(net.get("name") or "").strip() == name:
                out.append(cfg.get("name"))
        return [s for s in out if s]
    except Exception:
        return []


def _expand(system: str) -> list[str]:
    if system == "*":
        try:
            from core.config_loader import get_all_systems
            return [s.get("name") for s in get_all_systems() if s.get("name")] or ["*"]
        except Exception:
            return ["*"]
    if system.startswith("network:"):
        return _network_members(system) or [system]
    return [system]


def gate(system: str, kind: str, key=None, level: str | None = None) -> str | None:
    """
    None when an alert may go out; otherwise the reason it is held.
    Called by core/alert_ledger.should_send for every alert. Never raises.
    """
    try:
        if os.getenv("IBO_ALERT_CONTROL", "1").strip().lower() in ("0", "false", "no", "off"):
            return None
        members = _expand(system) if system.startswith("network:") else [system]
        # Maintenance: every system the alert is about must be in a window.
        windows_ = [active_window(m) for m in members]
        if members and all(windows_):
            w = max(windows_, key=lambda x: x["ends_at"])
            return f"in maintenance until {w['ends_at'][:16]} ({w.get('reason', '')})"
        if kind in RECOVERY_KINDS:
            return None
        if system in PLATFORM_SYSTEMS and kind in PLATFORM_KINDS:
            s = silenced(system, kind)
            return f"{s['mode']} by {s['created_by']}" if s else None
        if kind == "metric" and key:
            # one value: its own silence, or one for every value / every alert
            held = [_value_silence(m, key, level) or silenced(m, kind) for m in members]
        else:
            held = [silenced(m, kind) for m in members]
        if members and all(held):
            s = held[0]
            if s["mode"] == "snooze":
                return f"snoozed by {s['created_by']} until {str(s['until'])[:16]}"
            if s["mode"] == "ack":
                return f"acknowledged by {s['created_by']}"
            return (f"ignored until back to normal by {s['created_by']}" if is_value_kind(s["kind"])
                    else f"ignored until fixed by {s['created_by']}")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"alert gate failed open: {exc}")
    return None


# ----------------------------------------------------------------- summary

def status(system: str) -> dict:
    """What the wall needs for one card."""
    w = active_window(system)
    upcoming = None
    try:
        upcoming = ops_db.one(
            "SELECT * FROM maintenance WHERE (system=? OR system='*') AND cancelled_at IS NULL "
            "AND starts_at>? ORDER BY starts_at LIMIT 1", (system, ops_db.now()))
    except Exception:
        pass
    return {"maintenance": w, "next_maintenance": upcoming,
            "silences": active_silences(system), "parked": parked(system)}
