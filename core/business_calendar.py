"""
Business calendar: month-end, year-end, holidays and campaign days are not
"normal" days, and the learned baselines (core/learned_baseline.py) must
not judge them -- or be trained by them -- as if they were.

config/business_calendar.yaml (copy the .example):
    weekend: [sat, sun]
    holidays: ["2026-10-20", "2026-11-08"]          # treated like a weekend day
    month_end_working_days: 2                       # last 2 working days of the month
    month_start_working_days: 1                     # first working day
    fiscal_year_end: "03-31"                        # India; the last 5 working days before it are special
    special_days: ["2026-11-27"]                    # sales campaign, migration weekend, ...

day_class(date) -> "holiday" | "special" | "weekend" | "workday"
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from functools import lru_cache

from utils.logger import get_logger

log = get_logger(__name__)

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_cfg_cache: dict = {"stamp": None, "data": {}}


def config() -> dict:
    try:
        from core.config_loader import CONFIG_DIR
        path = os.path.join(CONFIG_DIR, "business_calendar.yaml")
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size)
        if _cfg_cache["stamp"] != stamp:
            import yaml
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            _cfg_cache.update(stamp=stamp, data=data if isinstance(data, dict) else {})
            _classify.cache_clear()
        return _cfg_cache["data"]
    except OSError:
        if _cfg_cache["stamp"] is not None:
            _cfg_cache.update(stamp=None, data={})
            _classify.cache_clear()
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"config/business_calendar.yaml unreadable: {exc}")
        return {}


def _dates(key: str, cfg: dict) -> set[date]:
    out = set()
    for d in cfg.get(key) or []:
        try:
            out.add(d if isinstance(d, date) else datetime.strptime(str(d)[:10], "%Y-%m-%d").date())
        except ValueError:
            continue
    return out


def _weekend(cfg: dict) -> set[int]:
    names = cfg.get("weekend") or ["sat", "sun"]
    return {_DAYS.index(str(n).lower()[:3]) for n in names if str(n).lower()[:3] in _DAYS}


def _is_working(d: date, cfg: dict) -> bool:
    return d.weekday() not in _weekend(cfg) and d not in _dates("holidays", cfg)


def _working_days_of_month(year: int, month: int, cfg: dict) -> list[date]:
    d = date(year, month, 1)
    out = []
    while d.month == month:
        if _is_working(d, cfg):
            out.append(d)
        d += timedelta(days=1)
    return out


@lru_cache(maxsize=4096)
def _classify(d: date, stamp) -> str:
    cfg = _cfg_cache["data"] or {}
    if d in _dates("holidays", cfg):
        return "holiday"
    if d in _dates("special_days", cfg):
        return "special"
    if d.weekday() in _weekend(cfg):
        return "weekend"
    if cfg:
        wd = _working_days_of_month(d.year, d.month, cfg)
        n_end = int(cfg.get("month_end_working_days", 0) or 0)
        n_start = int(cfg.get("month_start_working_days", 0) or 0)
        if (n_end and d in wd[-n_end:]) or (n_start and d in wd[:n_start]):
            return "special"
        fy = str(cfg.get("fiscal_year_end") or "").strip()
        if fy:
            try:
                m, day = (int(x) for x in fy.split("-"))
                fye = date(d.year, m, day)
                window = [x for x in (fye - timedelta(days=i) for i in range(0, 12)) if _is_working(x, cfg)][:5]
                if d in window:
                    return "special"
            except ValueError:
                pass
    return "workday"


_checked = {"at": -1e9}


def day_class(d) -> str:
    """Called for every history sample, so the file is re-checked at most every 30 s."""
    import time
    if isinstance(d, datetime):
        d = d.date()
    now = time.monotonic()
    if now - _checked["at"] > 30:
        _checked["at"] = now
        config()
    return _classify(d, _cfg_cache["stamp"])


def configured() -> bool:
    return bool(config())
