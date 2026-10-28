"""
Alerts per value, per system -- exactly what was chosen on the Thresholds page.

For every graded value (the wall's live read every few minutes, and every
sweep) this decides whether to tell someone, and how:

    alert     off       never (the default for every value)
              critical  when the value is CRITICAL
              warning   when it is WARNING or CRITICAL
    channels  email / whatsapp / sms / call  (ticked per value)

Both are set per value for all systems (config/thresholds.yaml) or for one
system (config/thresholds_systems.yaml), like the limits.

What is sent, per value (and per instance for per-instance values):
    new / level changed   at once, on every ticked channel except "call"
    still bad             again after REPEAT_MINUTES (critical 60, warning 480)
    critical 15 minutes,  phone call, once per incident (if "call" ticked)
      not acknowledged
    back to normal        a recovery message on the ticked mail / WhatsApp
Acknowledge / snooze / maintenance (Operations page, or the buttons in the
mail) hold everything for that system (core/alert_control.gate).

Every attempt -- sent, failed, held, channel not set up -- is written to the
table alert_sends (time, system, value, level, channel, receivers, result):
the Thresholds page shows the last one per value, the Alerts page all of them.

Nothing here reads SAP. Sending runs in a background thread, so a slow mail
server never holds up the live read.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__, "application")

LEVELS = ("off", "critical", "warning")
CHANNELS = ("email", "whatsapp", "sms", "call")
REPEAT_MINUTES = {"CRITICAL": 60, "WARNING": 480}
CALL_AFTER_MINUTES = 15
_lock = threading.Lock()


def _env_minutes(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


def repeat_minutes(level: str, system=None) -> int:
    """Reminder interval: the system's alert_timing (Systems page) > .env > default."""
    from core import alert_timing
    own = alert_timing.system_value(system, "repeat_critical_min" if level == "CRITICAL"
                                    else "repeat_warning_min") if system else None
    if own is not None:
        return max(1, own)
    if level == "CRITICAL":
        return _env_minutes("IBO_ALERT_REPEAT_CRITICAL_MIN", REPEAT_MINUTES["CRITICAL"])
    return _env_minutes("IBO_ALERT_REPEAT_WARNING_MIN", REPEAT_MINUTES["WARNING"])


def call_after_minutes() -> int:
    return _env_minutes("IBO_ALERT_CALL_AFTER_MIN", CALL_AFTER_MINUTES)


# ---------------------------------------------------------------- rules

def legacy_value_mails() -> bool:
    """
    The old combined mails ("SAP <system> CRITICAL ALERT" listing every
    critical value, and the daily-check mails) sent values nobody chose on
    the Thresholds page. Since 12 Oct 2026 values alert only through this
    engine, one mail per value, as chosen per value and system. Set
    IBO_LEGACY_VALUE_MAILS=1 to bring the old mails back.
    """
    return os.getenv("IBO_LEGACY_VALUE_MAILS", "0").strip().lower() in ("1", "true", "yes", "on")


def rule_for(system: str, key: str) -> dict:
    """{"alert": off|critical|warning, "channels": [...]} for one value on one system."""
    try:
        from core import thresholds as thr
        spec = thr.spec(key, system)
    except Exception:  # noqa: BLE001
        spec = {}
    raw = spec.get("alert")
    level = "off" if raw is False else str(raw or "off").strip().lower()   # unquoted off = False
    if level not in LEVELS:
        level = "off"
    chans = spec.get("channels")
    if not isinstance(chans, (list, tuple)):
        chans = ["email"] if level != "off" else []
    chans = [c for c in CHANNELS if c in [str(x).lower() for x in chans]]
    return {"alert": level, "channels": chans}


def _triggers(rule: dict, status: str) -> bool:
    if rule["alert"] == "critical":
        return status == "CRITICAL"
    if rule["alert"] == "warning":
        return status in ("WARNING", "CRITICAL")
    return False


# ---------------------------------------------------------------- recipients

def recipients(system_cfg: dict) -> dict:
    """
    Who gets a value alert for this system:
        {"email": {"to", "cc"}, "manager": [...], "whatsapp", "sms", "call"}.

    Owner's rule (Oct 2026): the first breach (YELLOW) goes to the Basis
    consultants only; the same value breached again (ORANGE / RED) adds the
    Basis manager. The CLIENT never gets a value alert -- the report
    receivers (alert_receivers / ALERT_TO_EMAILS) are NOT used here.
    Basis = core/alert_teams.basis_for: this system's own Alert-teams Basis
    team, else its Systems-page Basis box, else the standard Alert-teams Basis
    team, else on call / IBO_BASIS_EMAILS -- without any client address.
    """
    from core import recipients as rcp
    out = {"email": {"to": [], "cc": []}, "manager": [], "whatsapp": [], "sms": [], "call": []}
    to: list[str] = []
    try:
        from core import alert_teams
        to = list(alert_teams.basis_for(system_cfg)[0] or [])
    except Exception:  # noqa: BLE001
        to = []
    if not to:
        try:
            to = rcp.basis(system_cfg)
        except Exception:  # noqa: BLE001
            to = []
    to = rcp.strip_client(to, system_cfg)[0]
    out["email"] = {"to": to, "cc": []}
    try:
        out["manager"] = rcp.manager_internal(system_cfg)
    except Exception:  # noqa: BLE001
        out["manager"] = []
    phones = phone_numbers(system_cfg.get("name", ""))
    for ch in ("whatsapp", "sms", "call"):
        out[ch] = phones.get(ch) or []
    return out


# ---------------------------------------------------------------- grading
#
#   YELLOW   first breach of a value: Basis (and the value's teams) only
#   ORANGE   the same WARNING still breached after the repeat time, or a new
#            breach within IBO_ALERT_ESCALATE_WINDOW_MIN (default 120) of the
#            last one: Basis manager + Basis
#   RED      the same, at CRITICAL
YELLOW, ORANGE, RED = "YELLOW", "ORANGE", "RED"


def escalate_window_minutes(system=None) -> int:
    from core import alert_timing
    own = alert_timing.system_value(system, "escalate_window_min") if system else None
    return max(1, own) if own is not None else _env_minutes("IBO_ALERT_ESCALATE_WINDOW_MIN", 120)


def _escalated(level: str) -> str:
    return RED if level == "CRITICAL" else ORANGE


