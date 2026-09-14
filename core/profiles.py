"""
Monitoring profiles: several ways of watching one system, each with its own
T-code set, schedule and destination.

The existing scheduler holds one schedule per system. That forces a single
compromise: heavy enough to be a useful record and therefore too slow to run
often, or light enough to run often and therefore not a record of anything.
A profile lets both exist -- a four-screen check every two hours to a phone,
and the full sweep once a day to email.

TWO RULES THAT SHAPE THIS MODULE

Capture is not delivery. A profile that captures a T-code always writes it to
the evidence store; `deliver` only decides who gets told. Muting a phone
notification must never remove a screen from the audit record, or the record
quietly becomes a function of who was on call.

Alerts are not profiles. A CRITICAL finding routes by severity, immediately,
regardless of which profile was scheduled. Tying alerts to a delivery slot is
how an incident waits two hours for the next sweep to mention it.
"""

from __future__ import annotations

import os
import time
import threading
from datetime import datetime, timedelta

import yaml

from core.config_loader import CONFIG_DIR
from core.schedules import _normalise, describe, is_due, next_run_at
from utils.logger import get_logger

log = get_logger(__name__, "profiles")

PROFILES_PATH = os.path.join(CONFIG_DIR, "monitoring_profiles.yaml")
STATE_PATH = os.path.join(CONFIG_DIR, "profile_state.json")

_lock = threading.Lock()

_DEFAULT_ALERTS = {
    "CRITICAL": {"deliver": ["email", "whatsapp"], "repeat_after_minutes": 120},
    "WARNING": {"deliver": ["email"], "repeat_after_minutes": 480},
    "UNKNOWN": {"deliver": [], "repeat_after_minutes": 1440},
}

VALID_CHANNELS = ("email", "whatsapp")


_raw_cache: dict = {"data": None, "at": 0.0, "mtime": None}
_raw_cache_lock = threading.Lock()
_RAW_TTL_S = 2.0


