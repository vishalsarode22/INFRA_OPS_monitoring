"""
Tell "the parser broke" apart from "the metric is missing".

The screenshot path depends on SAP GUI theme, font, DPI scaling, column
layout and language. When any of those changes, OCR and scripting still
"succeed" -- the screenshot is taken -- but extract_patterns() returns
nothing, and every downstream reader sees an absent metric. Nobody is
told; the Excel sheet just has a blank cell.

This keeps, per system and T-code, how many data fields extraction
produced on its best day. A capture that produced zero where it used to
produce several is reported as BROKEN; one that produced less than half
is DEGRADED. The sweep attaches the verdict to the evidence record and
mails Basis once (ledger-suppressed) so a person looks at the screenshot
before the blank cells become a trend.

State: dashboard/parser_health.json
    {"<system>": {"<TCODE>": {"best": 7, "last": 0, "zero_streak": 2,
                               "last_ok": "2026-10-05 10:00:00"}}}
"""
from __future__ import annotations

import os
import threading
from datetime import datetime

from core.atomic_json import locked, read_json, write_json

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(_BASE, "dashboard", "parser_health.json")
_lock = threading.Lock()

# Keys the collector adds that are not extracted data.
_META_KEYS = {"evidence_id", "attempts", "system", "client", "recovery_actions", "started_at",
              "finished_at", "ocr_excerpt", "ocr_lines", "ocr_skipped", "analysis", "screenshot",
              "screenshots", "notes", "error", "parser_health"}


def extracted_field_count(extra_data: dict | None) -> int:
    """How many real data fields a T-code capture produced."""
    n = 0
    for key, value in (extra_data or {}).items():
        if key in _META_KEYS or str(key).startswith("_"):
            continue
        if value in (None, "", [], {}):
            continue
        n += 1
    return n


def assess(system: str, results: list) -> dict:
    """
    Update the baseline from this sweep's results and return the verdict:
        {"broken": [{"tcode", "best", "now"}], "degraded": [...], "checked": n}
    `results` are the MetricResult objects from collect_tcode_evidence.
    """
    verdict = {"broken": [], "degraded": [], "checked": 0}
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock, locked(STATE_PATH):
        state = read_json(STATE_PATH, default={}) or {}
        bucket = state.setdefault(system, {})
        for item in results or []:
            tcode = (getattr(item, "tcode", "") or "").upper()
            display = str(getattr(item, "display_value", "") or "")
            if not tcode or not display.startswith("captured"):
                continue   # a failed capture is already reported elsewhere
            count = extracted_field_count(getattr(item, "extra_data", None))
            entry = bucket.setdefault(tcode, {"best": 0, "last": 0, "zero_streak": 0, "last_ok": ""})
            best = int(entry.get("best", 0))
            verdict["checked"] += 1
            if count == 0 and best >= 1:
                entry["zero_streak"] = int(entry.get("zero_streak", 0)) + 1
                verdict["broken"].append({"tcode": tcode, "best": best, "now": 0,
                                          "streak": entry["zero_streak"]})
            elif best >= 2 and count < best / 2:
                entry["zero_streak"] = 0
                verdict["degraded"].append({"tcode": tcode, "best": best, "now": count})
            else:
                entry["zero_streak"] = 0
                if count > 0:
                    entry["last_ok"] = now_text
            entry["last"] = count
            entry["best"] = max(best, count)
            extra = getattr(item, "extra_data", None)
            if isinstance(extra, dict):
                extra["parser_health"] = (
                    "broken" if any(b["tcode"] == tcode for b in verdict["broken"]) else
                    "degraded" if any(d["tcode"] == tcode for d in verdict["degraded"]) else "ok")
                if extra["parser_health"] != "ok":
                    note = (f"PARSER CHECK: extracted {count} field(s), previously up to {best}. "
                            f"Screenshot captured fine -- a GUI theme, scaling, column or language "
                            f"change may have broken extraction. Open the screenshot.")
                    try:
                        item.detail = f"{getattr(item, 'detail', '') or ''} | {note}".strip(" |")
                    except Exception:
                        pass
        write_json(STATE_PATH, state)
    return verdict


def reset(system: str | None = None, tcode: str | None = None) -> None:
    """After a deliberate pattern change, forget the old baseline."""
    with _lock, locked(STATE_PATH):
        state = read_json(STATE_PATH, default={}) or {}
        if system is None:
            state = {}
        elif tcode is None:
            state.pop(system, None)
        else:
            (state.get(system) or {}).pop(tcode.upper(), None)
        write_json(STATE_PATH, state)


def snapshot() -> dict:
    return read_json(STATE_PATH, default={}) or {}
