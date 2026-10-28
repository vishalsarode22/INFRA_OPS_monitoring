"""
Attribution -- the "which / who / why" behind the counters.

A count of 54 dumps is not something a person can act on. This module turns
the live RFC payload and the collectors' extra_data into named things:

  dumps           who dumped, on which host, in which program (when known),
                  routed to a team (custom Z*/Y* -> ABAP, else Basis/SAP)
  jobs            which job failed, whose it is, and WHY (never started vs.
                  died mid-run), which are long-running and for how long
  work processes  which report a long-running / PRIV work process is in,
                  and under which user
  response time   which users and reports carry the dialog response, per
                  instance, and the DB-heavy ones
  locks           who holds the most / oldest locks, and duplicate-object
                  contention

Everything here is deterministic and computed BEFORE the model is called.
The model is handed this block to explain; it cannot invent a culprit the
block does not contain. Where a fact is unavailable over RFC the block says
so in `gaps`, so "unknown" is stated rather than filled in.
"""

from __future__ import annotations

from typing import Any

try:
    from core.dump_attribution import is_custom
except Exception:  # pragma: no cover - keep attribution usable standalone
    def is_custom(program: str) -> bool:
        p = (program or "").strip().upper()
        return p.startswith(("Z", "Y", "/")) and not p.startswith("/SDF/")


def _team_for_program(program: str) -> str:
    if not program:
        return "unknown"
    return "ABAP (custom code)" if is_custom(program) else "Basis / SAP standard"


def _get(m, attr, default=None):
    """Field of a MetricResult OR of a serialized rfc_metrics row."""
    if m is None:
        return default
    if isinstance(m, dict):
        if attr == "status":
            return m.get("status", default)
        if attr == "extra_data":
            return m.get("extra") or m.get("extra_data") or {}
        return m.get(attr, default)
    v = getattr(m, attr, default)
    if attr == "status" and v is not None and hasattr(v, "value"):
        return v.value
    return v


def _extra(metrics: dict, name: str, key: str, default=None):
    m = metrics.get(name)
    if m is None:
        return default
    return (_get(m, "extra_data") or {}).get(key, default)