# ---------------------------------------------------------------- hysteresis
#
# A current-size value (SM58 entries displayed, a queue) wanders around its
# limit. Alerting on every crossing mailed WARNING -> NORMAL -> CRITICAL ->
# NORMAL every few minutes. Now, per value:
#   raised    after `raise_after` consecutive breaching readings (default 3)
#             or `raise_minutes` in breach (0 = off), whichever comes first
#   cleared   after `clear_after` consecutive readings at or below the clear
#             level, `clear_pct` % of the alerting limit (default 80 %)
#   "back to normal" mail only when the alert lasted `recovery_min_minutes`
#             (default 30); a shorter one is logged in Alert history only.
# Defaults come from .env (IBO_ALERT_RAISE_AFTER, IBO_ALERT_RAISE_MINUTES,
# IBO_ALERT_CLEAR_AFTER, IBO_ALERT_CLEAR_PCT, IBO_ALERT_RECOVERY_MIN_MINUTES);
# any of them can be set per value (and per system) in thresholds.yaml /
# thresholds_systems.yaml next to the limits. Values read only by a sweep
# (hours apart) and per-window event counts default to 1 / 1.
_EVENT_DEFAULTS = {"raise_after": 1, "clear_after": 1}
EVENT_KEYS = ("sap.st22.user_dumps_10min", "sap.sm37.user_cancelled_15min")


def _env_num(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


def hysteresis(system: str, key: str, source: str = "live") -> dict:
    out = {"raise_after": int(_env_num("IBO_ALERT_RAISE_AFTER", 3)) or 1,
           "raise_minutes": _env_num("IBO_ALERT_RAISE_MINUTES", 0),
           "clear_after": int(_env_num("IBO_ALERT_CLEAR_AFTER", 3)) or 1,
           "clear_pct": _env_num("IBO_ALERT_CLEAR_PCT", 80) or 80,
           "recovery_min_minutes": _env_num("IBO_ALERT_RECOVERY_MIN_MINUTES", 30)}
    try:                                    # the system's own timing (Systems page)
        from core import alert_timing
        for f in ("raise_after", "recovery_min_minutes"):
            own = alert_timing.system_value(system, f)
            if own is not None:
                out[f] = max(1, own) if f == "raise_after" else float(own)
    except Exception:  # noqa: BLE001
        pass
    if source == "sweep" or key in EVENT_KEYS:
        out.update(_EVENT_DEFAULTS)
    try:
        from core import thresholds as thr
        spec = thr.spec(key, system)
    except Exception:  # noqa: BLE001
        spec = {}
    for f in out:
        if f in spec and spec[f] is not None and not isinstance(spec[f], bool):
            try:
                out[f] = float(spec[f]) if f in ("raise_minutes", "clear_pct",
                                                 "recovery_min_minutes") else max(1, int(spec[f]))
            except (TypeError, ValueError):
                pass
    return out


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _below_clear(item: dict, rule: dict, hyst: dict) -> bool:
    """At or below the clear level (80 % of the alerting limit by default)."""
    status = str(item.get("status") or "").upper()
    if status != "NORMAL" and _triggers(rule, status):
        return False
    v = _num(item.get("value"))
    ref = _num(item.get("critical")) if rule["alert"] == "critical" else \
        (_num(item.get("warning")) if _num(item.get("warning")) is not None else _num(item.get("critical")))
    if v is None or ref is None or item.get("direction") == "state":
        return status == "NORMAL"
    pct = float(hyst.get("clear_pct") or 80) / 100.0
    if item.get("direction") == "low":
        return v >= (ref / pct if pct else ref)
    return v <= ref * pct


def _recipients_path() -> str:
    try:
        from core.config_loader import CONFIG_DIR
    except Exception:  # noqa: BLE001
        CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
    return os.path.join(CONFIG_DIR, "alert_recipients.yaml")


def phone_numbers(system: str) -> dict:
    """WhatsApp / SMS / call numbers for a system (config/alert_recipients.yaml), else `default:`."""
    try:
        import yaml
        with open(_recipients_path(), "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"alert_recipients.yaml unreadable: {exc}")
        return {}
    systems = data.get("systems") or {}
    own = systems.get(system) or {}
    dflt = data.get("default") or {}
    out = {}
    for ch in ("whatsapp", "sms", "call"):
        vals = own.get(ch) if own.get(ch) else dflt.get(ch)
        out[ch] = [str(v).strip() for v in (vals or []) if str(v).strip()]
    return out


def save_phone_numbers(system: str, numbers: dict, by: str = "") -> dict:
    """Write one system's WhatsApp / SMS / call numbers (validated: + and digits)."""
    import re
    import yaml
    from core import timeline
    from core.config_editor import write_text_safely
    clean = {}
    for ch in ("whatsapp", "sms", "call"):
        raw = numbers.get(ch) or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,;\n]+", raw) if x.strip()]
        vals = []
        for n in raw:
            n = str(n).strip().replace(" ", "").replace("-", "")
            if not re.fullmatch(r"\+?\d{8,15}", n):
                raise ValueError(f"{ch}: {n!r} is not a phone number (use +91XXXXXXXXXX).")
            vals.append(n)
        clean[ch] = vals
    try:
        with open(_recipients_path(), "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        data = {}
    data.setdefault("systems", {})[system] = clean
    text = ("# WhatsApp / SMS / phone-call numbers for alerts, per system (Thresholds page).\n"
            "# `default:` is used for a system with none of its own.\n"
            + yaml.safe_dump(data, sort_keys=True, default_flow_style=False))

    def check(parsed):
        if ((parsed or {}).get("systems") or {}).get(system) != clean:
            raise RuntimeError("write check failed")
    write_text_safely("alert_recipients.yaml", text, check)
    timeline.add(system, "config", "Alert phone numbers changed",
                 ", ".join(f"{k}: {len(v)}" for k, v in clean.items()), actor=by or timeline.actor_name())
    return clean


def channel_status() -> dict:
    """Whether each channel can actually send, and why not."""
    from notifications import mailer
    out = {"email": {"ready": mailer.email_enabled(),
                     "note": "" if mailer.email_enabled() else "mail is switched off (IBO_ENABLE_EMAIL)"}}
    try:
        from notifications import whatsapp
        ready, note = whatsapp.value_alert_state()
    except Exception as exc:  # noqa: BLE001
        ready, note = False, f"WhatsApp module not loaded ({type(exc).__name__})"
    out["whatsapp"] = {"ready": ready, "note": note}
    out["sms"] = {"ready": False, "note": "not set up yet (next stage: Exotel account)"}
    out["call"] = {"ready": False, "note": "not set up yet (next stage: Exotel account)"}
    return out


# ---------------------------------------------------------------- state

def _now() -> datetime:
    return datetime.now()


def _fmt(d: datetime | None) -> str | None:
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else None


def _state(system: str, skey: str) -> dict | None:
    from core import ops_db
    return ops_db.one("SELECT * FROM alert_state WHERE system=? AND skey=?", (system, skey))


_STATE_COLS = ("status", "since", "last_sent_at", "last_sent_level", "escalated_at", "delivered",
               "value", "teams", "breach_n", "breach_since", "clear_n", "raised", "raised_at",
               "grade", "last_raised_at")


def _set_state(system: str, skey: str, **fields) -> None:
    from core import ops_db
    cur = _state(system, skey) or {"status": "", "since": _fmt(_now()), "last_sent_at": None,
                                   "last_sent_level": None, "escalated_at": None, "delivered": 0,
                                   "value": "", "teams": "", "breach_n": 0, "breach_since": None,
                                   "clear_n": 0, "raised": 0, "raised_at": None, "grade": "",
                                   "last_raised_at": None}
    cur.update(fields)
    for k in ("delivered", "breach_n", "clear_n", "raised"):
        cur[k] = int(cur.get(k) or 0)
    for k in ("value", "teams", "grade"):
        cur[k] = str(cur.get(k) or "")
    cols = ", ".join(_STATE_COLS)
    marks = ",".join("?" * (len(_STATE_COLS) + 2))
    upd = ", ".join(f"{c}=excluded.{c}" for c in _STATE_COLS)
    ops_db.execute(
        f"INSERT INTO alert_state(system, skey, {cols}) VALUES ({marks}) "
        f"ON CONFLICT(system, skey) DO UPDATE SET {upd}",
        (system, skey, *[cur.get(c) for c in _STATE_COLS]))


def _drop_state(system: str, skey: str) -> None:
    from core import ops_db
    ops_db.execute("DELETE FROM alert_state WHERE system=? AND skey=?", (system, skey))


def record(system, metric, instance, label, level, value, limits, kind, channel, recipients_text,
           ok, note, batch: str = "") -> None:
    from core import ops_db
    try:
        ops_db.execute(
            "INSERT INTO alert_sends(at, system, metric, instance, label, level, value, limits, kind, "
            "channel, recipients, ok, note, batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ops_db.now(), system, metric, instance or "", label or "", level, str(value), limits or "",
             kind, channel, recipients_text or "", 1 if ok else 0, str(note or "")[:400], batch or ""))
    except Exception as exc:  # noqa: BLE001
        log.warning(f"alert history not written: {exc}")


# ---------------------------------------------------------------- observe

def _limits_text(item: dict) -> str:
    if item.get("direction") == "state":
        return f"not running = {item.get('severity') or 'critical'}"
    w, c = item.get("warning"), item.get("critical")
    lo = item.get("direction") == "low"
    parts = []
    if w is not None:
        parts.append(f"warning {'≤' if lo else '≥'} {w}")
    if c is not None:
        parts.append(f"critical {'≤' if lo else '≥'} {c}")
    return ", ".join(parts)


def _value_text(item: dict) -> str:
    v = item.get("value")
    unit = (item.get("unit") or "").strip()
    if v is None:
        return "—"
    if isinstance(v, float):
        v = round(v) if abs(v) >= 10 else round(v, 1)
    text = str(v).replace(" count", "")
    return f"{text} {unit}".strip() if unit and unit not in text else text


# A value the live read reports is decided by the live read only: the sweep
# reads some of the same values from other sources (CPU over SSH, SMLG via
# SAP GUI), and two sources taking turns would alternate alert / recovery.
LIVE_OWNS_MINUTES = 20
_live_seen: dict = {}


def _mark_live(system: str, key: str, now) -> None:
    """Remember (in the database, so a sweep run as its own process sees it
    too) that the live read reported this value. Written at most once a minute."""
    last = _live_seen.get((system, key))
    if last and now - last < timedelta(minutes=1):
        return
    _live_seen[(system, key)] = now
    try:
        from core import ops_db
        ops_db.execute("INSERT INTO alert_live_seen(system, key, at) VALUES (?,?,?) "
                       "ON CONFLICT(system, key) DO UPDATE SET at=excluded.at",
                       (system, key, _fmt(now)))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"alert live mark failed: {exc}")


