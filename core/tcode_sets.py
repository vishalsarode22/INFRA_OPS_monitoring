"""
Which T-codes a system's sweep captures, in which order, with which settings.

Set per system on the Profile page ("T-codes for this system" card) and kept
in config/tcode_sets.yaml:

    systems:
      CEAT S4_QAS:
        tcodes:
          - {tcode: SM12}
          - {tcode: ST03N, settings: {period: today, task_types: [DIALOG, BACKGROUND, WS-RFC]}}
          - {tcode: STMS, settings: {connection_test: true}}
        off: [DB12]

Every sweep of the system uses this list: the SAP GUI captures, the
monitoring sheet (one row per T-code, in this order; unticked T-codes are
left out), the PDF and the report gate. A system with no entry gets every
task of config/monitoring_tasks.yaml in that file's order. A T-code added to
monitoring_tasks.yaml later is ON for every system until it is unticked.

A profile that names its own T-codes narrows the list further for its runs.
"""
from __future__ import annotations

import copy
import os
import threading

import yaml

from core import config_loader
from core.config_loader import CONFIG_DIR
from utils.logger import get_logger

log = get_logger(__name__)

PATH = os.path.join(CONFIG_DIR, "tcode_sets.yaml")
_lock = threading.Lock()

ST03_TYPES = ["DIALOG", "BACKGROUND", "RFC", "WS-RFC", "HTTP", "HTTPS", "UPDATE", "UPDATE2", "SPOOL",
              "ALE", "OTHER"]
ST02_BUFFERS = ["Program", "CUA", "Screen", "Tables Buffer", "Nametab Table definition",
                "Nametab Field definition", "Nametab Short NTAB", "Nametab Initial records",
                "Calendar", "OTR", "Export/import", "Exp./ Imp. SHM"]

# What can be set per system, with the defaults used when nothing is set.
SETTINGS: dict = {
    "ST03N": {
        "period": {"label": "Period", "type": "choice", "choices": ["today", "previous_day"],
                   "choice_labels": ["Today so far (all instances)", "Previous full day (total)"],
                   "default": "today"},
        "task_types": {"label": "Task types", "type": "multi", "choices": ST03_TYPES,
                       "default": ["DIALOG", "BACKGROUND", "WS-RFC"]},
    },
    "ST02": {
        "graded_buffers": {"label": "Buffers graded (hit ratio, swaps)", "type": "multi",
                           "choices": ST02_BUFFERS,
                           "default": ["Program", "CUA", "Screen", "Tables Buffer",
                                       "Nametab Table definition", "Nametab Field definition"]},
    },
    "ST04": {
        "top_n": {"label": "Large tables: how many (by memory and by records)", "type": "int",
                  "min": 1, "max": 50, "default": 10},
    },
    "STMS": {
        "connection_test": {"label": "Run SAP's connection test for each system", "type": "bool",
                            "default": True},
    },
}

LABELS = {
    "ST03N": "Workload overview: steps, total time, average time, CPU time per task type",
    "SMICM": "ICM services active",
    "ST02": "Buffers: hit ratio, swaps; SAP memory",
    "ST04": "Database: memory, data / log volume, large tables",
    "ST05": "Performance traces on / off, progress display",
    "STMS": "Transport domain: every system available",
    "STRUST": "Certificates: expiry date, days left",
}


def catalogue() -> list[dict]:
    """Every T-code that has a capture task (config/monitoring_tasks.yaml), in that file's order."""
    out = []
    for t in config_loader.get_monitoring_tasks():
        code = str(t.get("tcode") or "").strip().upper()
        if code:
            out.append({"tcode": code, "action": t.get("action", ""), "label": LABELS.get(code) or _sheet_name(code),
                        "settings": copy.deepcopy(SETTINGS.get(code, {}))})
    return out


def _sheet_name(code: str) -> str:
    """The monitoring sheet's name for the check (Check column)."""
    try:
        from reporting.excel_template_writer import _check_name
        name = _check_name(code)
        return "" if name == code else name
    except Exception:  # noqa: BLE001
        return ""


def _load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 -- a broken file must not stop the sweeps
        log.error(f"{PATH} could not be read ({type(exc).__name__}: {exc}); every T-code is used")
        return {}


def defaults(tcode: str) -> dict:
    return {k: copy.deepcopy(v["default"]) for k, v in SETTINGS.get(tcode, {}).items()}


