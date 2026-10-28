"""
Keep the AI on the free Gemini tier: run it only when the answer can change.

    ST22 analysis      once per system per SAP day, again only when the dump
                       count crosses into a higher band (1, 10, 50, 200, 1000
                       -- IBO_AI_ST22_BANDS). Ten dumps at 09:00 and twelve at
                       09:05 are the same picture; 60 dumps is a new one.
    report summaries   the AI paragraph of a report page is cached per system
                       and snapshot time: reloading the page costs nothing.
                       A live report (new timestamp every call) is limited to
                       one AI summary per system per IBO_AI_SUMMARY_MIN_MINUTES
                       (default 30); in between the rules-written text is used.

State is on disk (logs/ai_budget.json), shared by every process, so a
restart does not spend the budget again. Never raises: a broken state file
means "run it", the same as before this module.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__, "ai_provider")

_lock = threading.Lock()


def _path() -> str:
    try:
        from utils.logger import LOG_DIR
        base = LOG_DIR
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    return os.path.join(str(base), "ai_budget.json")


PATH = None          # tests may point this at a temp file


def _file() -> str:
    return PATH or _path()


def _load() -> dict:
    try:
        with open(_file(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        from utils.atomic import atomic_write_json
        os.makedirs(os.path.dirname(_file()), exist_ok=True)
        atomic_write_json(_file(), data)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"AI budget state not saved: {exc}")


# ------------------------------------------------------------------ ST22

def _bands() -> list[int]:
    raw = os.getenv("IBO_AI_ST22_BANDS", "1,10,50,200,1000")
    out = []
    for part in str(raw).split(","):
        try:
            out.append(int(part.strip()))
        except ValueError:
            continue
    return sorted(set(b for b in out if b > 0)) or [1, 10, 50, 200, 1000]


def st22_band(count) -> int:
    """0 for no dumps, else the index (1-based) of the highest band reached."""
    try:
        n = int(float(count or 0))
    except (TypeError, ValueError):
        return 0
    return sum(1 for b in _bands() if n >= b)


def _sap_day(system: str) -> str:
    try:
        from core.sap_clock import sap_today
        return sap_today(system)
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d")


def st22_due(system: str, count, day: str | None = None) -> bool:
    """True when an ST22 AI analysis would say something new for this system today."""
    band = st22_band(count)
    if band <= 0:
        return False
    day = day or _sap_day(system)
    with _lock:
        rec = (_load().get("st22") or {}).get(str(system)) or {}
    return not (rec.get("day") == day and int(rec.get("band") or 0) >= band)


def st22_mark(system: str, count, day: str | None = None) -> None:
    day = day or _sap_day(system)
    with _lock:
        data = _load()
        data.setdefault("st22", {})[str(system)] = {"day": day, "band": st22_band(count),
                                                    "count": count,
                                                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        _save(data)


# ------------------------------------------------------------------ summaries

def _summary_min_minutes() -> float:
    try:
        return max(0.0, float(os.getenv("IBO_AI_SUMMARY_MIN_MINUTES", "30") or 30))
    except ValueError:
        return 30.0


def summary_get(system: str, stamp: str) -> str | None:
    with _lock:
        rec = (_load().get("summaries") or {}).get(f"{system}|{stamp}")
    return (rec or {}).get("text") if rec else None


def summary_allowed(system: str) -> bool:
    """A new AI summary for this system may be made now (rate limit for live reports)."""
    with _lock:
        last = (_load().get("summary_last") or {}).get(str(system))
    if not last:
        return True
    try:
        at = datetime.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return datetime.now() - at >= timedelta(minutes=_summary_min_minutes())


def summary_put(system: str, stamp: str, text: str | None) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        data = _load()
        cache = data.setdefault("summaries", {})
        cache[f"{system}|{stamp}"] = {"text": text, "at": now}
        if len(cache) > 100:
            for k in sorted(cache, key=lambda k: str(cache[k].get("at")))[:len(cache) - 100]:
                cache.pop(k, None)
        data.setdefault("summary_last", {})[str(system)] = now
        _save(data)
