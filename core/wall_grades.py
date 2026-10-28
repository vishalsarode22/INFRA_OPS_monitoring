"""
Grade a live wall payload with the system's own limits.

Before this, CPU, memory, the work-process bar and the instance response were
coloured in the browser with fixed numbers (80/90, 80/90, 75/90, 400/1000 ms)
that the Thresholds page could not change. Now every value on the card is
graded here, on the server, with core/grading.py -- so a limit set on the
Thresholds page (for all systems or for one system) is what the wall shows,
what history stores and (stage 2) what alerts on.

grade_live_payload(payload) returns a COPY with:
    grades          {"cpu": {...}, "memory": {...}, "wp": {...}, "longest": {...},
                     "uptime": {...}, "dispatcher": {...}, "icm": {...},
                     "gateway": {...}, "free_wp": {"DIA": {...}, ...}}
    instances[i]    + "response_grade", "users_grade"
    overall         worst status of the card (UNKNOWN when not connected)
    grade_reasons   [{"status", "text", "key"}], worst first -- why the card
                    is amber/red
    graded_items    flat list of every graded value (for history and alerts)

It never reads SAP and never raises.
"""
from __future__ import annotations

from core import grading as g
from core import metric_catalogue as cat

# A service state taken from the last sweep (sapcontrol) is older than the
# live read; past this age it is shown but does not colour the card.
STALE_STATE_MINUTES = 15


def _memory_trusted(p: dict) -> bool:
    smon = p.get("smon") or {}
    if smon.get("memory_trusted") is True:
        return True
    if smon.get("memory_trusted") is False:
        return False
    return "incl. cache" not in str(smon.get("memory_basis") or "").lower()


def _fmt(v, unit=""):
    if v is None:
        return "—"
    if isinstance(v, float):
        v = round(v) if abs(v) >= 10 else round(v, 1)
    return f"{v}{unit}"


def grade_live_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    try:
        return _grade(payload)
    except Exception as exc:  # noqa: BLE001 -- the wall must render even if grading fails
        out = dict(payload)
        out["grades_error"] = f"{type(exc).__name__}: {exc}"
        return out