def _live_owns(system: str, key: str, now) -> bool:
    seen = _live_seen.get((system, key))
    if not (seen and now - seen < timedelta(minutes=LIVE_OWNS_MINUTES)):
        try:
            from core import ops_db
            row = ops_db.one("SELECT at FROM alert_live_seen WHERE system=? AND key=?", (system, key))
            seen = _parse(row["at"]) if row else None
        except Exception:  # noqa: BLE001
            seen = None
    return bool(seen and now - seen < timedelta(minutes=LIVE_OWNS_MINUTES))


def observe(system_cfg: dict, items: list[dict], source: str = "live", sender=None) -> list[dict]:
    """
    Feed one reading of a system: a list of graded values
    ({"key", "label", "value", "status", "warning", "critical", "direction",
    "unit", "instance"?, "stale"?}). Returns the sends it started (for tests).
    `sender` replaces the real channel senders (tests).
    """
    system = str(system_cfg.get("name") or "")
    if not system:
        return []
    started = []
    now = _now()
    with _lock:
        for it in items or []:
            key = it.get("key")
            if not key:
                continue
            if source == "live":
                _mark_live(system, key, now)
            elif _live_owns(system, key, now):
                continue                   # the live read decides this value
            status = str(it.get("status") or "").upper()
            if it.get("stale") or status not in ("NORMAL", "WARNING", "CRITICAL"):
                continue                   # unknown / not read: neither alert nor recovery
            rule = rule_for(system, key)
            inst = it.get("instance") or ""
            skey = key + (f"@{inst}" if inst else "")
            prev = _state(system, skey)
            vtext = _value_text(it)
            if rule["alert"] == "off":
                if prev is not None:
                    _clear_value_silences(system, skey)
                    _drop_state(system, skey)          # switched off: forget it, no recovery
                continue
            hyst = hysteresis(system, key, source)
            raised = bool(prev and int(prev.get("raised") or 0))
            if prev is not None and prev.get("last_sent_level") and not prev.get("raised_at"):
                raised = True                          # a row from before the hysteresis columns
            if _triggers(rule, status):
                n = int((prev or {}).get("breach_n") or 0) + 1
                first = _parse((prev or {}).get("breach_since")) if n > 1 else None
                first = first or now
                kind = None
                grade = str((prev or {}).get("grade") or "") or YELLOW
                if not raised:
                    due = n >= int(hyst["raise_after"]) or (
                        hyst["raise_minutes"] and now - first >= timedelta(minutes=hyst["raise_minutes"]))
                    if not due:
                        # Breaching, but not for long enough yet: no mail, no recovery later.
                        _set_state(system, skey, status=status, value=vtext, breach_n=n,
                                   breach_since=_fmt(first), clear_n=0)
                        continue
                    kind = "alert"
                    last_raise = _parse((prev or {}).get("last_raised_at"))
                    grade = (_escalated(status) if last_raise and now - last_raise
                             < timedelta(minutes=escalate_window_minutes(system)) else YELLOW)
                    _set_state(system, skey, status=status, since=_fmt(now), value=vtext,
                               escalated_at=None, delivered=0, breach_n=n, breach_since=_fmt(first),
                               clear_n=0, raised=1, raised_at=_fmt(now), last_raised_at=_fmt(now),
                               grade=grade, last_sent_level=None, last_sent_at=None)
                elif prev.get("last_sent_level") is None:
                    kind = "alert"                              # raised, the mail was held
                elif prev.get("last_sent_level") != status:
                    kind = "alert"                              # level changed
                    if grade == ORANGE and status == "CRITICAL":
                        grade = RED
                    if status == "CRITICAL":
                        # the 15 minutes before a call count from CRITICAL, not
                        # from the warning before it; a call already made for
                        # this incident is not made again
                        _set_state(system, skey, since=_fmt(now))
                else:
                    last = _parse(prev.get("last_sent_at"))
                    if last is None or now - last >= timedelta(minutes=repeat_minutes(status, system)):
                        kind = "repeat"
                        # still breached after the repeat time: the manager is told
                        grade = _escalated(status)
                chans = [c for c in rule["channels"] if c != "call"]
                if kind:
                    delivered = (prev or {}).get("delivered") or 0
                    it2 = {**it, "grade": grade}
                    if chans:
                        job = _dispatch(system_cfg, it2, status, kind, chans, sender)
                        started.append(job)
                        delivered = 0 if job.get("held") else 1
                    _set_state(system, skey, status=status, last_sent_at=_fmt(now),
                               last_sent_level=status, value=vtext, breach_n=n, clear_n=0,
                               grade=grade,
                               delivered=1 if (delivered or (prev or {}).get("delivered")) else 0,
                               # the recovery goes to whoever got the alert
                               teams=",".join(it.get("teams") or []) or (prev or {}).get("teams") or "")
                else:
                    _set_state(system, skey, status=status, value=vtext, breach_n=n, clear_n=0)
                # Phone call: CRITICAL for N minutes, once per incident.
                prev = _state(system, skey)
                since = _parse(prev.get("since")) if prev else None
                if (status == "CRITICAL" and "call" in rule["channels"] and prev
                        and not prev.get("escalated_at") and since
                        and now - since >= timedelta(minutes=call_after_minutes())):
                    job = _dispatch(system_cfg, {**it, "grade": prev.get("grade") or grade},
                                    status, "call", ["call"], sender)
                    started.append(job)
                    _set_state(system, skey, escalated_at=_fmt(now),
                               delivered=1 if (not job.get("held") or prev.get("delivered")) else 0)
            elif prev is not None:
                if not raised:
                    # A breach that never lasted long enough: start counting again.
                    last_raise = _parse(prev.get("last_raised_at"))
                    if last_raise and now - last_raise < timedelta(minutes=escalate_window_minutes(system)):
                        _set_state(system, skey, status=status, value=vtext, breach_n=0, clear_n=0,
                                   breach_since=None)
                    else:
                        _drop_state(system, skey)
                    continue
                if not _below_clear(it, rule, hyst):
                    # Between the clear level and the limit: still raised, nothing sent.
                    _set_state(system, skey, value=vtext, breach_n=0, clear_n=0)
                    continue
                cn = int(prev.get("clear_n") or 0) + 1
                if cn < int(hyst["clear_after"]):
                    _set_state(system, skey, value=vtext, breach_n=0, clear_n=cn)
                    continue
                _clear_value_silences(system, skey)
                chans = [c for c in rule["channels"] if c in ("email", "whatsapp")]
                raised_at = _parse(prev.get("raised_at")) or _parse(prev.get("since")) or now
                lasted = (now - raised_at).total_seconds() / 60
                # A recovery only follows an alert that actually went out, and
                # only one that lasted long enough to matter.
                if prev.get("delivered") and prev.get("last_sent_level") and chans:
                    if lasted >= float(hyst["recovery_min_minutes"]):
                        it2 = dict(it, grade=prev.get("grade") or YELLOW)
                        if prev.get("teams"):
                            it2["teams"] = [t for t in prev["teams"].split(",") if t]
                        started.append(_dispatch(system_cfg, it2, "NORMAL", "recovery", chans, sender))
                    else:
                        record(system, key, inst, it.get("label") or key, "NORMAL", vtext,
                               _limits_text(it), "recovery", "none", "", False,
                               f"back to normal after {lasted:.0f} min: no recovery mail for an alert "
                               f"shorter than {hyst['recovery_min_minutes']:.0f} min")
                # Keep the row (not raised) so a new breach within the window
                # goes to the manager; otherwise forget it.
                _set_state(system, skey, status="NORMAL", value=vtext, breach_n=0, clear_n=0,
                           breach_since=None, raised=0, raised_at=None, last_sent_level=None,
                           last_sent_at=None, escalated_at=None, delivered=0, grade="")
    return started


