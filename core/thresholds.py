"""
One answer to "what are the limits for metric X on system Y?".

Order, per field (warning, critical, inclusive, emergency):
    1. config/thresholds_systems.yaml   -- per system, set on the Thresholds
                                            page with a system selected
    2. config/thresholds.yaml           -- the company standard (all systems)
    3. the collector's built-in default -- what the code shipped with

thresholds_systems.yaml:
    systems:
      CEAT BW_PRD:
        sap.st22.dumps: {warning: 20, critical: 40}
      PS4:
        sap.sm12.lock_count: {critical: 3000}     # warning stays the standard

Which system is being graded: passed explicitly, or the "current system"
set by the code that reads / sweeps one system (use_system()). A thread or
process that never set one gets the company standard -- never another
system's override.

Both files are re-read only when they change (mtime), so a save on the
page applies at the next read without a restart.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import threading

_current: contextvars.ContextVar[str] = contextvars.ContextVar("ibo_threshold_system", default="")
_lock = threading.Lock()
_cache: dict[str, tuple] = {}          # path -> (mtime, size, data)
FIELDS = ("warning", "critical", "inclusive", "emergency", "severity", "enabled", "alert", "channels")
# Alert hysteresis per value (core/alert_engine.hysteresis), set in either
# file next to the limits -- per metric for all systems, or per system:
#   raise_after           consecutive breaching readings before the alert is raised
#   raise_minutes         ... or this many minutes in breach (whichever first)
#   clear_after           consecutive readings below the clear level before it clears
#   clear_pct             clear level, % of the alerting limit (default 80)
#   recovery_min_minutes  no "back to normal" mail for an alert shorter than this
HYSTERESIS_FIELDS = ("raise_after", "raise_minutes", "clear_after", "clear_pct",
                     "recovery_min_minutes")
FIELDS = FIELDS + HYSTERESIS_FIELDS
_edit_lock = threading.Lock()
# One figure, two names: the SAP GUI sweep reads ST22 as sap.st22.dump_count,
# the RFC read and the wall as sap.st22.dumps. A system's own limit set on
# either name applies to both, so the sheet and the wall agree.
ALIASES = {"sap.st22.dump_count": "sap.st22.dumps", "sap.st22.dumps": "sap.st22.dump_count"}


def _config_dir() -> str:
    try:
        from core.config_loader import CONFIG_DIR
        return CONFIG_DIR
    except Exception:  # noqa: BLE001
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")


def global_path() -> str:
    return os.path.join(_config_dir(), "thresholds.yaml")


def systems_path() -> str:
    return os.path.join(_config_dir(), "thresholds_systems.yaml")


def _load(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_mtime, st.st_size)
    with _lock:
        hit = _cache.get(path)
        if hit and hit[:2] == key:
            return hit[2]
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001 -- a broken file must not stop grading; the last good copy stays
        with _lock:
            hit = _cache.get(path)
        return hit[2] if hit else {}
    if not isinstance(data, dict):
        data = {}
    with _lock:
        _cache[path] = (key[0], key[1], data)
    return data


def global_table() -> dict:
    """thresholds.yaml as {metric: {field: value}} (non-mapping entries skipped)."""
    return {str(k): v for k, v in _load(global_path()).items() if isinstance(v, dict)}


def all_overrides() -> dict:
    """{system: {metric: {field: value}}}"""
    data = _load(systems_path()).get("systems") or {}
    out = {}
    if isinstance(data, dict):
        for sysname, metrics in data.items():
            if isinstance(metrics, dict):
                out[str(sysname)] = {str(k): v for k, v in metrics.items() if isinstance(v, dict)}
    return out


def overrides(system: str | None) -> dict:
    if not system:
        return {}
    return all_overrides().get(str(system), {})


# ------------------------------------------------------------ current system

def current_system() -> str:
    return _current.get()


@contextlib.contextmanager
def use_system(system: str | None):
    """Grade everything inside with this system's overrides."""
    token = _current.set(str(system or ""))
    try:
        yield
    finally:
        _current.reset(token)


def set_system(system: str | None) -> None:
    """For a thread that works on one system for its whole life (the sweep thread)."""
    _current.set(str(system or ""))


# ------------------------------------------------------------ lookups

def spec(name: str, system: str | None = None) -> dict:
    """Merged {warning, critical, inclusive, emergency} from the files (no code defaults)."""
    sysname = current_system() if system is None else system
    out = {}
    table = global_table()
    # Alias in the standard too (not only in overrides): editing the
    # standard sap.st22.dumps must also grade sap.st22.dump_count.
    g = table.get(name) or table.get(ALIASES.get(name, "")) or {}
    for f in FIELDS:
        if f in g:
            out[f] = g[f]
    own = overrides(sysname)
    o = own.get(name) or own.get(ALIASES.get(name, "")) or {}
    for f in FIELDS:
        if f in o:
            out[f] = o[f]
    return out


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def limits(name: str, default=(None, None), system: str | None = None) -> tuple:
    """(warning, critical) for `name` on `system` (default: the current system)."""
    s = spec(name, system)
    warn, crit = default if default else (None, None)
    if "warning" in s:
        warn = _num(s["warning"])
    if "critical" in s:
        crit = _num(s["critical"])
    return _intify(warn), _intify(crit)


