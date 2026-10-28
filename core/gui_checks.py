"""
Values and grades for the SAP GUI checks read by sap_gui/tcode_actions_extra.py:
ST03N workload, SMICM services, ST02 buffers, ST04 database, ST05 traces,
STMS systems and STRUST certificates.

Each check's screen facts become graded metrics (core.grading.grade_number,
so every limit can be changed per system on the Thresholds & alerts page),
and the check itself gets one status and one observation line for the
monitoring sheet:

    result.extra_data["check_status"]       NORMAL / WARNING / CRITICAL / UNKNOWN
    result.extra_data["check_observation"]  what is wrong, in plain words

ST02 swaps are graded per day: the swaps since the last reading, scaled to
24 hours. The previous reading is kept per system (meta table, key
"st02_swaps:<system>"); after an instance restart (new startup time) the
swaps since startup are used instead.
"""
from __future__ import annotations

import json
from datetime import datetime

from core.models import MetricResult, Status
from utils.logger import get_logger

log = get_logger(__name__)

READERS = {"st03n_workload", "smicm_services", "st02_tune", "st04_db", "st05_status",
           "stms_systems", "strust_certs"}

ST03_KEYS = {"DIALOG": "sap.st03n.dialog_avg_ms", "BACKGROUND": "sap.st03n.background_avg_ms",
             "WS-RFC": "sap.st03n.wsrfc_avg_ms"}
DEFAULT_ST02_GRADED = ["Program", "CUA", "Screen", "Tables Buffer", "Nametab Table definition",
                       "Nametab Field definition"]
_MIN_INTERVAL_H = 1.0          # readings closer than this are not used as a new baseline


def is_reader_result(result) -> bool:
    return (result.extra_data or {}).get("reader") in READERS


def _status(text: str) -> Status:
    return {"NORMAL": Status.NORMAL, "WARNING": Status.WARNING, "CRITICAL": Status.CRITICAL}.get(
        str(text), Status.UNKNOWN)


def _metric(result, name, value, display, unit, system, detail, category):
    from core.grading import grade_number
    g = grade_number(name, value, system)
    return MetricResult(
        name=name, value=value, display_value=display if value is not None else "not read",
        status=_status(g["status"]), threshold_warning=g.get("warning"), threshold_critical=g.get("critical"),
        source="sap_gui_collector", tcode=(result.tcode or "").upper(), detail=detail,
        screenshot_path=result.screenshot_path, screenshot_paths=list(result.screenshot_paths),
        extra_data={"evidence_metric": True, "check": result.extra_data.get("reader")},
        timestamp=result.timestamp, category=category, unit=unit, metric_type="gauge")


def _fmt(v, unit=""):
    if v is None:
        return "-"
    v = float(v)
    s = f"{v:,.0f}" if v.is_integer() or abs(v) >= 100 else f"{v:,.1f}"
    return f"{s} {unit}".strip() if unit != "%" else f"{s}%"


# ------------------------------------------------------------------ checks

def _st03n(result, f, system):
    out, notes = [], []
    read = f.get("task_types") or {}
    present = set(f.get("all_task_types") or [])
    for tt in [str(w).upper() for w in f.get("wanted") or []]:
        if tt in present and tt not in read:
            notes.append(f"{tt} figures not readable")
            f["_incomplete"] = True
        key = ST03_KEYS.get(tt)
        if key is None:
            continue                                    # shown in the sheet, no limit for this task type
        if tt not in read and present and tt not in present:
            continue                                    # no steps of that type in the period: nothing to grade
        val = read[tt].get("avg_ms") if tt in read else None
        out.append(_metric(result, key, val, _fmt(val, "ms"), "ms", system,
                           f"ST03N average response time, {tt}, {f.get('period') or ''}", "response"))
    if f.get("note"):
        notes.append(f["note"])
    return out, notes