def _clear_value_silences(system: str, skey: str) -> None:
    """The value is back to normal: its Acknowledge / Ignore (mail buttons) end; snoozes run out by time."""
    try:
        from core import alert_control
        alert_control.clear_until_fixed(system, {f"metric:{skey}"} | (
            {f"metric:{skey.split('@', 1)[0]}"} if "@" not in skey else set()),
            by="auto: value back to normal")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"{system}: value silences not cleared: {exc}")


def _parse(stamp):
    if not stamp:
        return None
    try:
        return datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def items_from_metrics(metrics) -> list[dict]:
    """A sweep's MetricResults as observe() items."""
    from core import metric_catalogue as cat
    out = []
    aliases = set(cat.ALIASES.values())       # one figure, two names: alert once
    for m in metrics or []:
        if m.name in aliases:
            continue
        st = getattr(getattr(m, "status", None), "value", None) or str(getattr(m, "status", ""))
        e = cat.CATALOGUE.get(m.name) or {}
        out.append({"key": m.name, "label": e.get("label") or m.name,
                    "value": m.value if m.value is not None else m.display_value,
                    "status": str(st).upper(), "warning": m.threshold_warning,
                    "critical": m.threshold_critical, "direction": e.get("direction", "high"),
                    "unit": e.get("unit", "")})
    return out


