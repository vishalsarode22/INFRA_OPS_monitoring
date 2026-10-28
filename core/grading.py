"""
Grade one value against the limits for one system.

    grade_number("cpu", 87.0, "CEAT BW_PRD")
      -> {"status": "WARNING", "warning": 80, "critical": 90,
          "direction": "high", "enabled": True, "key": "cpu"}

Limits come from core/thresholds.py (this system's override, then
config/thresholds.yaml, then the catalogue default in
core/metric_catalogue.py, then `code_default` if the caller has one).

Rules -- the same everywhere this module is used:
  direction "high": critical when value >= critical (or > when the limit is
                    set "limit is NOT critical", i.e. inclusive: false);
                    warning when value >= warning.
  direction "low":  mirror image: critical when value <= critical (or <),
                    warning when value <= warning.
  enabled: false    the metric is switched off for this system: never
                    WARNING/CRITICAL (status NORMAL, enabled False).
  value None        UNKNOWN. Never NORMAL: unread is not healthy.

States (Dispatcher, ICM, gateway): grade_state(). "Running" is NORMAL, an
unreadable state is UNKNOWN, anything else gets the configured severity
("critical" / "warning"; "off" -> NORMAL).
"""
from __future__ import annotations

from core import metric_catalogue as cat

NORMAL, WARNING, CRITICAL, UNKNOWN = "NORMAL", "WARNING", "CRITICAL", "UNKNOWN"
_RANK = {UNKNOWN: 0, NORMAL: 1, WARNING: 2, CRITICAL: 3}


def worst(*statuses: str) -> str:
    """Worst of NORMAL/WARNING/CRITICAL; UNKNOWN only if nothing else is known."""
    known = [s for s in statuses if s in (NORMAL, WARNING, CRITICAL)]
    if not known:
        return UNKNOWN
    return max(known, key=lambda s: _RANK[s])


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _spec(key: str, system: str | None) -> dict:
    try:
        from core import thresholds as thr
        return thr.spec(key, system)
    except Exception:  # noqa: BLE001 -- a config problem must not stop grading
        return {}


def limits_for(key: str, system: str | None = None, code_default=None) -> dict:
    """{"warning", "critical", "inclusive", "enabled", "direction"} for this system."""
    s = _spec(key, system)
    w, c = cat.default_limits(key)
    if w is None and c is None and code_default:
        try:
            w, c = code_default
        except (TypeError, ValueError):
            pass
    if "warning" in s:
        w = _num(s["warning"])
    if "critical" in s:
        c = _num(s["critical"])
    return {"warning": _int(_num(w)), "critical": _int(_num(c)),
            "inclusive": bool(s.get("inclusive", True)),
            "enabled": s.get("enabled", True) is not False,
            "direction": cat.direction(key),
            "explicit": "warning" in s or "critical" in s}


def grade_number(key: str, value, system: str | None = None, code_default=None) -> dict:
    lim = limits_for(key, system, code_default)
    out = {"key": key, "value": value, **{k: lim[k] for k in
                                           ("warning", "critical", "direction", "enabled")}}
    v = _num(value)
    if v is None:
        out["status"] = UNKNOWN
        return out
    if not lim["enabled"]:
        out["status"] = NORMAL
        return out
    w, c, incl = lim["warning"], lim["critical"], lim["inclusive"]
    if lim["direction"] == cat.LOW:
        if c is not None and (v <= c if incl else v < c):
            out["status"] = CRITICAL
        elif w is not None and v <= w:
            out["status"] = WARNING
        else:
            out["status"] = NORMAL
    else:
        if c is not None and (v >= c if incl else v > c):
            out["status"] = CRITICAL
        elif w is not None and v >= w:
            out["status"] = WARNING
        else:
            out["status"] = NORMAL
    return out


def severity_for(key: str, system: str | None = None) -> str:
    """'critical' / 'warning' / 'off' for a state metric on this system."""
    s = _spec(key, system)
    if s.get("enabled") is False:
        return "off"
    raw = s.get("severity")
    if raw is False:                         # an unquoted "off" in YAML
        return "off"
    sev = str(raw or cat.default_severity(key)).strip().lower()
    return sev if sev in cat.STATE_SEVERITIES else "critical"


_TRANSITIONAL = ("starting", "stopping", "yellow")


def grade_state(key: str, label, system: str | None = None, reported: str | None = None) -> dict:
    """
    Grade a service state. `reported` is the collector's own status for it
    (NORMAL / WARNING / CRITICAL / UNKNOWN), when there is one.

      unreadable                       UNKNOWN
      "Running" (or reported NORMAL)   NORMAL
      starting / stopping / yellow     WARNING at most (a restart in progress
                                       is not an outage); NORMAL when "off"
      anything else (stopped, gray,    the severity set for this system:
      red)                             critical (default) / warning / off
    """
    sev = severity_for(key, system)
    text = str(label or "").strip()
    rep = str(reported or "").strip().upper()
    if not text or text.lower() in ("unknown", "…", "..."):
        status = UNKNOWN
    elif text.lower() == "running" or rep == NORMAL:
        status = NORMAL
    elif sev == "off":
        status = NORMAL
    elif text.lower() in _TRANSITIONAL or rep == WARNING:
        status = WARNING
    else:
        status = CRITICAL if sev == "critical" else WARNING
    return {"key": key, "value": text or None, "status": status, "severity": sev,
            "direction": cat.STATE, "enabled": sev != "off"}