def _smicm(result, f, system):
    services = f.get("services") or []
    inactive, unknown = f.get("inactive") or 0, f.get("unknown_state") or 0
    if not services:
        val = None
    elif unknown and not inactive:
        val = None                                      # an unreadable state is not "all active"
    else:
        val = float(inactive)
    notes = []
    if inactive:
        notes.append("not active: " + ", ".join(f"{s['protocol']} {s['port']}" for s in services
                                                if s.get("active") is False))
    if unknown:
        notes.append(f"state not readable for {unknown} service(s) -- see screenshot")
        f["_incomplete"] = True
    return [_metric(result, "sap.smicm.inactive_services", val, _fmt(val), "count", system,
                    "ICM services that are not active (SMICM > Goto > Services)", "availability")], notes


def _graded_buffers(f, names) -> list[dict]:
    want = {str(n).strip().lower() for n in names}
    return [b for b in f.get("buffers") or [] if str(b.get("buffer", "")).strip().lower() in want]


def _swaps_per_day(system, f, graded) -> tuple[float | None, str, dict]:
    """Highest swaps per day of the graded buffers, how it was worked out, and per buffer."""
    from core import ops_db
    snap = _parse(f.get("snapshot")) or datetime.now()
    startup = str(f.get("startup") or "")
    now_swaps = {b["buffer"]: float(b["swaps"]) for b in graded if b.get("swaps") is not None}
    key = f"st02_swaps:{system}:{f.get('instance') or ''}"
    prev = None
    try:
        row = ops_db.one("SELECT value FROM meta WHERE key=?", (key,))
        prev = json.loads(row["value"]) if row else None
    except Exception as exc:  # noqa: BLE001
        log.debug(f"ST02 previous reading: {exc}")
    per, basis = {}, ""
    hours = None
    if prev and prev.get("startup") == startup and _parse(prev.get("snapshot")):
        hours = (snap - _parse(prev["snapshot"])).total_seconds() / 3600
    if hours is not None and hours >= _MIN_INTERVAL_H:
        for b, n in now_swaps.items():
            before = (prev.get("swaps") or {}).get(b)
            if before is None or n < before:
                continue
            per[b] = round((n - before) * 24 / hours, 1)
        basis = f"swaps since the last reading {prev['snapshot']} ({hours:.1f} h), per day"
        _save = True
        if not per and now_swaps:
            hours = None                      # counters were reset: fall through to "since startup"
    if per or (hours is not None and hours >= _MIN_INTERVAL_H):
        pass
    elif hours is not None and 0 <= hours < _MIN_INTERVAL_H and prev.get("per_day") is not None:
        # Readings minutes apart say nothing per day: keep the last result and the older baseline.
        per = dict(prev.get("per_day_by_buffer") or {})
        basis = prev.get("basis") or ""
        _save = False
    else:
        start = _parse(startup)
        days = max(((snap - start).total_seconds() / 86400) if start else 1.0, 1.0)
        per = {b: round(n / days, 1) for b, n in now_swaps.items()}
        basis = (f"first reading since the instance started {startup}: swaps since startup per day"
                 if startup else "swaps since startup per day")
        _save = hours is None or hours >= 0       # an older screen never moves the baseline back
    worst = max(per.values()) if per else None
    if _save and now_swaps:
        try:
            ops_db.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                           "value=excluded.value",
                           (key, json.dumps({"snapshot": f"{snap:%d.%m.%Y %H:%M:%S}", "startup": startup,
                                             "swaps": now_swaps, "per_day": worst,
                                             "per_day_by_buffer": per, "basis": basis})))
        except Exception as exc:  # noqa: BLE001
            log.debug(f"ST02 reading not kept: {exc}")
    return worst, basis, per


def _parse(text):
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(text or "").strip(), fmt)
        except ValueError:
            continue
    return None