# ---------------------------------------------------------------- sending

DUMP_KEYS = ("sap.st22.dumps", "sap.st22.user_dumps_10min")


def _dispatch(system_cfg, item, level, kind, channels, sender=None) -> dict:
    system = system_cfg.get("name", "")
    job = {"system": system, "metric": item.get("key"), "instance": item.get("instance") or "",
           "label": item.get("label") or item.get("key"), "level": level, "kind": kind,
           "value": _value_text(item), "limits": _limits_text(item), "channels": list(channels)}
    # Custom alerts (core/custom_alerts.py): who it goes to, what it is about,
    # and the "mail the user" approval for cancelled jobs.
    for k in ("teams", "detail", "approval_id", "grade"):
        if item.get(k):
            job[k] = item[k]
    job.setdefault("grade", YELLOW)
    skey = str(item.get("key") or "") + (f"@{job['instance']}" if job["instance"] else "")
    job["skey"] = skey
    try:
        from core.alert_control import gate
        # A recovery is held only by maintenance (like the other recoveries),
        # not by an acknowledge or snooze of the alert it closes. A value's
        # own snooze / acknowledge (the mail buttons) holds only that value.
        held = gate(system, "recovered" if kind == "recovery" else "metric", skey, level=level)
    except Exception:  # noqa: BLE001
        held = None
    import uuid
    job["batch"] = uuid.uuid4().hex[:12]
    if held:
        for ch in channels:
            record(system, job["metric"], job["instance"], job["label"], level, job["value"],
                   job["limits"], kind, ch, "", False, f"held: {held}", job["batch"])
        job["held"] = held
        return job
    if sender is not None:
        _add_breakdown(job)
        sender(system_cfg, job)
        return job
    threading.Thread(target=_send_all, args=(system_cfg, job), daemon=True,
                     name=f"alert-{system}").start()
    return job


def _add_breakdown(job: dict) -> None:
    """
    ABAP dump alerts: who and when (core/custom_alerts.dump_breakdown). Run in
    the sender thread, outside the engine lock, and only for a mail that goes out.
    """
    if job.get("kind") == "recovery" or job.get("metric") not in DUMP_KEYS or "breakdown" in job:
        return
    try:
        from core import custom_alerts
        rows = custom_alerts.dump_breakdown(job["system"], user=job.get("instance") or "" if
                                            job["metric"] == "sap.st22.user_dumps_10min" else "",
                                            now=_now())
        if rows:
            job["breakdown"] = rows
    except Exception as exc:  # noqa: BLE001
        log.info(f"{job.get('system')}: dump breakdown skipped: {type(exc).__name__}: {exc}")


def mail_recipients(system_cfg: dict, job: dict) -> dict:
    """
    {"to", "cc"} for one value alert by its grade: YELLOW -> Basis (and the
    value's teams); ORANGE / RED -> Basis in To, the Basis manager in Cc.
    The client is removed in every case.
    """
    from core import recipients as rcp_mod
    if job.get("kind") == "recovery":
        own = rcp_mod.special(system_cfg, "recovery")
        if own:
            return {"to": own, "cc": []}
    rcp = recipients(system_cfg)
    to = list(rcp["email"]["to"])
    if job.get("teams"):
        from core import alert_teams
        to, notes = alert_teams.resolve(system_cfg, job["teams"])
        job["team_notes"] = notes
    cc: list[str] = []
    if job.get("grade") in (ORANGE, RED) and job.get("kind") != "test":
        mgr = list(rcp.get("manager") or [])
        if mgr:
            # Owner's rule (27.10.2026): Basis stays in To (they act), the
            # manager is told in Cc.
            low = {a.lower() for a in to}
            cc = [m for m in mgr if m.lower() not in low]
        else:
            job.setdefault("team_notes", []).append("no Basis manager address configured "
                                                    "(ALERT_MANAGER_EMAILS / manager_receivers)")
    to, removed1 = rcp_mod.strip_client(to, system_cfg)
    cc, removed2 = rcp_mod.strip_client(cc, system_cfg)
    if not to and cc:
        to, cc = cc[:1], cc[1:]
    if removed1 or removed2:
        log.warning(f"{system_cfg.get('name')}: client address(es) left out of a value alert: "
                    f"{removed1 + removed2}")
    return {"to": to, "cc": cc}


def _send_all(system_cfg: dict, job: dict) -> None:
    import uuid
    job.setdefault("batch", uuid.uuid4().hex[:12])
    job.setdefault("grade", YELLOW)
    _add_breakdown(job)
    rcp = recipients(system_cfg)
    rcp["email"] = mail_recipients(system_cfg, job)
    status = channel_status()
    for ch in job["channels"]:
        try:
            if ch == "email":
                held = _gate_email(job)
                if held:
                    record(job["system"], job["metric"], job["instance"], job["label"], job["level"],
                           job["value"], job["limits"], job["kind"], ch,
                           ", ".join(rcp["email"]["to"] + rcp["email"]["cc"]), False, held,
                           job.get("batch", ""))
                    continue
                ok, who, note = _send_email(system_cfg, job, rcp["email"])
                _ledger_done(job, ok, who, note)
                copies = job.pop("_copies", None) or []
                if len(copies) > 1:
                    # Two copies (ours with buttons, the other without): one
                    # gate decision, one row per copy so a failed copy is plain.
                    for name, c_who, c_ok, c_note in copies:
                        record(job["system"], job["metric"], job["instance"], job["label"], job["level"],
                               job["value"], job["limits"], job["kind"], ch, c_who, c_ok,
                               f"{name}: " + ("sent" if c_ok else f"NOT sent ({c_note})"),
                               job.get("batch", ""))
                    _timeline(job, ch, ok, who, note)
                    continue
            elif ch == "whatsapp":
                from notifications import whatsapp
                ok, who, note = whatsapp.send_value_alert(job, rcp.get("whatsapp") or [])
            else:
                who = ", ".join(rcp.get(ch) or [])
                ok, note = False, status.get(ch, {}).get("note") or "not set up"
            if ok and ch == "email" and job.get("grade") in (ORANGE, RED) and job["kind"] != "recovery":
                note = f"{job['grade']}: Basis manager informed"
            record(job["system"], job["metric"], job["instance"], job["label"], job["level"],
                   job["value"], job["limits"], job["kind"], ch, who, ok, note, job.get("batch", ""))
            _timeline(job, ch, ok, who, note)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"{job['system']}: alert {ch} failed: {type(exc).__name__}: {exc}")
            record(job["system"], job["metric"], job["instance"], job["label"], job["level"],
                   job["value"], job["limits"], job["kind"], ch, "", False, f"{type(exc).__name__}: {exc}",
                   job.get("batch", ""))