def _load_raw() -> dict:
    """
    Parsed monitoring_profiles.yaml, cached for a couple of seconds.

    This is read on every /api/scheduler poll (every 15s per open tab). YAML
    parsing plus a disk open on each of those, while the single SAP GUI thread
    is busy, is what made the endpoint take seconds. The cache serves the
    parsed dict without touching disk, refreshing when it is older than the
    TTL or when the file's mtime changes -- so an edit still shows up within
    a couple of seconds, and a burst of polls costs one read at most.
    """
    if not os.path.isfile(PROFILES_PATH):
        return {}
    now = time.monotonic()
    with _raw_cache_lock:
        cached = _raw_cache["data"]
        if cached is not None and (now - _raw_cache["at"]) < _RAW_TTL_S:
            return cached
        try:
            mtime = os.path.getmtime(PROFILES_PATH)
        except OSError:
            mtime = None
        # File unchanged since last parse: reuse it, just extend the window.
        if cached is not None and mtime is not None and mtime == _raw_cache["mtime"]:
            _raw_cache["at"] = now
            return cached
        try:
            with open(PROFILES_PATH, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except Exception as exc:
            # A broken profiles file must not stop monitoring. Falling back to
            # no profiles means the legacy per-system schedule still runs.
            log.error(f"Could not read {PROFILES_PATH}: {exc}")
            return cached if cached is not None else {}
        _raw_cache.update({"data": data, "at": now, "mtime": mtime})
        return data


def _recipients(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(r).strip() for r in raw if str(r).strip()]
    import re as _re
    return [r.strip() for r in _re.split(r"[,;\n]", str(raw)) if r.strip()]


def load_profiles() -> list[dict]:
    """All profiles, normalised. Invalid entries are dropped with a warning."""
    raw = _load_raw()
    out = []

    for entry in raw.get("profiles", []) or []:
        pid = str(entry.get("id") or "").strip()
        system = str(entry.get("system") or "").strip()
        if not pid or not system:
            log.warning(f"Profile skipped: needs both id and system -- {entry}")
            continue

        channels = [c for c in (entry.get("deliver") or [])
                    if c in VALID_CHANNELS]
        unknown = [c for c in (entry.get("deliver") or [])
                   if c not in VALID_CHANNELS]
        if unknown:
            # Named loudly rather than ignored: a typo in a channel name
            # otherwise looks identical to "nobody was meant to be told".
            log.warning(f"Profile {pid}: unknown delivery channel(s) "
                        f"{unknown} -- valid are {list(VALID_CHANNELS)}")

        tcodes = entry.get("tcodes", "all")
        if isinstance(tcodes, str):
            tcodes = tcodes.strip().lower()
            tcodes = "all" if tcodes == "all" else [tcodes.upper()]
        else:
            tcodes = [str(t).strip().upper() for t in (tcodes or []) if str(t).strip()]

        out.append({
            "id": pid,
            "system": system,
            "label": entry.get("label") or pid,
            "description": entry.get("description") or "",
            "tcodes": tcodes,
            "schedule": _normalise(entry.get("schedule") or {}),
            "deliver": channels,
            # Addresses and numbers are configuration, not secrets, so they
            # live here and are shown in full. Credentials stay in .env.
            # Empty means "use the global default from .env" rather than
            # "send to nobody" -- most profiles want the same recipients.
            "email_to": _recipients(entry.get("email_to")),
            "email_cc": _recipients(entry.get("email_cc")),
            "whatsapp_to": _recipients(entry.get("whatsapp_to")),
            # Which sender account to use. Empty falls back to an account
            # claiming this system, then to the global settings in .env, so a
            # profile with nothing chosen still sends rather than silently
            # delivering nothing.
            "email_account": str(entry.get("email_account") or "").strip(),
            "whatsapp_account": str(entry.get("whatsapp_account") or "").strip(),
        })

    return out


def save_profiles(profiles: list[dict], alerts: dict | None = None) -> list[dict]:
    """
    Write profiles back to monitoring_profiles.yaml.

    Rewrites the whole file, so the descriptive comments in the shipped
    version are lost on first save. A short header is re-emitted to keep the
    file self-explanatory for anyone who opens it later -- an editable config
    with no explanation of what its fields mean is how the wrong value gets
    set confidently.

    Validation happens here rather than in the UI: the file can also be edited
    by hand, and a rule enforced only in the browser is not enforced.
    """
    cleaned = []
    seen: set[str] = set()

    for entry in profiles or []:
        pid = str(entry.get("id") or "").strip()
        system = str(entry.get("system") or "").strip()
        if not pid or not system:
            raise ValueError("Every profile needs an id and a system.")
        if pid in seen:
            raise ValueError(f"Duplicate profile id: {pid}")
        seen.add(pid)

        bad = [c for c in (entry.get("deliver") or []) if c not in VALID_CHANNELS]
        if bad:
            raise ValueError(f"{pid}: unknown delivery channel(s) {bad}. "
                             f"Valid: {list(VALID_CHANNELS)}")

        tcodes = entry.get("tcodes", "all")
        if isinstance(tcodes, str) and tcodes.strip().lower() == "all":
            tcodes = "all"
        else:
            tcodes = [str(t).strip().upper() for t in (tcodes or []) if str(t).strip()]
            if not tcodes:
                raise ValueError(f"{pid}: select at least one T-code, or use "
                                 f"'all'. A profile that captures nothing "
                                 f"would run and deliver an empty message.")

        emails = _recipients(entry.get("email_to"))
        ccs = _recipients(entry.get("email_cc"))
        phones = _recipients(entry.get("whatsapp_to"))

        from core.delivery_settings import validate_recipients
        problems = validate_recipients(emails + ccs, phones)
        if problems:
            # Rejected on save, not at send time. A malformed address fails
            # silently at 3am, when the alert it was meant to carry is the
            # reason anyone is awake.
            raise ValueError(f"{pid}: " + "; ".join(problems))

        cleaned.append({
            "id": pid,
            "system": system,
            "label": entry.get("label") or pid,
            "description": entry.get("description") or "",
            "tcodes": tcodes,
            "schedule": _normalise(entry.get("schedule") or {}),
            "deliver": [c for c in (entry.get("deliver") or [])
                        if c in VALID_CHANNELS],
            "email_to": emails,
            "email_cc": ccs,
            "whatsapp_to": phones,
            "email_account": str(entry.get("email_account") or "").strip(),
            "whatsapp_account": str(entry.get("whatsapp_account") or "").strip(),
        })

    payload = {"profiles": cleaned,
               "alerts": alerts if alerts is not None else _load_raw().get("alerts")
               or {k: dict(v) for k, v in _DEFAULT_ALERTS.items()}}

    header = (
        "# Monitoring profiles -- edited from the Profiles page in the UI.\n"
        "#\n"
        "# A system can be watched in more than one way at once: a short check\n"
        "# every couple of hours, and a full sweep once a day. They need\n"
        "# different T-codes, frequencies and destinations, so each is its own\n"
        "# profile.\n"
        "#\n"
        "#   tcodes: all   -> every task in monitoring_tasks.yaml\n"
        "#   tcodes: [...] -> only these\n"
        "#   deliver       -> email, whatsapp, or both\n"
        "#\n"
        "# Alerts route by SEVERITY, not by profile: a CRITICAL finding is sent\n"
        "# when it is found, not when the next profile happens to deliver.\n\n")

    with _lock:
        os.makedirs(os.path.dirname(PROFILES_PATH), exist_ok=True)
        with open(PROFILES_PATH, "w", encoding="utf-8") as handle:
            handle.write(header)
            yaml.safe_dump(payload, handle, sort_keys=False,
                           default_flow_style=False, allow_unicode=True)

    log.info(f"Saved {len(cleaned)} monitoring profile(s).")
    return cleaned


def save_alerts(alerts: dict) -> dict:
    """Update only the severity routing table, leaving profiles untouched."""
    merged = {}
    for severity, cfg in (alerts or {}).items():
        key = str(severity).upper()
        merged[key] = {
            "deliver": [c for c in (cfg.get("deliver") or []) if c in VALID_CHANNELS],
            "repeat_after_minutes": max(0, int(cfg.get("repeat_after_minutes", 240))),
        }
    save_profiles(load_profiles(), alerts=merged)
    return merged


def available_tcodes() -> list[dict]:
    """Every T-code that has a capture task, for the picker."""
    try:
        path = os.path.join(CONFIG_DIR, "monitoring_tasks.yaml")
        with open(path, "r", encoding="utf-8") as handle:
            tasks = (yaml.safe_load(handle) or {}).get("tasks", [])
        return [{"tcode": str(t.get("tcode", "")).strip().upper(),
                 "action": t.get("action", "")}
                for t in tasks if t.get("tcode")]
    except Exception as exc:
        log.warning(f"Could not read monitoring_tasks.yaml: {exc}")
        return []


def alert_routing() -> dict:
    raw = _load_raw().get("alerts") or {}
    merged = {k: dict(v) for k, v in _DEFAULT_ALERTS.items()}
    for severity, cfg in raw.items():
        key = str(severity).upper()
        if key not in merged:
            merged[key] = {"deliver": [], "repeat_after_minutes": 240}
        merged[key].update({
            "deliver": [c for c in (cfg.get("deliver") or []) if c in VALID_CHANNELS],
            "repeat_after_minutes": int(cfg.get("repeat_after_minutes", 240)),
        })
    return merged


def profiles_for(system: str) -> list[dict]:
    return [p for p in load_profiles() if p["system"] == system]


def resolve_tcodes(profile: dict) -> list[str]:
    """
    The T-codes this profile captures, in configured order.

    A profile naming a T-code that has no task is reported rather than
    silently dropped -- an image that never arrives is otherwise
    indistinguishable from one that arrived and showed nothing wrong.
    """
    try:
        path = os.path.join(CONFIG_DIR, "monitoring_tasks.yaml")
        with open(path, "r", encoding="utf-8") as handle:
            tasks = (yaml.safe_load(handle) or {}).get("tasks", [])
        available = [str(t.get("tcode", "")).strip().upper()
                     for t in tasks if t.get("tcode")]
    except Exception as exc:
        log.warning(f"Could not read monitoring_tasks.yaml: {exc}")
        return []

    if profile.get("tcodes") == "all":
        return available

    wanted = profile.get("tcodes") or []
    missing = [t for t in wanted if t not in available]
    if missing:
        log.warning(f"Profile {profile['id']}: no capture task for {missing}. "
                    f"Add them to monitoring_tasks.yaml or they will never be sent.")
    return [t for t in wanted if t in available]


# ---------------------------------------------------------------------------
# Run state -- last run per profile, and last alert per incident
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if not os.path.isfile(STATE_PATH):
        return {"profiles": {}, "alerts": {}}
    try:
        import json
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            state = json.load(handle) or {}
        state.setdefault("profiles", {})
        state.setdefault("alerts", {})
        return state
    except Exception:
        return {"profiles": {}, "alerts": {}}


def _save_state(state: dict) -> None:
    try:
        import json
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
    except Exception as exc:
        log.warning(f"Could not save profile state: {exc}")


def last_run(profile_id: str) -> datetime | None:
    stamp = _load_state()["profiles"].get(profile_id)
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


def mark_run(profile_id: str, when: datetime | None = None) -> None:
    with _lock:
        state = _load_state()
        state["profiles"][profile_id] = (when or datetime.now()).isoformat(
            timespec="seconds")
        _save_state(state)


def due_profiles(now: datetime | None = None) -> list[dict]:
    """Profiles whose next run has arrived, each with its resolved T-codes."""
    now = now or datetime.now()
    due = []
    for profile in load_profiles():
        if not profile["schedule"].get("enabled", True):
            continue
        if is_due(profile["schedule"], last_run(profile["id"]), now):
            entry = dict(profile)
            entry["resolved_tcodes"] = resolve_tcodes(profile)
            due.append(entry)
    return due


def status(now: datetime | None = None) -> list[dict]:
    """Every profile with its schedule description and next run, for the UI."""
    now = now or datetime.now()
    out = []
    for profile in load_profiles():
        previous = last_run(profile["id"])
        nxt = next_run_at(profile["schedule"], previous, now)
        out.append({
            **profile,
            "resolved_tcodes": resolve_tcodes(profile),
            "schedule_text": describe(profile["schedule"]),
            "last_run": previous.isoformat(timespec="seconds") if previous else None,
            "next_run": nxt.isoformat(timespec="seconds") if nxt else None,
            "due": bool(nxt and nxt <= now),
        })
    return out


# ---------------------------------------------------------------------------
# Alert suppression
# ---------------------------------------------------------------------------

def should_alert(key: str, severity: str, now: datetime | None = None) -> bool:
    """
    Whether this alert may be sent, or is still inside its quiet window.

    `key` identifies the condition -- system plus rule id, not the timestamp --
    so the same ongoing incident is recognised across cycles. Without this a
    persistent CRITICAL mails every sweep, and a channel that alerts
    identically at 3am and 3pm is muted by its recipients within a week.
    """
    now = now or datetime.now()
    routing = alert_routing().get(str(severity).upper())
    if not routing or not routing["deliver"]:
        return False

    state = _load_state()
    stamp = state["alerts"].get(key)
    if not stamp:
        return True
    try:
        previous = datetime.fromisoformat(stamp)
    except ValueError:
        return True

    quiet = timedelta(minutes=routing["repeat_after_minutes"])
    return now - previous >= quiet


def mark_alerted(key: str, when: datetime | None = None) -> None:
    with _lock:
        state = _load_state()
        state["alerts"][key] = (when or datetime.now()).isoformat(timespec="seconds")
        _save_state(state)


def channels_for(severity: str) -> list[str]:
    return alert_routing().get(str(severity).upper(), {}).get("deliver", [])