def clean_settings(tcode: str, raw: dict | None) -> dict:
    """Only known settings, each checked; anything wrong falls back to its default."""
    out = defaults(tcode)
    for key, spec in SETTINGS.get(tcode, {}).items():
        if not raw or key not in raw:
            continue
        v = raw[key]
        if spec["type"] == "choice" and v in spec["choices"]:
            out[key] = v
        elif spec["type"] == "multi" and isinstance(v, list):
            picked = [str(x).strip() for x in v if str(x).strip()]
            if spec["choices"] and key == "task_types":
                picked = [x.upper() for x in picked]
            picked = [x for x in picked if x in spec["choices"]]
            if picked:
                out[key] = list(dict.fromkeys(picked))
        elif spec["type"] == "int":
            try:
                out[key] = max(spec["min"], min(spec["max"], int(v)))
            except (TypeError, ValueError):
                pass
        elif spec["type"] == "bool":
            out[key] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
    return out


# Read only from a SAP GUI screen (no RFC reading behind them).
GUI_ONLY = {"ST03N", "SMICM", "ST02", "ST04", "ST05", "STMS", "STRUST"}


def has_gui(system: str) -> bool:
    try:
        for s in config_loader.get_systems() or []:
            if str(s.get("name") or "") == system:
                return s.get("has_gui_access", True) is not False
    except Exception:  # noqa: BLE001
        pass
    return True


def for_system(system: str) -> list[dict]:
    """
    [{tcode, on, settings, label, action}] for the Profile page: the saved
    order first, then any T-code the system has not seen yet -- ON, except a
    screen-only check on a system without SAP GUI access.
    """
    cat = {c["tcode"]: c for c in catalogue()}
    gui = has_gui(system)
    saved = ((_load().get("systems") or {}).get(system) or {})
    off = {str(t).strip().upper() for t in saved.get("off") or []}
    out, seen = [], set()
    for item in saved.get("tcodes") or []:
        code = str((item or {}).get("tcode") if isinstance(item, dict) else item or "").strip().upper()
        if code in cat and code not in seen:
            seen.add(code)
            out.append({**cat[code], "on": code not in off,
                        "settings": clean_settings(code, item.get("settings") if isinstance(item, dict) else None),
                        "settings_spec": cat[code]["settings"]})
    for code, c in cat.items():
        if code not in seen:
            on = code not in off and (gui or code not in GUI_ONLY)
            out.append({**c, "on": on, "settings": defaults(code), "settings_spec": c["settings"]})
    return out


def tasks_for(system: str, only: list[str] | None = None) -> list[dict]:
    """
    The capture tasks of this system's sweep, in its order, each with its
    settings. `only`: a profile's own T-codes (narrows; never adds).
    """
    tasks = {str(t.get("tcode") or "").strip().upper(): t for t in config_loader.get_monitoring_tasks()}
    keep = {str(t).strip().upper() for t in only} if only else None
    out = []
    for item in for_system(system):
        if not item["on"] or (keep is not None and item["tcode"] not in keep):
            continue
        task = dict(tasks[item["tcode"]])
        if item.get("settings"):
            task["settings"] = dict(item["settings"])
        out.append(task)
    return out


def save(system: str, items: list[dict]) -> list[dict]:
    """Save the order, ticks and settings of one system. Unknown T-codes are refused."""
    if not str(system or "").strip():
        raise ValueError("system is required")
    cat = {c["tcode"] for c in catalogue()}
    clean, off, seen = [], [], set()
    for item in items or []:
        code = str((item or {}).get("tcode") or "").strip().upper()
        if code not in cat:
            raise ValueError(f"unknown T-code: {code or '(empty)'}")
        if code in seen:
            continue
        seen.add(code)
        entry = {"tcode": code}
        if SETTINGS.get(code):
            entry["settings"] = clean_settings(code, item.get("settings"))
        clean.append(entry)
        if not item.get("on", True):
            off.append(code)
    if clean and len(off) == len(clean):
        raise ValueError("at least one T-code must stay ticked")
    with _lock:
        data = _load()
        data.setdefault("systems", {})[system] = {"tcodes": clean, "off": off}
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("# T-codes per system -- edited on the Profile page (core/tcode_sets.py).\n")
            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)
        from utils.atomic import replace_or_overwrite   # Windows: file in use by readers
        replace_or_overwrite(tmp, PATH)
    log.info(f"{system}: T-code set saved ({len(clean) - len(off)} on, {len(off)} off)")
    return for_system(system)


def order_for(system: str, only: list[str] | None = None) -> list[str]:
    """The T-codes of this system's sheet, in order."""
    return [t["tcode"] for t in tasks_for(system, only)]