def _timeline(job: dict, ch: str, ok: bool, who: str, note: str) -> None:
    try:
        from core import timeline
        timeline.add(job["system"], "alert",
                     f"{job['level']} {job['label']}: {ch} {'sent' if ok else 'NOT sent'}",
                     f"{job['value']} ({job['limits']}) -> {who or 'nobody'}"
                     + (f"; {note}" if note and not ok else ""),
                     severity="critical" if job["level"] == "CRITICAL" else
                     "warning" if job["level"] == "WARNING" else "ok")
    except Exception:  # noqa: BLE001
        pass


def _ledger_key(job: dict) -> str:
    return f"{job.get('skey') or job.get('metric')}|{job.get('level')}|{job.get('grade')}|{job.get('kind')}"


def _gate_email(job: dict) -> str:
    """
    The delivery gate (core/alert_ledger.should_send) for one value mail:
    policy, mail outage and the per-system / total daily caps. The
    acknowledge / snooze / maintenance hold and the repeat timing were
    already decided by observe() and _dispatch(). '' = may go out.
    """
    if job.get("kind") == "test":
        return ""
    try:
        from core import alert_ledger
        ok, why = alert_ledger.should_send(job["system"], "metric", _ledger_key(job), 0, 0,
                                           severity=job.get("level", ""), mirror=False,
                                           check_silences=False, closing=job.get("kind") == "recovery")
        return "" if ok else why
    except Exception as exc:  # noqa: BLE001 -- the gate fails open
        log.debug(f"delivery gate unavailable: {exc}")
        return ""


def _ledger_done(job: dict, ok: bool, who: str, note: str) -> None:
    if job.get("kind") == "test":
        return
    try:
        from core import alert_ledger
        alert_ledger.record(job["system"], "metric", _ledger_key(job), job.get("level", ""),
                            f"{job.get('level')} {job.get('label')}", who, delivered=ok,
                            reason=note, mirror=False, hooks=False)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"ledger record failed: {exc}")


def _send_email(system_cfg: dict, job: dict, rcp: dict) -> tuple[bool, str, str]:
    """
    One alert mail. With buttons (Acknowledge / Snooze / Ignore, core/action_links.py)
    only for our own addresses (IBO_INTERNAL_DOMAINS / IBO_INTERNAL_ADDRESSES):
    when the receivers mix ours and others (a customer, a private mailbox),
    ours get a copy with the buttons and the others the same mail without them.
    """
    from notifications import mailer
    from core import action_links
    to, cc = rcp.get("to") or [], rcp.get("cc") or []
    who = ", ".join(to + cc)
    if not to:
        return False, who, "no mail recipients for this system"
    if not mailer.email_enabled():
        return False, who, "mail is switched off (IBO_ENABLE_EMAIL)"
    from core.config_loader import get_smtp_config
    smtp = get_smtp_config(system_cfg, require_recipients=False)
    subject, html, text = render_email(job)
    recovered = job["kind"] == "recovery"
    everyone = to + cc
    if _domains_known():
        ours, others = action_links.split_internal(everyone)
    elif action_links.internal_only(everyone):        # IBO_MAIL_BUTTONS_ALWAYS=1
        ours, others = list(everyone), []
    else:
        ours, others = [], list(everyone)
    if ours and others and not action_links.public_url():
        ours, others = [], list(everyone)             # no buttons to give: one mail, as before
    sends = []
    if ours:
        o_to = [a for a in to if a in ours]
        o_cc = [a for a in cc if a in ours]
        if not o_to:
            o_to, o_cc = o_cc[:1], o_cc[1:]
        o_html = html
        if others:
            note = (f"<p style='font:12px Segoe UI,Arial,sans-serif;color:#64748b'>Also sent, without the "
                    f"buttons, to: {_esc(', '.join(others))}</p>")
            o_html = _put(o_html, note)
        if job.get("approval_id") and job["kind"] in ("alert", "repeat"):
            try:
                from core import job_user_mail
                block = job_user_mail.button_html(int(job["approval_id"]), o_to + o_cc)
                o_html = _put(o_html, block)
            except Exception as exc:  # noqa: BLE001
                log.info(f"approval button skipped: {exc}")
        footer = action_links.footer_html(job["system"], f"metric:{job.get('skey') or job['metric']}",
                                          recovered=recovered, label=job["label"], level=job["level"])
        if footer:
            o_html = _put(o_html, "<!-- ibo-actions -->" + footer)
        sends.append(("our copy", o_to, o_cc, o_html))
    if others:
        x_to = [a for a in to if a in others]
        x_cc = [a for a in cc if a in others]
        if not x_to:
            x_to, x_cc = x_cc[:1], x_cc[1:]
        x_html = html
        if job.get("approval_id") and job["kind"] in ("alert", "repeat") and not ours:
            try:
                from core import job_user_mail
                block = job_user_mail.button_html(int(job["approval_id"]), x_to + x_cc)
                x_html = _put(x_html, block)
            except Exception as exc:  # noqa: BLE001
                log.info(f"approval button skipped: {exc}")
        sends.append(("copy without buttons", x_to, x_cc, x_html))
    # Both copies were cleared by ONE gate decision (_gate_email, taken before
    # this function). Each copy is sent once; the outcome of each is kept for
    # Alert history, so "one copy went out, the other did not" is never hidden.
    failed, copies = [], []
    for name, s_to, s_cc, s_html in sends:
        extra = {}
        if ours and name == "copy without buttons":
            # a reply from the customer reaches our team (and the sending mailbox)
            extra["reply_to"] = list(dict.fromkeys(([smtp.get("from_email")] if smtp.get("from_email") else [])
                                                   + ours))
        try:
            ok = bool(mailer.send_html_now(subject, s_html, s_to, smtp, cc=s_cc, text_fallback=text, **extra))
            why = "" if ok else (mailer.last_reason() or "the mail server refused or did not answer")
        except Exception as exc:  # noqa: BLE001
            ok, why = False, f"{type(exc).__name__}: {exc}"
        copies.append((name, ", ".join(s_to + s_cc), ok, why))
        if not ok:
            failed.append((name, why))
    job["_copies"] = copies
    if not failed:
        return True, who, ""
    if len(failed) == len(sends):
        return False, who, (f"the mail server refused or did not answer: {failed[0][1]} "
                            f"(see logs/email.log)")
    return False, who, (f"{failed[0][0]} not sent ({failed[0][1]}; see logs/email.log); "
                        f"the other copy went out")