def _intify(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def inclusive(name: str, default: bool = False, system: str | None = None) -> bool:
    s = spec(name, system)
    return bool(s["inclusive"]) if "inclusive" in s else default


def apply_to_table(table: dict, system: str | None = None) -> dict:
    """A thresholds.yaml-shaped dict with this system's overrides laid over it (a copy)."""
    sysname = current_system() if system is None else system
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in (table or {}).items()}
    for metric, o in overrides(sysname).items():
        base = out.get(metric) if isinstance(out.get(metric), dict) else {}
        merged = dict(base)
        merged.update({f: o[f] for f in FIELDS if f in o})
        out[metric] = merged
    return out


def apply_to_pairs(pairs: dict, system: str | None = None) -> dict:
    """A {metric: (warning, critical)} dict with thresholds.yaml and the overrides laid over it (a copy)."""
    out = dict(pairs or {})
    names = set(out) | set(global_table()) | set(overrides(current_system() if system is None else system))
    for name in names:
        out[name] = limits(name, out.get(name, (None, None)), system)
    return out


# ------------------------------------------------------------ editing (page)

def save_override(system: str, key: str, warning, critical, inclusive_flag=None, by: str = "",
                  severity=None, enabled=None, alert=None, channels=None) -> dict:
    """Set one metric's limits for one system. Same validation as the company standard."""
    from core import config_editor, timeline
    from core import metric_catalogue as cat
    system = str(system or "").strip()
    if not system or not _known_system(system):
        raise ValueError(f"Unknown system {system!r}.")
    errors, vals = config_editor.validate_threshold(key, warning, critical, inclusive_flag,
                                                    severity, enabled, alert, channels)
    if errors:
        raise ValueError(" ".join(errors))
    key = key.strip()
    entry = {k: vals[k] for k in ("warning", "critical", "inclusive", "severity", "enabled",
                                  "alert", "channels")
             if vals.get(k) is not None}
    # The pair that will actually grade: this entry over the standard (then
    # the built-in default).
    std = spec(key, system="")
    dw, dc = cat.default_limits(key)
    w = entry.get("warning", _num(std.get("warning", dw)))
    c = entry.get("critical", _num(std.get("critical", dc)))
    if w is not None and c is not None:
        low = cat.direction(key) == cat.LOW
        if (float(w) < float(c)) if low else (float(w) > float(c)):
            raise ValueError(f"For {system} that gives warning {_intify(_num(w))} "
                             f"{'below' if low else 'above'} critical {_intify(_num(c))} "
                             f"(the other value comes from the standard). Set both.")
    with _edit_lock:
        data = _editable()
        old = (data["systems"].get(system) or {}).get(key) or {}
        # The page does not edit the hysteresis settings; a save there must
        # not drop the ones written into the file.
        keep = {f: old[f] for f in HYSTERESIS_FIELDS if f in old}
        data["systems"].setdefault(system, {})[key] = {**entry, **keep}
        _write(data, lambda parsed: _check_written(parsed, system, key, entry))
    timeline.add(system, "config", f"Threshold {key} set for {system}",
                 config_editor._change_text(entry) + " (this system only)",
                 actor=by or timeline.actor_name())
    return entry


def remove_override(system: str, key: str, by: str = "") -> bool:
    from core import timeline
    with _edit_lock:
        data = _editable()
        metrics = data["systems"].get(system) or {}
        if key not in metrics:
            return False
        metrics.pop(key)
        if not metrics:
            data["systems"].pop(system, None)
        _write(data, lambda parsed: _check_removed(parsed, system, key))
    timeline.add(system, "config", f"Threshold {key} for {system} back to the standard", "",
                 actor=by or timeline.actor_name())
    return True


def _known_system(name: str) -> bool:
    try:
        from core.config_loader import get_all_systems
        return any(str(s.get("name")) == name for s in get_all_systems())
    except Exception:  # noqa: BLE001
        return False


def _editable() -> dict:
    data = dict(_load(systems_path()))
    systems = data.get("systems") if isinstance(data.get("systems"), dict) else {}
    data["systems"] = {str(k): dict(v) for k, v in systems.items() if isinstance(v, dict)}
    return data


_HEADER = ("# Per-system threshold overrides -- written by the Thresholds page (system selected).\n"
           "# Anything not listed here uses config/thresholds.yaml (the standard for all systems).\n")