def _st02(result, f, system, settings):
    names = settings.get("graded_buffers") or DEFAULT_ST02_GRADED
    graded = _graded_buffers(f, names)
    hits = [b["hit_ratio"] for b in graded if b.get("hit_ratio") is not None]
    hit = min(hits) if hits else None
    low = min(graded, key=lambda b: b.get("hit_ratio", 999)) if hits else None
    out = [_metric(result, "sap.st02.min_hit_ratio", hit, _fmt(hit, "%"), "%", system,
                   "Lowest hit ratio of the graded buffers" + (f" ({low['buffer']})" if low else ""), "memory")]
    per_day, basis, per = _swaps_per_day(system, f, graded) if graded else (None, "", {})
    top = max(per, key=per.get) if per else ""
    out.append(_metric(result, "sap.st02.swaps_per_day", per_day, _fmt(per_day), "count", system,
                       f"Most swaps per day of the graded buffers ({top}); {basis}", "memory"))
    f["swaps_per_day"] = per
    f["swaps_basis"] = basis
    notes = []
    if low is not None:
        notes.append(f"lowest hit ratio {low['buffer']} {low['hit_ratio']}%")
    if per:
        notes.append(f"most swaps {top} {per[top]:,.0f}/day")
    missing = [n for n in names if n.strip().lower() not in {b['buffer'].strip().lower() for b in graded}]
    if missing:
        notes.append("not on the screen: " + ", ".join(missing))
        f["_incomplete"] = True
    return out, notes


def _configured_db(system: str | None) -> str:
    """'hana', 'ase', 'oracle', ... from the system's configuration, or '' when it does not say."""
    try:
        from core.config_loader import get_systems
        cfg = next((s for s in get_systems() or [] if str(s.get("name") or "") == str(system or "")), {})
        if cfg.get("hana"):
            return "hana"
        db = cfg.get("db") or {}
        if isinstance(db, dict) and str(db.get("type") or "").strip().lower() in ("ase", "sybase"):
            return "ase"                                  # the ASE monitoring login (collectors/ase_collector.py)
    except Exception:  # noqa: BLE001
        pass
    try:
        import yaml
        from core.config_loader import CONFIG_DIR
        import os
        with open(os.path.join(CONFIG_DIR, "rca.yaml"), encoding="utf-8") as fh:
            hint = str((((yaml.safe_load(fh) or {}).get("database") or {}).get("systems") or {}).get(system) or "")
        return "" if hint.lower() in ("", "detect", "generic") else hint.lower()
    except Exception:  # noqa: BLE001
        return ""


def _st04(result, f, system):
    if f.get("database") != "HANA":
        conf = _configured_db(system)
        screen = f.get("database") if f.get("database") not in (None, "", "not recognised") else ""
        if (conf and conf != "hana") or (screen and not conf and not f.get("mentions_hana")):
            f["_not_applicable"] = (f"{screen or conf.upper()}: overview captured; the ST04 limits are for "
                                    "HANA, so these values are shown, not graded")
            return [], []
        f["_incomplete"] = True
        return [], [("the HANA overview did not open" if conf == "hana" else
                     "the ST04 screen was not recognised as HANA or another database")
                    + " -- see screenshot"]
    out = []
    for key, part, label in (("sap.st04.memory_used_pct", "memory", "HANA memory used / allocation limit"),
                             ("sap.st04.tenant_memory_pct", "tenant_memory", "HANA tenant memory used / total"),
                             ("sap.st04.data_volume_pct", "data_volume", "HANA data volume / disk"),
                             ("sap.st04.log_volume_pct", "log_volume", "HANA log volume / disk")):
        v = (f.get(part) or {}).get("pct")
        out.append(_metric(result, key, v, _fmt(v, "%"), "%", system, label, "database"))
    return out, ([f["large_tables"]["note"]] if (f.get("large_tables") or {}).get("note") else [])


def _st05(result, f, system):
    v = f.get("traces_on")
    v = float(v) if v is not None else None
    return [_metric(result, "sap.st05.traces_on", v, _fmt(v), "count", system,
                    "ST05 traces switched on", "configuration")], []