def _domains_known() -> bool:
    from core import action_links
    return bool(action_links._domains() or action_links._addresses())


def _esc(text: str) -> str:
    from html import escape
    return escape(str(text or ""))


# Values whose instance is a SAP user: the subject says "user X" so a per-user
# alert never looks like the system-wide one (e.g. ABAP dumps today).
USER_KEYS = {"sap.st22.user_dumps_10min", "sap.sm37.user_cancelled_15min", "sap.sm12.user_old_locks"}


def where_text(job: dict) -> str:
    inst = job.get("instance") or ""
    if not inst or job.get("metric") in USER_KEYS:
        return ""                                      # the user is in the label (shown_label)
    return f" ({inst})"


def shown_label(job: dict) -> str:
    """'ABAP dumps by one user (15 min)' for user EY_SD -> 'ABAP dumps by user EY_SD (15 min)'."""
    label, inst = str(job.get("label") or job.get("metric") or ""), job.get("instance") or ""
    if not inst or job.get("metric") not in USER_KEYS:
        return label
    return label.replace("by one user", f"by user {inst}") if "by one user" in label else f"{label} -- user {inst}"


_MEANING = [
    (("dump",), "Program crashes (ST22). A rise usually means a program, transport or system problem that "
                "users notice."),
    (("same table",), "Users waiting for each other on the same locked table (SM12)."),
    (("held lock", "lock age", "oldest lock"), "How long the oldest lock has been held (SM12). An old lock blocks "
                                               "everyone who needs the same record."),
    (("trfc", "qrfc", "rfc queue", "rfc"), "Calls between systems that failed or are stuck (SM58 / SMQ1 / SMQ2). "
                                           "Data may not reach the other system."),
    (("cancelled job", "job"), "Background jobs (SM37) that failed or ran too long."),
    (("response",), "How long users wait for each screen (ST03 / SMLG)."),
    (("work process", "free wp", "dialog wp"), "The server processes that do users' work (SM50). When they are "
                                               "all busy, users queue."),
    (("update",), "Saves that failed (SM13): the user's data may not have been saved."),
    (("long-running", "longest-running", "running for"), "Work that has been running for a long time (SM66)."),
    (("spool",), "Print requests (SP01)."),
    (("cpu",), "How busy the server's processors are."),
    (("memory", "swap", "paging"), "Memory use on the server."),
    (("disk", "file system", "filesystem", "free space"), "Free disk space on the server. When it runs out, "
                                                          "SAP stops."),
    (("certificate", "pse"), "Security certificates (STRUST). When one expires, the connections that use it "
                             "stop working."),
    (("users", "logged on"), "Number of logged-on users."),
]


def _meaning(label: str) -> str:
    low = str(label or "").lower()
    for keys, text in _MEANING:
        if any(k in low for k in keys):
            return text
    return ""


def tidy_detail(detail: str) -> str:
    """'IB_KULDEEP, IB_KULDEEP, IB_KULDEEP, 65675, 65675' -> 'IB_KULDEEP (3), 65675 (2)'."""
    import re
    text = str(detail or "").strip()
    head, sep, tail = text.partition(" -- ")
    parts = [p.strip() for p in head.split(",")]
    if len(parts) < 3:
        return text
    counts: dict[str, int] = {}
    for p in parts:
        if p:
            counts[p] = counts.get(p, 0) + 1
    if len(counts) == len(parts):
        return text
    nice = ", ".join(f"{k} ({n})" if n > 1 else k for k, n in counts.items())
    return nice + (sep + tail if sep else "")


def _limit_numbers(limits: str) -> tuple[str, str, bool, bool]:
    """('5', '10', lower_is_worse, is_state) from 'warning ≥ 5, critical ≥ 10'."""
    import re
    t = str(limits or "")
    w = re.search(r"warning\s*[≥≤>=<]+\s*([^,]+)", t)
    c = re.search(r"critical\s*[≥≤>=<]+\s*([^,]+)", t)
    return ((w.group(1).strip() if w else ""), (c.group(1).strip() if c else ""),
            "≤" in t or "<=" in t, "not running" in t)


def _put(html: str, block: str) -> str:
    from notifications.mail_style import insert
    return insert(html, block)


