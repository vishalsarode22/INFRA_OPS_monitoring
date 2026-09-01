"""
Live RFC read for the operations wall display.

WHY THIS IS SEPARATE FROM THE SWEEP
-----------------------------------
The monitoring sweep is heavy and slow: it drives SAP GUI, captures
screenshots, runs OCR, writes evidence, builds reports and calls the AI
layer. It is meant to run on a dedicated monitoring machine every few hours.

A wall display needs the opposite: a small, fast, read-only snapshot every
few seconds, with no side effects. Running the sweep to refresh a screen
would relaunch SAP Logon and kill the operator's own GUI session.

So this module does one thing: open one RFC connection, read the handful of
values the wall display shows, close it. It writes nothing, triggers no
alerts, creates no events and produces no evidence.

WHAT IT READS
-------------
    instances        TH_SERVER_LIST      name, host, type per app server
    work processes   TH_WPINFO           total / in use / free, per type
    dispatcher, ICM  sapcontrol via Z FM or SXPG
    CPU / memory     Z_GET_OBSERVABILITY_DATA (SXPG -> /proc)
    response time    NOT available over RFC -- see below

RESPONSE TIME
-------------
Per-instance dialog response time (what SMLG shows) lives in the ST03
workload statistics, which need the SAP_COLLECTOR_FOR_PERFMONITOR job and
the SAPWL_* function modules. That interface was not confirmed on this
release, so this module does NOT invent a number for it.

Instead `response_time` is read from the most recent monitoring snapshot,
where the SAP GUI collector stores `sap.smlg.response_time`, and is returned
WITH ITS AGE. A stale number labelled stale is useful; a stale number
presented as live is not.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from collectors.rfc_collector import (
    SapSession,
    cooldown_remaining,
    _from_function_module,
    _from_standard_modules,
)

# One live read per system is cached briefly. A wall display polling every
# 5s across 4 systems would otherwise open 48 RFC logons a minute, which is
# the audit-log and account-lockout problem the sweep already had to solve.
_CACHE_TTL_SECONDS = 8
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _cached(system: str):
    with _lock:
        hit = _cache.get(system)
    if not hit:
        return None
    age, payload = time.monotonic() - hit[0], hit[1]
    if age > _CACHE_TTL_SECONDS:
        return None
    out = dict(payload)
    out["cache_age_seconds"] = round(age, 1)
    return out


def _store(system: str, payload: dict):
    with _lock:
        _cache[system] = (time.monotonic(), payload)


# Human labels for the T-code counters, so the wall reads like a Basis
# checklist rather than a list of metric keys.
_CHECK_LABELS = {
    "sap.st22.dumps":          ("ST22",  "ABAP dumps today"),
    "sap.sm12.lock_count":     ("SM12",  "Lock entries"),
    "sap.sm13.failed_updates": ("SM13",  "Failed updates"),
    "sap.sm37.cancelled_jobs": ("SM37",  "Cancelled jobs"),
    "sap.sm37.active_jobs":    ("SM37",  "Running jobs"),
    "sap.sm58.stuck_entries":  ("SM58",  "Stuck tRFC"),
    "sap.smq1.entries":        ("SMQ1",  "Outbound queues"),
    "sap.smq2.entries":        ("SMQ2",  "Inbound queues"),
    "sap.we02.failed_idocs":   ("WE02",  "Failed IDocs"),
    "sap.sm21.errors":         ("SM21",  "Log errors"),
    "sap.al08.user_logons":    ("AL08",  "User sessions"),
    "sap.su01.locked_users":   ("SU01",  "Locked users"),
    "sap.sm50.free_dia_wp":    ("SM50",  "Free dialog WP"),
    "sap.sm50.total_dia_wp":   ("SM50",  "Total dialog WP"),
    "sap.sm66.wp_saturation_pct": ("SM66", "WP saturation"),
    "sap.db12.last_backup":    ("DB12",  "Last backup"),
}

# Shown as tiles at the top of a card; excluded from the checklist grid so
# nothing appears twice.
_TILE_METRICS = {"cpu", "memory", "memory.total_gb", "load_1m",
                 "sap.sm50.free_dia_wp", "sap.sm50.total_dia_wp"}


def _checks_from_metrics(metrics) -> list[dict]:
    """
    Converts the RFC collector's MetricResult list into compact rows for the
    wall.

    Reuses the collector rather than re-querying, so the live view and the
    sweep grade identically -- two code paths reading the same counter with
    different thresholds is how a dashboard starts disagreeing with its own
    reports.
    """
    rows = []
    for m in metrics:
        if m.name in _TILE_METRICS:
            continue
        tcode, label = _CHECK_LABELS.get(m.name, (m.tcode or "—", m.name))
        rows.append({
            "metric": m.name,
            "tcode": tcode,
            "label": label,
            "value": m.display_value,
            "status": m.status.value,
            "detail": (m.detail or "")[:160],
        })
    rows.sort(key=lambda r: ({"CRITICAL": 0, "WARNING": 1, "NORMAL": 2,
                              "UNKNOWN": 3}.get(r["status"], 4), r["tcode"]))
    return rows


def _process_state(raw) -> tuple[str, str]:
    """Maps a sapcontrol colour to (label, status)."""
    text = str(raw or "").strip().upper()
    if text in ("GREEN", "RUNNING", "OK"):
        return "Running", "NORMAL"
    if text in ("YELLOW", "STARTING", "STOPPING"):
        return text.title(), "WARNING"
    if text in ("RED", "STOPPED", "GRAY"):
        return text.title(), "CRITICAL"
    return "Unknown", "UNKNOWN"


def _work_processes(session: SapSession) -> dict:
    """
    Total / in use / free work processes, overall and per type.

    A bare "free" count is not actionable without the denominator: 3 free is
    healthy on a 20-WP system and an emergency on a 4-WP one.
    """
    result = session.call("TH_WPINFO")
    if result is None:
        return {"available": False}

    rows = result.get("WPLIST", []) or []
    if not rows:
        return {"available": False}

    def busy(row) -> bool:
        return not str(row.get("WP_STATUS", "")).strip().lower().startswith("wait")

    by_type: dict[str, dict] = {}
    for row in rows:
        wp_type = str(row.get("WP_TYP", "")).strip().upper() or "OTHER"
        slot = by_type.setdefault(wp_type, {"total": 0, "in_use": 0})
        slot["total"] += 1
        if busy(row):
            slot["in_use"] += 1

    total = len(rows)
    in_use = sum(1 for r in rows if busy(r))

    longest = None
    for row in rows:
        if not busy(row):
            continue
        try:
            seconds = int(str(row.get("WP_ELTIME", "") or 0).strip() or 0)
        except ValueError:
            continue
        if longest is None or seconds > longest["seconds"]:
            longest = {
                "seconds": seconds,
                "wp": str(row.get("WP_NO", "?")).strip(),
                "user": str(row.get("WP_USER", "") or "").strip(),
                "report": str(row.get("WP_REPORT", "") or "").strip(),
            }

    return {
        "available": True,
        "total": total,
        "in_use": in_use,
        "free": total - in_use,
        "utilization_pct": round(in_use / total * 100) if total else None,
        "by_type": {k: {**v, "free": v["total"] - v["in_use"]}
                    for k, v in sorted(by_type.items())},
        "longest_running": longest,
    }


def _rfc_round_trip_ms(session: SapSession) -> int | None:
    """
    Measures how long the application server takes to answer a trivial RFC
    call, in milliseconds.

    This is NOT dialog response time. It does not include screen rendering,
    database work or user think time, and it must never be labelled as if it
    were -- SMLG's number and this one measure different things.

    What it IS: a live, honest latency signal. If the dispatcher is saturated,
    work processes are all busy, or the network path has degraded, this climbs
    immediately. On a wall display that is often the first visible sign that a
    system is in trouble, and unlike ST03 it needs no collector job, no extra
    authorisation and no custom function module.

    RFC_PING is used because it does almost nothing on the server side, so the
    number reflects dispatcher latency rather than the cost of the call.
    """
    best = None
    for _ in range(2):
        started = time.perf_counter()
        result = session.call("RFC_PING")
        if result is None:
            return None
        elapsed = (time.perf_counter() - started) * 1000
        # Two samples, keep the faster: the first call can carry connection
        # warm-up that has nothing to do with the system's current health.
        best = elapsed if best is None else min(best, elapsed)
    return int(round(best))


def _logon_groups(session: SapSession) -> list[dict]:
    """
    SMLG logon/server groups with per-instance response time, read straight
    from RZLLITAB.

    This is the table behind transaction SMLG -- CLASSNAME, APPLSERVER,
    RESP_TIME, USERS are its columns, in that order on screen. It is a plain
    database table, so RFC_READ_TABLE reaches it with no ABAP module, no
    stat/level change and no dependence on ST03 aggregation.

    RESP_TIME is written by the message server as logons are distributed
    through a group. On a system where nobody logs on via a group it stays
    at 0 -- which is "not measured", not "0 ms". A zero is therefore
    reported as None so the UI shows "No data" rather than an impossibly
    fast system.

    GROUPTYPE distinguishes logon groups from server groups ('S'). Both are
    returned, labelled, because an operator looking at SMLG sees both.
    """
    rows = session.read_table(
        "RZLLITAB",
        ["CLASSNAME", "APPLSERVER", "GROUPTYPE", "RESP_TIME", "USERS"],
        "", 100,
    )
    if not rows:
        return []

    groups = []
    for row in rows:
        if len(row) < 5:
            continue
        name, server, gtype, resp, users = (c.strip() for c in row[:5])
        if not server:
            continue

        def number(text):
            try:
                return int(text)
            except (TypeError, ValueError):
                return None

        resp_ms = number(resp)
        user_count = number(users)

        groups.append({
            "group": name or "(none)",
            "instance": server,
            "kind": "server group" if gtype.upper() == "S" else "logon group",
            # 0 means the message server has not measured this group, not
            # that it responded in zero milliseconds.
            "response_ms": resp_ms if resp_ms else None,
            "users": user_count,
        })

    groups.sort(key=lambda g: (g["response_ms"] is None, -(g["response_ms"] or 0)))
    return groups


def _instances(session: SapSession) -> list[dict]:
    """Application servers registered with the message server."""
    result = session.call("TH_SERVER_LIST")
    if result is None:
        return []
    out = []
    for row in result.get("LIST", []) or []:
        out.append({
            "name": str(row.get("NAME", "")).strip(),
            "host": str(row.get("HOST", "")).strip(),
            "services": str(row.get("SERVICES", "")).strip(),
            # TH_SERVER_LIST reports which servers are REGISTERED, not which
            # are responding, so no availability claim is made here.
            "state": "registered",
        })
    return out


def read_live(system: str, cfg: dict, use_cache: bool = True) -> dict:
    """
    One fast RFC read. Never raises; never writes anything.

    `connected: False` means UNKNOWN. The caller must not render it as
    healthy.
    """
    if use_cache:
        hit = _cached(system)
        if hit is not None:
            return hit

    payload = {
        "system": system,
        "sid": cfg.get("sap_system_id") or system,
        "client": cfg.get("client", ""),
        "connected": False,
        "error": None,
        "cooldown": 0,
        "read_at": datetime.now().strftime("%H:%M:%S"),
        "cache_age_seconds": 0,
        "cpu": None, "memory": None, "load_1m": None,
        "dispatcher": {"label": "Unknown", "status": "UNKNOWN"},
        "icm": {"label": "Unknown", "status": "UNKNOWN"},
        "gateway": {"label": "Unknown", "status": "UNKNOWN"},
        "work_processes": {"available": False},
        "instances": [],
        "users": None,
        "response_time": None,
        "rfc_round_trip_ms": None,
        "checks": [],
        "logon_groups": [],
    }

    remaining, cached_error = cooldown_remaining(system)
    if remaining:
        payload["error"] = cached_error
        payload["cooldown"] = remaining
        _store(system, payload)
        return payload

    if not cfg.get("rfc"):
        payload["error"] = "no RFC configured for this system"
        _store(system, payload)
        return payload

    with SapSession(system, cfg) as session:
        if not session.ok:
            payload["error"] = session.error
            _store(system, payload)
            return payload

        payload["connected"] = True

        fm = session.call("Z_GET_OBSERVABILITY_DATA")
        if fm:
            def number(key):
                try:
                    value = float(str(fm.get(key, "")).strip())
                except (TypeError, ValueError):
                    return None
                # -1 means the ABAP side could not read it. 0 is unavailable
                # for CPU/memory only -- a live host is never at 0% CPU.
                if value < 0:
                    return None
                return value

            cpu, memory = number("EV_CPU_UTIL_PCT"), number("EV_MEM_UTIL_PCT")
            payload["cpu"] = None if cpu == 0 else cpu
            payload["memory"] = None if memory == 0 else memory
            payload["load_1m"] = number("EV_LOAD_1M")   # 0.00 is a real value
            payload["users"] = number("EV_ACTIVE_USERS")
            payload["db_type"] = str(fm.get("EV_DB_TYPE", "") or "").strip() or None
            payload["server_time"] = str(fm.get("EV_SYS_TIME", "") or "").strip() or None

        # Full T-code counter set, read on the SAME connection. Calling the
        # collector separately would double the number of RFC logons per poll.
        try:
            metrics = _from_function_module(session) or _from_standard_modules(session)
            payload["checks"] = _checks_from_metrics(metrics)
            payload["check_source"] = "Z_FM" if metrics and metrics[0].extra_data.get(
                "collector") == "Z_FM" else "RFC_TABLE"
        except Exception as exc:
            payload["checks"] = []
            payload["check_error"] = f"{type(exc).__name__}: {exc}"

        rtt = _rfc_round_trip_ms(session)
        if rtt is not None:
            payload["rfc_round_trip_ms"] = rtt

        payload["work_processes"] = _work_processes(session)
        payload["instances"] = _instances(session)
        payload["logon_groups"] = _logon_groups(session)

        # Group response time is the closest thing to what SMLG displays, so
        # prefer it over the sweep's OCR-derived value when it has been
        # measured.
        measured = [g for g in payload["logon_groups"] if g["response_ms"]]
        if measured:
            best = max(measured, key=lambda g: g["response_ms"])
            payload["response_time"] = {
                "value": f"{best['response_ms']} ms",
                "status": ("CRITICAL" if best["response_ms"] >= 2000
                           else "WARNING" if best["response_ms"] >= 1000
                           else "NORMAL"),
                "source": f"RFC · SMLG {best['group']}",
                "age_minutes": 0,
            }

        # Dispatcher / ICM / gateway state. TH_WPINFO answering at all proves
        # the dispatcher is serving requests -- we are talking to it.
        if payload["work_processes"].get("available"):
            payload["dispatcher"] = {"label": "Running", "status": "NORMAL"}
        for key, count_key in (("icm", "EV_ICM_STATE"), ("gateway", "EV_GW_STATE")):
            if fm and count_key in fm:
                label, status = _process_state(fm.get(count_key))
                payload[key] = {"label": label, "status": status}

    _store(system, payload)
    return payload


def attach_snapshot_extras(payload: dict, snapshot: dict | None) -> dict:
    """
    Fills tiles the live RFC read could not supply, from the last sweep.

    Every value carries `source` and `age_minutes` so the UI can label it.
    Mixing a two-hour-old GUI reading into a live tile without saying so is
    how a dashboard starts lying quietly.

    Why this is needed per tile:
      * memory  -- the function module exports EV_MEM_UTIL_PCT only if the
                   corrected Z FM is installed; the SSH collector always has
                   it where OS access exists.
      * ICM / dispatcher -- there is no RFC call for these. sapcontrol
                   GetProcessList is the authoritative source and only the
                   SSH collector runs it.
      * response time -- ST03/SMLG workload data is not reachable over RFC
                   on this release; the SAP GUI collector captures it.
    """
    if not snapshot:
        return payload

    metrics = {m.get("name"): m for m in snapshot.get("metrics", []) or []}
    stamp = snapshot.get("cycle_timestamp") or snapshot.get("generated_at")

    age_minutes = None
    if stamp:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                age_minutes = max(0, round(
                    (datetime.now() - datetime.strptime(str(stamp), fmt)).total_seconds() / 60
                ))
                break
            except ValueError:
                continue

    def numeric_fallback(field: str, metric_name: str):
        """Fills a numeric tile only when the live read produced nothing."""
        if payload.get(field) is not None:
            return
        hit = metrics.get(metric_name)
        if not hit:
            return
        try:
            payload[field] = float(str(hit.get("display_value", "")).rstrip("%").split()[0])
        except (ValueError, IndexError):
            return
        # Record where it came from so the tile is not shown as RFC-live.
        payload.setdefault("fallback_sources", {})[field] = {
            "source": hit.get("source") or "last sweep",
            "age_minutes": age_minutes,
        }

    numeric_fallback("cpu", "cpu")
    numeric_fallback("memory", "memory")
    numeric_fallback("load_1m", "load_1m")

    def process_state(field: str, metric_name: str):
        """Dispatcher / ICM / gateway, from sapcontrol via the SSH collector."""
        if payload.get(field, {}).get("status") not in (None, "UNKNOWN"):
            return
        hit = metrics.get(metric_name)
        if not hit:
            return
        colour = str(hit.get("display_value", "")).strip().upper()
        label = {"GREEN": "Running", "YELLOW": "Starting",
                 "RED": "Stopped"}.get(colour, colour.title() or "Unknown")
        payload[field] = {
            "label": label,
            "status": (hit.get("status") or "UNKNOWN"),
            "source": "sapcontrol",
            "age_minutes": age_minutes,
        }

    process_state("dispatcher", "sap_process_disp+work")
    process_state("icm", "sap_process_icman")
    process_state("gateway", "sap_process_gwrd")

    def carry(target, metric_name, label):
        hit = metrics.get(metric_name)
        if not hit:
            return
        payload[target] = {
            "value": hit.get("display_value"),
            "status": hit.get("status", "UNKNOWN"),
            "source": label,
            "age_minutes": age_minutes,
        }

    # The GUI collector's metric name for response time has varied across
    # this codebase (sap.smlg.response_time, sap.st03n.dialog_response_time,
    # and OCR-derived variants). Match on meaning rather than on one exact
    # string, so a rename in the collector does not silently blank the tile.
    def carry_first(target, candidates, label):
        for name in candidates:
            if name in metrics:
                carry(target, name, label)
                return True
        for name, hit in metrics.items():
            low = name.lower()
            if "response" in low and ("smlg" in low or "st03" in low or "dialog" in low):
                carry(target, name, label)
                return True
        return False

    carry_first("response_time",
                ["sap.smlg.response_time", "sap.smlg.avg_response_time"],
                "SAP GUI · SMLG")
    carry_first("dialog_response",
                ["sap.st03n.dialog_response_time", "sap.st03n.response_time"],
                "SAP GUI · ST03N")

    payload["snapshot_age_minutes"] = age_minutes
    payload["snapshot_status"] = snapshot.get("overall_status")
    payload["open_incidents"] = len(snapshot.get("incidents") or [])
    return payload
