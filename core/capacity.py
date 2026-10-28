"""
Capacity forecasting: "file system /usr/sap full in about 40 days at the
current growth", from the history already stored.

WHICH METRICS
Anything that fills up towards a fixed limit. Defaults (no config needed):
    disk_*            file system % used (SSH collector; CCMS RZ20 file
                      systems over RFC, collectors/ccms_os.py)  limit 100 %
    *used_pct         any "% used" metric                       limit 100 %
More, or other limits, in config/capacity.yaml:
    metrics:
      db.size_gb:      {limit: 2048, unit: " GB", label: "Database size"}
      "disk_*":        {limit: 95}          # plan for 95 %, not 100 %
    warn_days: 30

HOW (plain language)
Take the highest reading of each day for the last 60 days, draw the
straight line that fits those daily highs best, and see where that line
crosses the limit. "How well the line fits" (R^2, 0 to 1) decides how much
to trust it: a steady climb fits well; a value that jumps up and down does
not, and the forecast says so instead of giving a confident date.

Needs at least 7 days of readings spread over at least 7 days. A metric
that is flat or shrinking says "not growing".

ALERT
Once a day, a forecast that runs out within warn_days (default 30) with a
medium or high confidence mails Basis (kind "capacity", at most once a day
per metric) and goes on the timeline.
"""
from __future__ import annotations

import fnmatch
import os
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

DEFAULTS = {"disk_*": {"limit": 100.0, "unit": "%", "label": "File system"},
            "*used_pct": {"limit": 100.0, "unit": "%", "label": "Used"},
            "*_used_pct": {"limit": 100.0, "unit": "%", "label": "Used"}}


def _config() -> dict:
    try:
        from core.config_loader import CONFIG_DIR
        import yaml
        path = os.path.join(CONFIG_DIR, "capacity.yaml")
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"config/capacity.yaml unreadable: {exc}")
        return {}


def catalog() -> dict:
    cat = dict(DEFAULTS)
    for k, v in ((_config().get("metrics") or {}).items()):
        if isinstance(v, (int, float)):
            v = {"limit": float(v)}
        if isinstance(v, dict) and v.get("limit") is not None:
            cat[str(k)] = {**cat.get(str(k), {}), **v}
    return cat


def spec_for(metric: str) -> dict | None:
    cat = catalog()
    if metric in cat:
        return cat[metric]
    for pattern, spec in cat.items():
        if fnmatch.fnmatchcase(metric, pattern):
            return spec
    return None


def warn_days() -> float:
    try:
        return float(os.getenv("IBO_FORECAST_WARN_DAYS") or _config().get("warn_days") or 30)
    except (TypeError, ValueError):
        return 30.0


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least-squares line y = a + b*x. Returns (a, b, r2)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my, 0.0, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return a, b, max(0.0, min(1.0, r2))


def forecast_series(metric: str, series: list[tuple[datetime, float]], spec: dict,
                    now: datetime | None = None) -> dict:
    now = now or datetime.now()
    limit = float(spec["limit"])
    unit = spec.get("unit", "")
    daily: dict = {}
    for t, v in series:
        d = t.date()
        daily[d] = max(daily.get(d, v), v)
    days = sorted(daily)
    out = {"metric": metric, "limit": limit, "unit": unit, "label": spec.get("label") or metric,
           "days_of_data": len(days)}
    if len(days) < 7 or (days[-1] - days[0]).days < 6:
        out.update(state="learning", text=f"needs 7 days of readings (has {len(days)})")
        return out
    d0 = days[0]
    xs = [float((d - d0).days) for d in days]
    ys = [daily[d] for d in days]
    a, b, r2 = _fit(xs, ys)
    current = ys[-1]
    out.update(current=round(current, 2), per_day=round(b, 4), r2=round(r2, 2))
    if current >= limit:
        out.update(state="full", days_left=0, text=f"already at {current:g}{unit} (limit {limit:g}{unit})")
        return out
    span = max(1.0, xs[-1] - xs[0])
    if b <= max(1e-6, abs(limit) * 1e-5):
        out.update(state="stable", text=f"not growing ({current:g}{unit} now, limit {limit:g}{unit})")
        return out
    days_left = (limit - current) / b
    when = now + timedelta(days=days_left)
    conf = "high" if r2 >= 0.7 and span >= 21 else "medium" if r2 >= 0.4 else "low"
    out.update(state="growing", days_left=round(days_left, 1), date=when.strftime("%Y-%m-%d"),
               confidence=conf)
    growth = f"{b:.2f}{unit} a day" if abs(b) < 10 else f"{b:.0f}{unit} a day"
    if conf == "low":
        out["text"] = (f"rising about {growth}, but readings jump around too much for a reliable date "
                       f"(fit {r2:.2f}); {current:g}{unit} now")
    else:
        out["text"] = (f"full in about {days_left:.0f} days (around {when:%d %b}) at the current growth of "
                       f"{growth}; {current:g}{unit} now, limit {limit:g}{unit}; based on {len(days)} days")
    return out


def forecasts(system: str) -> list[dict]:
    from core.learned_baseline import _load_system
    data = _load_system(system, days=int(os.getenv("IBO_FORECAST_DAYS", "60") or 60))
    out = []
    for metric, series in sorted(data.items()):
        spec = spec_for(metric)
        if spec:
            f = forecast_series(metric, series, spec)
            f["system"] = system
            out.append(f)
    order = {"full": 0, "growing": 1, "learning": 2, "stable": 3}
    out.sort(key=lambda f: (order.get(f.get("state"), 9), f.get("days_left") or 1e9))
    return out


def check_all() -> int:
    """Daily: mail Basis about anything running out within warn_days. Returns mails attempted."""
    from core.config_loader import get_all_systems
    n = 0
    for cfg in get_all_systems():
        system = cfg.get("name")
        try:
            for f in forecasts(system):
                soon = (f.get("state") == "full" or
                        (f.get("state") == "growing" and f.get("confidence") in ("high", "medium")
                         and f.get("days_left", 1e9) <= warn_days()))
                if not soon:
                    continue
                n += 1
                _alert(cfg, f)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"{system}: capacity check failed: {exc}")
    return n


def _alert(cfg: dict, f: dict) -> None:
    from core import recipients
    from core.config_loader import get_smtp_config
    from notifications.mailer import send_html
    import html
    system = cfg.get("name")
    to = recipients.basis(cfg)
    subject = f"[CAPACITY] InfraBeatOps -- {system}: {f['label']} {f['metric']} {f['text'][:60]}"
    body = (f"<p><b>{html.escape(system)}: {html.escape(f['metric'])}</b></p>"
            f"<p>{html.escape(f['text'])}.</p>"
            f"<p>Forecast from the daily highest reading; fit {f.get('r2', '?')} "
            f"(1 = a perfectly steady climb). Plan the extension or the clean-up before then.</p>")
    send_html(subject=subject, html=body, to=to, smtp_config=get_smtp_config(cfg, require_recipients=False),
                     system=system, kind="capacity", key=f["metric"], min_interval_s=24 * 3600,
                     severity="WARNING")