def render_email(job: dict) -> tuple[str, str, str]:
    """A value alert / reminder / back-to-normal mail, in plain words (notifications/mail_style)."""
    from notifications import mail_style as ms
    lvl = job["level"]
    where = where_text(job)
    job = {**job, "label": shown_label(job)}
    name = f"{job['label']}{where}"
    sysn = job["system"]
    value = str(job.get("value", ""))
    w, c, low, state = _limit_numbers(job.get("limits") or "")
    recovered = job["kind"] == "recovery"
    if recovered:
        subject = f"[InfraBeatOps] RECOVERED {sysn}: {name} now {value}"
        level, title = "OK", f"Back to normal: {name}"
        lead = (f"<b>{ms.esc(name)}</b> on <b>{ms.esc(sysn)}</b> is back to <b>{ms.esc(value)}</b>"
                + (f", inside its limit ({'warning at ' + ms.esc(w)})" if w else "")
                + ". <b>Nothing to do</b> -- this mail closes the earlier alert.")
    else:
        tag = {"repeat": " (still)", "call": ""}.get(job["kind"], "")
        esc_ = f"{job['grade']} · " if job.get("grade") in (ORANGE, RED) else ""
        subject = f"[InfraBeatOps] {esc_}{lvl}{tag} {sysn}: {name} = {value}"
        level = lvl
        if state:
            title = f"{name} is not running"
            lead = f"<b>{ms.esc(name)}</b> on <b>{ms.esc(sysn)}</b> is <b>not running</b> ({ms.esc(value)})."
        else:
            title = f"{name} is too {'low' if low else 'high'}: {value}"
            over = "under" if low else "over"
            lead = f"<b>{ms.esc(name)}</b> on <b>{ms.esc(sysn)}</b> is now <b>{ms.esc(value)}</b>. "
            if lvl == "CRITICAL" and c:
                lead += f"That is {over} the <b>critical</b> limit of {ms.esc(c)}."
            elif w:
                lead += (f"That is {over} the warning limit of {ms.esc(w)}"
                         + (f"; it becomes critical at {ms.esc(c)}." if c else "."))
        if job["kind"] == "repeat":
            lead += " <b>Reminder:</b> it is still not back to normal."
        grade = job.get("grade")
        if grade == YELLOW:
            lead += " This is the first time, so only the Basis team got this mail."
        elif grade in (ORANGE, RED):
            lead += (" It stayed bad or went bad again soon after recovering, so the <b>Basis manager</b> got "
                     "this mail too.")
    blocks = []
    meaning = _meaning(job["label"])
    detail = tidy_detail(job.get("detail") or "")
    rows = []
    if recovered:
        # Back to normal: short -- one sentence, when, nothing to act on.
        lead += f" <span style='color:#64748b'>Checked {ms.esc(ms.ist())}.</span>"
        foot = ""
    else:
        if meaning:
            blocks.append(ms.section("What this value is", f"<div style='font:14px/1.5 {ms.FONT};color:#334155'>"
                                                           f"{ms.esc(meaning)}</div>"))
        rows = [("Now", value)]
        if job.get("limits"):
            rows.append(("Limits", job["limits"]))
        if detail:
            rows.append(("What IBOPS saw", detail))
        for k, v in job.get("breakdown") or []:
            rows.append((k, v))
        if job.get("teams"):
            rows.append(("Teams told", ", ".join(job["teams"]) + (f" ({'; '.join(job['team_notes'])})"
                                                                  if job.get("team_notes") else "")))
        rows.append(("Checked at", ms.ist()))
        blocks.append(ms.section("Details", ms.facts(rows)))
        foot = ("You get this because alerts are switched on for this value on the Thresholds page. "
                "Times are India time (IST).")
    eyebrow = (f"{'BACK TO NORMAL' if recovered else 'VALUE ALERT'} &nbsp;{ms.badge('OK' if recovered else lvl)}"
               f" &nbsp;{ms.esc(sysn)}")
    html = ms.page(level, eyebrow, title, lead, blocks, foot)
    import re as _re
    text = (_re.sub(r"<[^>]+>", "", lead) + "\n\n" + "\n".join(f"{k}: {v}" for k, v in rows)
            + ("\n\n" + meaning if meaning else ""))
    return subject, html, text


def send_test(system_cfg: dict, channel: str) -> dict:
    """Send a test alert on one channel to this system's receivers, now. Recorded like a real one."""
    if channel not in CHANNELS:
        raise ValueError("Unknown channel")
    import uuid
    job = {"system": system_cfg.get("name", ""), "metric": "test", "instance": "",
           "label": "Test alert", "level": "WARNING", "kind": "test",
           "value": "test", "limits": "", "channels": [channel], "batch": uuid.uuid4().hex[:12]}
    _send_all(system_cfg, job)
    from core import ops_db
    row = ops_db.one("SELECT * FROM alert_sends WHERE system=? AND metric='test' AND channel=? "
                     "ORDER BY id DESC LIMIT 1", (job["system"], channel))
    return row or {}


# ---------------------------------------------------------------- reading back

def last_sends(system: str | None = None, systems: list | None = None) -> dict:
    """{metric: last send with all its channels} -- for one system, or the newest across `systems`."""
    from core import ops_db
    if system:
        rows = ops_db.rows("SELECT * FROM alert_sends WHERE system=? AND metric<>'test' AND id IN "
                           "(SELECT MAX(id) FROM alert_sends WHERE system=? AND metric<>'test' GROUP BY metric)",
                           (system, system))
    else:
        allowed = [str(x) for x in (systems or [])]
        if not allowed:
            return {}
        marks = ",".join("?" * len(allowed))
        rows = ops_db.rows(f"SELECT * FROM alert_sends WHERE metric<>'test' AND id IN "
                           f"(SELECT MAX(id) FROM alert_sends WHERE metric<>'test' AND system IN ({marks}) "
                           f"GROUP BY metric)", tuple(allowed))
    out = {}
    for r in rows:
        if r.get("batch"):
            sib = ops_db.rows("SELECT channel, recipients, ok, note FROM alert_sends WHERE batch=? "
                              "ORDER BY id", (r["batch"],))
        else:
            sib = [{k: r[k] for k in ("channel", "recipients", "ok", "note")}]
        r["channels"] = sib
        out[r["metric"]] = r
    return out


def history(system: str | None = None, days: int = 7, limit: int = 500,
            systems: list | None = None) -> list[dict]:
    from core import ops_db
    since = (datetime.now() - timedelta(days=max(1, min(int(days), 90)))).strftime("%Y-%m-%d %H:%M:%S")
    if system:
        return ops_db.rows("SELECT * FROM alert_sends WHERE system=? AND at>=? ORDER BY id DESC LIMIT ?",
                           (system, since, int(limit)))
    allowed = [str(x) for x in (systems or [])]
    if systems is not None and not allowed:
        return []
    if systems is None:
        return ops_db.rows("SELECT * FROM alert_sends WHERE at>=? ORDER BY id DESC LIMIT ?",
                           (since, int(limit)))
    marks = ",".join("?" * len(allowed))
    return ops_db.rows(f"SELECT * FROM alert_sends WHERE at>=? AND system IN ({marks}) "
                       f"ORDER BY id DESC LIMIT ?", (since, *allowed, int(limit)))