def _grade(payload: dict) -> dict:
    p = dict(payload)
    system = str(p.get("system") or "") or None
    p["instances"] = [dict(i) for i in (payload.get("instances") or []) if isinstance(i, dict)]
    grades: dict = {}
    items: list[dict] = []

    def add(name, grade, label, unit="", instance=None):
        grade = dict(grade)
        grade["label"] = label
        grade["unit"] = unit
        if instance:
            grade["instance"] = instance
        items.append(grade)
        return grade

    connected = bool(p.get("connected"))

    # ---- host ------------------------------------------------------------
    grades["cpu"] = add("cpu", g.grade_number("cpu", p.get("cpu"), system), "CPU", "%")
    mem = g.grade_number("memory", p.get("memory"), system)
    if not _memory_trusted(p) and p.get("memory") is not None:
        # With page cache counted as used, Linux/AIX sit near 100 % on a
        # healthy host: shown, never graded.
        mem["status"] = g.UNKNOWN
        mem["note"] = "includes reclaimable cache -- shown, not graded"
    grades["memory"] = add("memory", mem, "Memory", "%")

    # ---- work processes ----------------------------------------------------
    wp = p.get("work_processes") or {}
    if wp.get("available"):
        grades["wp"] = add("sap.sm50.wp_utilization",
                           g.grade_number("sap.sm50.wp_utilization", wp.get("utilization_pct"), system),
                           "Work processes in use", "%")
        free = {}
        for wtype, slot in (wp.get("by_type") or {}).items():
            key = f"sap.wp.free.{wtype}"
            if key not in cat.CATALOGUE:
                continue
            lim = g.limits_for(key, system)
            if lim["warning"] is None and lim["critical"] is None:
                continue          # not graded until someone sets a limit
            free[wtype] = add(key, g.grade_number(key, (slot or {}).get("free"), system),
                              f"Free {wtype} work processes")
        if free:
            grades["free_wp"] = free
        lr = wp.get("longest_running") or {}
        minutes = round(lr["seconds"] / 60, 1) if isinstance(lr.get("seconds"), (int, float)) else 0
        grades["longest"] = add("sap.sm50.longest_running_min",
                                g.grade_number("sap.sm50.longest_running_min", minutes, system),
                                "Longest-running work process", " min")

    # ---- availability -------------------------------------------------------
    for field, key, label in (("dispatcher", "sap.dispatcher.state", "Dispatcher"),
                              ("icm", "sap.icm.state", "ICM"),
                              ("gateway", "sap.gateway.state", "Gateway")):
        state = p.get(field) or {}
        if not isinstance(state, dict):
            continue
        sg = g.grade_state(key, state.get("label"), system, state.get("status"))
        age = state.get("age_minutes") if "age_minutes" in state else None
        if "age_minutes" in state and (age is None or age > STALE_STATE_MINUTES):
            # Carried over from the last sweep (sapcontrol), not read now:
            # shown on its tile, but it does not colour the card or alert.
            sg["stale"] = True
        grades[field] = add(key, sg, label)

    up = p.get("uptime_seconds")
    if isinstance(up, (int, float)):
        ug = g.grade_number("sap.uptime_minutes", round(up / 60), system)
        if p.get("uptime_bounded") and ug["status"] in (g.WARNING, g.CRITICAL):
            # The figure is only a lower bound ("at least N minutes"): it
            # cannot prove a recent restart.
            ug["status"] = g.NORMAL
            ug["note"] = "lower bound only -- not graded"
        grades["uptime"] = add("sap.uptime_minutes", ug, "Time since restart", " min")

    # ---- instances -----------------------------------------------------------
    users_lim = g.limits_for("sap.instance.users", system)
    for inst in p["instances"]:
        name = inst.get("name") or inst.get("host") or "?"
        resp = inst.get("response_ms")
        rg = g.grade_number("sap.smlg.response_time", resp, system)
        if inst.get("response_low_confidence") and rg["status"] in (g.WARNING, g.CRITICAL):
            rg["status"] = g.NORMAL
            rg["note"] = inst.get("response_confidence_note") or "too few dialog steps to grade"
        if resp is None:
            rg["status"] = g.UNKNOWN
        inst["response_grade"] = add("sap.smlg.response_time", rg,
                                     f"Response {name}", " ms", instance=name)
        if users_lim["warning"] is not None or users_lim["critical"] is not None:
            inst["users_grade"] = add("sap.instance.users",
                                      g.grade_number("sap.instance.users", inst.get("users"), system),
                                      f"Users {name}", instance=name)

    # ---- T-code checks (already graded by the collectors, per system) --------
    for c in p.get("checks") or []:
        st = str(c.get("status") or "").upper()
        if st in (g.WARNING, g.CRITICAL, g.NORMAL):
            items.append({"key": c.get("metric") or c.get("tcode") or "check",
                          "label": c.get("label") or c.get("metric") or "check",
                          "value": c.get("value"), "status": st,
                          "warning": c.get("warning"), "critical": c.get("critical"),
                          "direction": cat.direction(str(c.get("metric") or "")), "unit": "",
                          "detail": str(c.get("detail") or "")[:300], "from_check": True})

    # ---- the card ------------------------------------------------------------
    reasons = []
    for it in items:
        if it.get("stale"):
            continue
        if it["status"] in (g.WARNING, g.CRITICAL):
            unit = it.get("unit") or ""
            if it.get("direction") == cat.STATE:
                text = f"{it['label']} {it.get('value') or 'not running'}"
            elif it.get("from_check"):
                text = f"{it['label']} {str(it.get('value') or '').replace(' count', '')}".strip()
            else:
                text = f"{it['label']} {_fmt(it.get('value'), unit)}"
            reasons.append({"status": it["status"], "text": text, "key": it["key"]})
    reasons.sort(key=lambda r: 0 if r["status"] == g.CRITICAL else 1)

    p["grades"] = grades
    p["graded_items"] = items
    p["grade_reasons"] = reasons
    p["overall"] = g.worst(*[it["status"] for it in items if not it.get("stale")]) \
        if connected else g.UNKNOWN
    if connected and p["overall"] == g.UNKNOWN:
        p["overall"] = g.NORMAL
    return p