def _stms(result, f, system):
    systems = f.get("systems") or []
    v = float(f.get("unavailable") or 0) if systems else None
    notes = []
    if f.get("unjudged"):
        notes.append("connection test not judged for " + ", ".join(f["unjudged"]))
        f["_incomplete"] = True
    return [_metric(result, "sap.stms.unavailable_systems", v, _fmt(v), "count", system,
                    "Transport domain systems not available (status or connection test)", "availability")], notes


def _strust(result, f, system):
    v = f.get("min_days_left")
    v = float(v) if v is not None else None
    soon = f.get("soonest") or {}
    out = [_metric(result, "sap.strust.min_days_left", v, _fmt(v, "days"), "days", system,
                   f"Own certificate closest to expiry: {soon.get('pse', '-')} valid to {soon.get('valid_to', '-')}",
                   "security")]
    notes = []
    if f.get("unreadable"):
        notes.append("expiry not readable for " + ", ".join(f["unreadable"]))
        f["_incomplete"] = True
    return out, notes


def metrics_for(result, system: str | None = None, settings: dict | None = None) -> list[MetricResult]:
    """Graded metrics of one reader result; also sets check_status / check_observation on it."""
    f = result.extra_data or {}
    reader = f.get("reader")
    settings = settings or f.get("settings") or {}
    if f.get("not_authorized"):
        f["check_status"] = "NOT AUTHORIZED"
        f["check_observation"] = (f"The monitoring user may not open {result.tcode}: {f.get('error') or ''}. "
                                  "Give the role the authorization, or untick the T-code for this system "
                                  "on the Profile page.")
        return []
    try:
        if reader == "st03n_workload":
            out, notes = _st03n(result, f, system)
        elif reader == "smicm_services":
            out, notes = _smicm(result, f, system)
        elif reader == "st02_tune":
            out, notes = _st02(result, f, system, settings)
        elif reader == "st04_db":
            out, notes = _st04(result, f, system)
        elif reader == "st05_status":
            out, notes = _st05(result, f, system)
        elif reader == "stms_systems":
            out, notes = _stms(result, f, system)
        elif reader == "strust_certs":
            out, notes = _strust(result, f, system)
        else:
            return []
    except Exception as exc:  # noqa: BLE001 -- a grading problem must not lose the sweep
        log.error(f"{reader}: grading failed: {type(exc).__name__}: {exc}")
        out, notes = [], [f"grading failed: {type(exc).__name__}"]
    order = {Status.UNKNOWN: 0, Status.NORMAL: 1, Status.WARNING: 2, Status.CRITICAL: 3}
    graded = [m for m in out if m.status != Status.UNKNOWN]
    incomplete = bool(f.pop("_incomplete", False)) or any(m.status == Status.UNKNOWN for m in out)
    not_applicable = f.pop("_not_applicable", "")
    # Something not read is never "all fine": a WARNING / CRITICAL among what
    # was read stands; otherwise the check is UNKNOWN.
    if graded:
        status = max((m.status for m in graded), key=lambda s: order[s])
        if status == Status.NORMAL and (incomplete or f.get("error")):
            status = Status.UNKNOWN
    elif not incomplete and not f.get("error") and f.get("report_rows"):
        status = Status.NORMAL                    # everything read; nothing here has a limit
        notes = notes + ["shown, not graded (no limit applies to these values)"]
    else:
        status = Status.UNKNOWN
    bad = [f"{_label(m.name)} {m.display_value} ({m.status.value})" for m in out
           if m.status in (Status.WARNING, Status.CRITICAL)]
    obs = "; ".join(bad + notes + ([f["error"]] if f.get("error") else []))
    if not_applicable and not f.get("error"):
        f["check_status"] = "NOT APPLICABLE"
        f["check_observation"] = "; ".join([not_applicable] + notes)
        return out
    f["check_status"] = status.value
    f["check_observation"] = obs or ("All within limits" if status == Status.NORMAL else
                                      "Not everything could be read -- see the screenshot")
    return out


def _label(key: str) -> str:
    try:
        from core import metric_catalogue as cat
        return (cat.entry(key) or {}).get("label") or key
    except Exception:  # noqa: BLE001
        return key