def build_attribution(payload: dict, metrics: list) -> dict[str, Any]:
    """payload = rfc_live.read_live(...) result; metrics = MetricResult list."""
    # The live payload's rfc_metrics rows carry the collectors' full
    # extra_data (top users, PRIV rows, lock owners); the MetricResult list
    # built from the compact checklist does not. Prefer the rich rows.
    by_name: dict = {}
    for m in metrics or []:
        by_name[_get(m, "name")] = m
    for row in payload.get("rfc_metrics") or []:
        if isinstance(row, dict) and row.get("metric"):
            by_name[row["metric"]] = row
    gaps: list[str] = []
    out: dict[str, Any] = {"dumps": {}, "jobs": {}, "work_processes": {},
                           "response": {}, "locks": {}, "gaps": gaps}

    # ---- dumps -----------------------------------------------------------
    dd = payload.get("dump_detail") or {}
    st22 = by_name.get("sap.st22.dumps")
    total = dd.get("count")
    if total is None and st22 is not None:
        total = int(_get(st22, "value", 0) or 0)
    programs = [{"program": p["program"], "count": p["count"], "team": _team_for_program(p["program"])}
                for p in (dd.get("by_program") or [])]
    teams: dict[str, int] = {}
    for p in programs:
        teams[p["team"]] = teams.get(p["team"], 0) + p["count"]
    out["dumps"] = {
        "total_today": total or 0,
        "by_user": dd.get("by_user") or [],
        "by_host": dd.get("by_host") or [],
        "by_program": programs,
        "by_team": [{"team": t, "count": n} for t, n in sorted(teams.items(), key=lambda kv: -kv[1])],
        "recent": dd.get("recent") or [],
        "program_source": dd.get("program_source"),
    }
    if (total or 0) > 0 and not programs:
        gaps.append(dd.get("note") or "dump program / runtime-error names not available over RFC")

    # ---- jobs ------------------------------------------------------------
    jd = payload.get("job_detail") or {}
    cancelled = []
    for j in jd.get("cancelled") or []:
        why = ("never started -- ended at its start time (authorisation, missing variant, or no free BGD work process)"
               if j.get("never_started") else
               f"ran {j.get('duration_text') or '?'} then cancelled -- failure inside the job (ST22 dump at {j.get('ended_at') or '?'} or job log in SM37)")
        cancelled.append({"job": j.get("job"), "owner": j.get("owner"), "owner_email": j.get("owner_email"),
                          "step_user": j.get("user"), "scheduled_by": j.get("scheduled_by"),
                          "started_at": j.get("started_at"), "ended_at": j.get("ended_at"),
                          "duration": j.get("duration_text"), "is_backup": bool(j.get("is_backup")),
                          "why": why})
    long_running = [{"job": j.get("job"), "owner": j.get("owner"), "owner_email": j.get("owner_email"),
                     "step_user": j.get("user"), "started_at": j.get("started_at"),
                     "running_for": j.get("duration_text"),
                     "why": "long-running -- confirm in SM50/SM66 whether it is working (DB activity, CPU) or blocked (waiting on a lock, RFC, or dialog step) before cancelling"}
                    for j in jd.get("long_running") or []]
    out["jobs"] = {"cancelled": cancelled, "long_running": long_running,
                   "backup_failed": any(c["is_backup"] for c in cancelled)}
    if not jd:
        gaps.append("job detail (TBTCO) not read this cycle")

    # ---- work processes ----------------------------------------------------
    wp = payload.get("work_processes") or {}
    priv = _extra(by_name, "sap.sm50.priv_mode_wp", "priv", []) or []
    long_rows = _extra(by_name, "sap.sm50.long_running_wp", "long_running", []) or []

    def _why_wp(w: dict) -> str:
        act = (w.get("action") or "").strip()
        rsn = (w.get("reason") or "").strip()
        tbl = (w.get("table") or "").strip()
        if rsn:
            return f"waiting on {rsn}" + (f" (table {tbl})" if tbl else "") + " -- blocked, not working"
        if act:
            return f"currently {act}" + (f" on {tbl}" if tbl else "") + " -- working, but heavy"
        return "occupying a work process longer than a dialog step should; SM50 shows the current action"

    def _fmt_s(sec) -> str:
        try:
            sec = int(sec or 0)
        except (TypeError, ValueError):
            return "?"
        return f"{sec // 60}m {sec % 60}s" if sec >= 60 else f"{sec}s"

    lr = [{"wp": w.get("wp_no"), "instance": w.get("instance"), "user": w.get("user"),
           "client": w.get("client"), "report": w.get("report"),
           "team": _team_for_program(w.get("report") or ""),
           "elapsed": _fmt_s(w.get("elapsed_s")), "status": w.get("status"),
           "why": _why_wp(w)} for w in long_rows]
    longest = wp.get("longest_running") or {}
    if longest and not lr:
        lr = [{"wp": longest.get("wp"), "user": longest.get("user"), "report": longest.get("report"),
               "team": _team_for_program(longest.get("report") or ""),
               "elapsed": _fmt_s(longest.get("seconds")), "why": "longest-running work process right now"}]
    out["work_processes"] = {
        "long_running": lr,
        "priv_mode": [{"wp": p.get("wp_no"), "instance": p.get("instance"), "user": p.get("user"),
                       "report": p.get("report"), "team": _team_for_program(p.get("report") or ""),
                       "elapsed": _fmt_s(p.get("elapsed_s")),
                       "why": "PRIV mode -- the work process took private memory; it is not freed until the step ends and starves other users. " + _why_wp(p)}
                      for p in priv],
        "in_use": wp.get("in_use"), "total": wp.get("total"),
    }

    # ---- response time -----------------------------------------------------
    resp = by_name.get("sap.st03.dialog_resp_ms")
    if resp is not None:
        ex = _get(resp, "extra_data") or {}
        top_users = ex.get("top_users_by_total_ms") or []
        top_resp = ex.get("top_reports_by_response") or []
        top_db = ex.get("top_reports_by_db_time") or []
        top_mem = ex.get("top_reports_by_memory") or []
        def team(r): return _team_for_program((r.get("report") or r.get("program") or "") if isinstance(r, dict) else "")
        out["response"] = {
            "avg_ms": _get(resp, "value"), "status": _get(resp, "status"),
            "window": ex.get("window"), "steps": ex.get("steps"), "low_sample": ex.get("low_sample", False),
            "per_instance": ex.get("per_instance") or {},
            "db_share_pct": _get(by_name.get("sap.st03.db_time_pct"), "value"),
            "top_users": top_users[:5],
            "top_reports_by_response": [dict(r, team=team(r)) for r in top_resp[:5]],
            "top_reports_by_db_time": [dict(r, team=team(r)) for r in top_db[:5]],
            "top_reports_by_memory": [dict(r, team=team(r)) for r in top_mem[:5]],
            "expensive_sql": _extra(by_name, "sap.sqlm.expensive_programs", "programs", []) or [],
        }
        if ex.get("low_sample"):
            gaps.append(f"response average is over only {ex.get('steps')} dialog step(s) -- too few to blame anyone")
        if not (top_users or top_resp or top_db):
            gaps.append("no per-user / per-report response breakdown in this window (ST03N aggregate or idle system)")

    # ---- locks -------------------------------------------------------------
    lk = by_name.get("sap.sm12.locks_per_user_max")
    old = by_name.get("sap.sm12.oldest_lock_minutes")
    if lk is not None or old is not None:
        out["locks"] = {
            "top_users": _extra(by_name, "sap.sm12.locks_per_user_max", "top_users", []) or [],
            "same_object_dups": _extra(by_name, "sap.sm12.locks_per_user_max", "same_object_dups", []) or [],
            "oldest_minutes": _get(old, "value"),
            "oldest_detail": _get(old, "detail"),
            "clock_offset_min": _extra(by_name, "sap.sm12.oldest_lock_minutes", "owner_clock_offset_min", None),
            "why": "old locks usually mean a user session that ended without releasing (network drop, killed GUI) or a long-running update; SM12 shows the owner and the transaction",
        }

    return out