def _write(data: dict, check) -> None:
    import yaml
    from core.config_editor import write_text_safely
    text = _HEADER + yaml.safe_dump({"systems": data["systems"]}, sort_keys=True, allow_unicode=True,
                                    default_flow_style=False)
    write_text_safely("thresholds_systems.yaml", text, check)
    with _lock:
        _cache.pop(systems_path(), None)


def _check_written(parsed, system, key, entry):
    got = ((parsed or {}).get("systems") or {}).get(system, {}).get(key) or {}
    for f in ("warning", "critical"):
        if f in entry and float(got.get(f)) != float(entry[f]):
            raise RuntimeError("write check failed")
    for f in ("severity", "enabled", "inclusive", "alert", "channels"):
        if f in entry and got.get(f) != entry[f]:
            raise RuntimeError("write check failed")


def _check_removed(parsed, system, key):
    if key in (((parsed or {}).get("systems") or {}).get(system) or {}):
        raise RuntimeError("write check failed")


# ------------------------------------------------------------ catalogue (page)

def catalogue() -> dict:
    """
    Every metric the dashboard and the sweeps grade, with the limits built
    into the code: {metric: {"warning", "critical", "label"}}. The Thresholds
    page lists these next to the entries of thresholds.yaml, so a limit that
    was only in the code can be seen and changed there too.
    """
    out: dict[str, dict] = {}

    def add(table, label_for=None):
        for name, pair in (table or {}).items():
            try:
                w, c = pair
            except (TypeError, ValueError):
                continue
            out.setdefault(str(name), {"warning": w, "critical": c, "label": ""})

    try:
        from collectors import rfc_collector
        add(rfc_collector._THRESHOLDS)
    except Exception:  # noqa: BLE001
        pass
    try:
        from collectors import rfc_perf
        add(rfc_perf._THRESHOLDS)
    except Exception:  # noqa: BLE001
        pass
    try:
        from core import live_metrics
        add(live_metrics.DEFAULT_THRESHOLDS)
    except Exception:  # noqa: BLE001
        pass
    try:
        from sap_gui.smlg_analyzer import SMLG_THRESHOLDS as _S
        out.setdefault("sap.smlg.response_time", {"warning": _S["warning_ms"], "critical": _S["critical_ms"],
                                                  "label": ""})
    except Exception:  # noqa: BLE001
        pass
    labels = {}
    try:
        from collectors.rfc_live import _CHECK_LABELS
        labels = {k: f"{v[0]} · {v[1]}" for k, v in _CHECK_LABELS.items()}
    except Exception:  # noqa: BLE001
        pass
    labels.update({"cpu": "CPU %", "memory": "Memory %", "disk": "File system % used", "swap": "Swap %",
                   "load_1m": "Load (1 min)", "sap.smlg.response_time": "SMLG · Response time (ms)",
                   "dialog_queue": "Dialog queue", "update_queue": "Update queue",
                   "db_rtt_ms": "DB round trip (ms)", "sap.sm58.stuck_entries": "SM58 · failed tRFC today",
                   "sap.sm58.entries_today": "SM58 · entries displayed (today)",
                   "sap.smq1.entries": "SMQ1 · Outbound queue entries",
                   "sap.smq2.entries": "SMQ2 · Inbound queue entries"})
    for name in set(out) | set(global_table()):
        out.setdefault(name, {"warning": None, "critical": None, "label": ""})
        out[name]["label"] = labels.get(name, out[name].get("label") or "")

    # Every value the dashboard grades (core/metric_catalogue.py): plain
    # name, group, unit, which direction is worse, where it is shown and
    # where it is stored. The catalogue default wins over older code tables
    # (e.g. live_metrics' stale 10/50 for SM12), because it is what grades.
    from core import metric_catalogue as cat
    table = global_table()
    for name, e in cat.CATALOGUE.items():
        row = out.setdefault(name, {"warning": None, "critical": None, "label": ""})
        d = e.get("default")
        if isinstance(d, (tuple, list)):
            row["warning"], row["critical"] = d
        if e["direction"] == cat.STATE:
            row["warning"] = row["critical"] = None
            row["severity"] = cat.default_severity(name)
        row.update({"label": e["label"], "group": e["group"], "unit": e["unit"],
                    "direction": e["direction"], "where": e["where"], "stored": e["stored"],
                    "tcode": e["tcode"], "note": e["note"]})
    for name in list(out):
        if name in cat.DEAD_KEYS and name not in table:
            out.pop(name)
        elif name in cat.ALIASES.values():
            out.pop(name)                     # shown and saved under its main name
    for name, row in out.items():
        row.setdefault("group", "Other")
        row.setdefault("direction", cat.HIGH)
        for f in ("warning", "critical"):
            row[f] = _intify(_num(row[f]))
    return out
