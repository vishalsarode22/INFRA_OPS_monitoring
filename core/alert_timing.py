"""
Per-system alert timing: the "How often" column of the Systems page preview.

systems.yaml, per system (every key optional; empty = the .env value):

    alert_timing:
      raise_after: 3            readings in a row over the limit before the first mail
      repeat_critical_min: 60   reminder while a CRITICAL value stays bad
      repeat_warning_min: 480   reminder while a WARNING value stays bad
      escalate_window_min: 120  "bad again soon after it recovered" counts within this
      recovery_min_minutes: 30  "back to normal" mail only if the problem lasted this long
      down_after: 2             failed checks in a row before SYSTEM DOWN
      down_repeat_min: 5        SYSTEM DOWN reminder while it stays down
      access_repeat_min: 30     VPN / tunnel / logon problem reminder while it lasts
      rca_daily_cap: 4          automatic performance analyses a day (0 = no cap)
      mail_cap_per_day: 150     everyday mails a day for this system; CRITICAL, DOWN,
                                VPN and logon mails are not counted (0 = no cap)

A limit set for one value on the Thresholds page (raise_after,
recovery_min_minutes next to its limits) still wins over the system's.
"""
from __future__ import annotations

KEYS = {
    # key: (label, .env default name, built-in default, minimum)
    "raise_after": ("Readings over the limit before the first mail", "IBO_ALERT_RAISE_AFTER", 3, 1),
    "repeat_critical_min": ("Reminder for a CRITICAL value, minutes", "IBO_ALERT_REPEAT_CRITICAL_MIN", 60, 1),
    "repeat_warning_min": ("Reminder for a WARNING value, minutes", "IBO_ALERT_REPEAT_WARNING_MIN", 480, 1),
    "escalate_window_min": ("'Bad again soon' window, minutes", "IBO_ALERT_ESCALATE_WINDOW_MIN", 120, 1),
    "recovery_min_minutes": ("'Back to normal' only after, minutes", "IBO_ALERT_RECOVERY_MIN_MINUTES", 30, 0),
    "down_after": ("Failed checks before SYSTEM DOWN", "IBO_DOWN_CONSECUTIVE_FAILURES", 2, 1),
    "down_repeat_min": ("SYSTEM DOWN reminder, minutes", "IBO_DOWN_REPEAT_MIN", 5, 1),
    "access_repeat_min": ("VPN / logon problem reminder, minutes", "IBO_ACCESS_REPEAT_MIN", 30, 5),
    "rca_daily_cap": ("Automatic RCAs a day (0 = no cap)", "IBO_RCA_DAILY_CAP", 4, 0),
    "mail_cap_per_day": ("Everyday mails a day (CRITICAL not counted)", "IBO_MAIL_CAP_PER_SYSTEM", 40, 0),
}


def _num(value):
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _cfg(system) -> dict:
    if isinstance(system, dict):
        return system
    if not system:
        return {}
    try:
        from core.config_loader import get_all_systems
        name = str(system)
        if name.startswith("network:"):
            return {}
        return next((s for s in get_all_systems() if s.get("name") == name), None) or {}
    except Exception:  # noqa: BLE001
        return {}


def system_value(system, key: str):
    """The system's own setting, or None when it has none."""
    own = (_cfg(system).get("alert_timing") or {})
    if not isinstance(own, dict):
        return None
    v = _num(own.get(key))
    if v is None:
        return None
    return max(KEYS[key][3], v)


def env_value(key: str) -> int:
    import os
    _label, env, default, low = KEYS[key]
    if key == "mail_cap_per_day":
        from core.alert_ledger import cap_per_system
        return cap_per_system()
    v = _num(os.getenv(env))
    return default if v is None else max(low, v)


def get(system, key: str, fallback=None) -> int:
    """System setting > .env > fallback (or the built-in default)."""
    own = system_value(system, key)
    if own is not None:
        return own
    if fallback is not None:
        return fallback
    return env_value(key)


def clean(raw) -> dict:
    """What the Systems form sends -> what systems.yaml keeps (blank and bad entries dropped)."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, (_label, _env, _default, low) in KEYS.items():
        v = _num(raw.get(key))
        if v is not None:
            out[key] = max(low, v)
    return out


def describe(system=None) -> list[dict]:
    """For the form: each setting, what this system has, and what applies when empty."""
    out = []
    for key, (label, env, default, low) in KEYS.items():
        out.append({"key": key, "label": label, "env": env, "default": env_value(key),
                    "value": system_value(system, key) if system is not None else None, "min": low})
    return out
