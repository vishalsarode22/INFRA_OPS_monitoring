"""
Time windows of the timed alerts ("ABAP dumps by one user in 15 minutes"),
set on the Thresholds & alerts page -- for all systems, or per system.

    config/alert_windows.yaml
        standard: {sap.st22.user_dumps_10min: 15, ...}
        systems:  {CEAT S4P: {sap.st22.user_dumps_10min: 30}}

A system's own value wins over the standard, the standard over the built-in
default. The metric KEYS keep their old names (…_10min, …_15min) so history,
limits and alert state stay attached; only the label shows the real window.
"""
from __future__ import annotations

import os
import threading

from utils.logger import get_logger

log = get_logger(__name__)


def _env_default(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


# key: (built-in default minutes, label with {m}, what the window means)
WINDOWS: dict[str, tuple[int, str, str]] = {
    "sap.st22.user_dumps_10min": (_env_default("IBO_DUMP_WINDOW_MIN", 15), "ABAP dumps by one user ({m} min)",
                                  "dumps counted per user over the last N minutes"),
    "sap.sm37.user_cancelled_15min": (15, "Cancelled jobs by one user ({m} min)",
                                      "jobs that ended cancelled in the last N minutes (SAP time)"),
    "sap.sm12.user_old_locks": (15, "Locks held {m} min+ by one user",
                                "a lock counts when it is older than N minutes"),
    "sap.sm12.users_same_table_15min": (15, "Users locking the same table ({m} min)",
                                        "locks set in the last N minutes"),
    "sap.smq2.waiting_10min": (10, "Inbound queue entries waiting {m} min+",
                               "an entry counts when it has waited N minutes"),
    "sap.smq2.error_10min": (10, "Inbound queue entries in error {m} min+",
                             "an entry counts when it has been in error N minutes"),
}
MIN_MINUTES, MAX_MINUTES = 1, 1440
_lock = threading.Lock()
_cache = {"stamp": None, "data": {"standard": {}, "systems": {}}}


def _path() -> str:
    from core.config_editor import config_dir
    return os.path.join(config_dir(), "alert_windows.yaml")


def load() -> dict:
    path = _path()
    try:
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        return {"standard": {}, "systems": {}}
    if _cache["stamp"] == stamp:
        return _cache["data"]
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"alert_windows.yaml unreadable: {exc}")
        return {"standard": {}, "systems": {}}

    def clean(d):
        out = {}
        for k, v in (d or {}).items() if isinstance(d, dict) else []:
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if str(k) in WINDOWS and MIN_MINUTES <= n <= MAX_MINUTES:
                out[str(k)] = n
        return out
    data = {"standard": clean(raw.get("standard")),
            "systems": {str(s): clean(v) for s, v in (raw.get("systems") or {}).items()}
            if isinstance(raw.get("systems"), dict) else {}}
    _cache.update(stamp=stamp, data=data)
    return data


def minutes(system: str | None, key: str) -> int:
    if key not in WINDOWS:
        raise KeyError(key)
    data = load()
    own = (data["systems"].get(str(system or "")) or {}).get(key)
    if own:
        return own
    return data["standard"].get(key) or WINDOWS[key][0]


def label(system: str | None, key: str) -> str:
    return WINDOWS[key][1].format(m=minutes(system, key))


def describe(system: str | None) -> dict:
    """{key: {minutes, default, standard, own, meaning}} for the Thresholds page."""
    data = load()
    own = data["systems"].get(str(system or "")) or {}
    return {k: {"minutes": minutes(system, k), "default": d, "standard": data["standard"].get(k) or d,
                "own": own.get(k), "meaning": meaning, "label": label(system, k)}
            for k, (d, _lbl, meaning) in WINDOWS.items()}


def save(system: str | None, key: str, value, by: str = "") -> dict:
    """Set (value) or clear (None / '') one window, for one system or the standard."""
    from core import timeline
    from core.config_editor import write_text_safely
    import yaml
    if key not in WINDOWS:
        raise ValueError(f"{key} has no time window.")
    n = None
    if value not in (None, ""):
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError("The window is a whole number of minutes.") from None
        if not MIN_MINUTES <= n <= MAX_MINUTES:
            raise ValueError(f"The window must be {MIN_MINUTES}-{MAX_MINUTES} minutes.")
    system = str(system or "").strip()
    with _lock:
        data = {"standard": dict(load()["standard"]),
                "systems": {s: dict(v) for s, v in load()["systems"].items()}}
        part = data["systems"].setdefault(system, {}) if system else data["standard"]
        if n is None:
            part.pop(key, None)
        else:
            part[key] = n
        data["systems"] = {s: v for s, v in data["systems"].items() if v}
        text = ("# Time windows of the timed alerts (Thresholds & alerts page).\n"
                "# standard: every system; systems: a system's own windows, in minutes.\n"
                + yaml.safe_dump(data, sort_keys=True, default_flow_style=False, allow_unicode=True))

        def check(parsed):
            got = ((parsed or {}).get("systems", {}).get(system) or {}) if system else (parsed or {}).get("standard") or {}
            if got.get(key) != n:
                raise RuntimeError("write check failed")
        write_text_safely("alert_windows.yaml", text, check)
        _cache["stamp"] = None
    timeline.add(system or "IBOPS", "config", f"Alert window {key}",
                 (f"{n} min" if n else "back to the standard") + (" (this system only)" if system else " (all systems)"),
                 actor=by or timeline.actor_name())
    return {"key": key, "system": system, "minutes": minutes(system, key)}
