"""
RFC performance collector -- the SM50/SM66/ST03N/SM12/SQLM view, all instances.

Uses only the RFC user this project already has. No custom ABAP, no OS
access, no database login. Every read is a standard remote-enabled function
module or a table read through RFC_READ_TABLE.

WHAT IT READS
-------------
    instances            TH_SERVER_LIST
    work processes       TH_WPINFO with SRVNAME per instance   -> PRIV mode, long runners
    dialog response      SWNC_GET_STATRECS_FRAME               -> per instance, per user, last N min
    lock aggregation     ENQUE_READ2                           -> per user, same object, oldest
    expensive SQL        SQLMD via RFC_READ_TABLE              -> by ABAP program (SQL Monitor)
    table shape          DDIF_FIELDINFO_GET                    -> which columns exist here

SHAPE DISCOVERY, NOT SHAPE ASSUMPTION
-------------------------------------
Two of these sources vary by release: the STAT record structure and SQLMD.
Rather than hardcode column names and dump on the target when they are
wrong, this module asks DDIF_FIELDINFO_GET what the table actually has and
maps roles ("program", "total time") onto whichever candidate column
exists. A source whose essential roles are missing is skipped with a log
line naming what was looked for. That log line is the verification step:
run once, read it, correct the candidate list if needed.

WHAT PRIV MODE MEANS, AND WHY IT IS A METRIC
--------------------------------------------
A dialog work process enters PRIV when a user's context outgrows roll +
extended memory and spills into heap. The process is then pinned to that
user until they log off or the step ends, so every PRIV process is one
fewer for everyone else. Two PRIV processes on one instance is a warning;
five is the instance about to stop taking dialog work. The user and report
named on the PRIV process are usually the expensive-statement culprit the
memory-dump analysis is looking for -- this is the live view of that.

FAILURE MODEL
-------------
As the RFC collector: (metrics_so_far, error). Nothing readable -> error,
never a healthy zero.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta

from collectors.rfc_collector import SapSession, cooldown_remaining
from core.models import MetricResult, Status
from utils.logger import get_logger

log = get_logger(__name__)

# (warning, critical). Overridden by config/thresholds.yaml where present.
_THRESHOLDS = {
    "sap.sm50.priv_mode_wp":          (2, 5),
    "sap.sm50.long_running_wp":       (2, 5),
    "sap.sm66.max_instance_saturation_pct": (80, 90),
    "sap.st03.dialog_resp_ms":        (1000, 2500),
    "sap.st03.max_instance_resp_ms":  (1500, 3500),
    "sap.st03.db_time_pct":           (50, 70),
    "sap.st03.top_user_memory_mb":    (1024, 2048),
    "sap.st03.top_report_db_ms":      (60_000, 300_000),
    "sap.sm12.locks_per_user_max":    (20, 50),
    "sap.sm12.oldest_lock_minutes":   (30, 120),
    "sap.sm12.users_with_many_locks": (2, 5),
    "sap.sqlm.expensive_programs":    (3, 10),
    "sap.sqlm.top_program_total_s":   (300, 1800),
}

LONG_RUNNING_WP_SECONDS = 600       # SM50 "Time" column, seconds
# STAT records lookback. Every record is ~100 fields plus sub-records, so on
# a busy PRD five minutes can be tens of thousands of records over RFC.
# Override with IBO_STAT_WINDOW_MINUTES if the read is slow; two minutes is
# still a fair sample of dialog response.
import os as _os
RESP_WINDOW_MINUTES = int(_os.environ.get("IBO_STAT_WINDOW_MINUTES", "5") or 5)

# A response-time average over a handful of steps is noise: on an idle QAS a
# single 3712 ms dialog step (someone opening one transaction) becomes a
# "3712 ms average" and grades CRITICAL, when the system is simply idle. Below
# this many dialog steps the figure is reported for information but not graded
# -- the same principle as the collectors' "a reading we can't trust is not a
# healthy reading, and not an alarming one either".
MIN_DIALOG_STEPS = int(_os.environ.get("IBO_MIN_DIALOG_STEPS", "10") or 10)
LOCKS_PER_USER_MANY = 10            # "many locks" threshold per user
# Lock objects whose age is expected to be long and must not drive the
# oldest-lock metric. /SDF/ is the Service Data Framework: SMON holds
# /SDF/SMON_CALL for its entire run, Cloud ALM holds /SDF/CALM_HM_K hourly.
# Confirmed live on PRD. They still count toward per-user totals.
LOCK_AGE_IGNORE_PREFIXES = ("/SDF/", "BGRFC")
SQLM_ROWS = 5000                    # RFC_READ_TABLE cap for SQLMD
SQLM_LOOKBACK_DAYS = 7              # SQLMD rows with DDATE in this window

# Systems where a SQLMD scan came back empty, i.e. SQL Monitor is off. Once
# learned, the scan is skipped for the rest of the process (see sqlm_summary).
_sqlm_off: set[str] = set()
_sqlm_off_lock = threading.Lock()


def reset_sqlm_off(system: str | None = None) -> None:
    with _sqlm_off_lock:
        _sqlm_off.clear() if system is None else _sqlm_off.discard(system)
SQLM_EXPENSIVE_TOTAL_S = 60         # program is "expensive" past this total DB time
TOP_N = 10


def _thresholds_for(key):
    warn, crit = _THRESHOLDS.get(key, (None, None))
    try:
        from core.config_loader import get_thresholds
        cfg = get_thresholds().get(key) or {}
        warn, crit = cfg.get("warning", warn), cfg.get("critical", crit)
    except Exception:  # noqa: BLE001
        pass
    return warn, crit


def _grade(key, value):
    if value is None:
        return Status.UNKNOWN
    warn, crit = _thresholds_for(key)
    if crit is not None and value >= crit:
        return Status.CRITICAL
    if warn is not None and value >= warn:
        return Status.WARNING
    return Status.NORMAL


def _num(raw):
    try:
        text = str(raw if raw is not None else "").strip().replace(",", ".")
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _s(raw, n=40):
    # Lock arguments and some kernel strings arrive padded with U+FFFF or
    # NULs (seen live on PRD: RSTABLE GARG). Drop anything non-printable.
    text = "".join(ch for ch in str(raw or "") if ch.isprintable() and ch != "\uffff")
    return " ".join(text.split())[:n]


def _metric(name, value, unit, category, tcode, detail="", display=None, extra=None, status=None):
    warn, crit = _thresholds_for(name)
    if display is None:
        display = f"{value:.1f}{unit}" if unit == "%" else f"{value:.0f} {unit}"
    return MetricResult(
        name=name, value=float(value), display_value=display,
        status=status or _grade(name, value), threshold_warning=warn, threshold_critical=crit,
        source="rfc_perf", tcode=tcode, detail=detail, category=category, unit=unit,
        extra_data={"collector": "RFC_PERF", **(extra or {})},
    )


# ---------------------------------------------------------------------------
# Table shape discovery
# ---------------------------------------------------------------------------

_shape_cache: dict[tuple[str, str], list[str]] = {}
_shape_lock = threading.Lock()


def table_fields(session: SapSession, system: str, table: str) -> list[str]:
    """Column names of `table` on this system via DDIF_FIELDINFO_GET.
    Empty list means the table does not exist or is not readable."""
    key = (system, table)
    with _shape_lock:
        if key in _shape_cache:
            return list(_shape_cache[key])
    res = session.call("DDIF_FIELDINFO_GET", TABNAME=table)
    fields = []
    if res:
        fields = [str(f.get("FIELDNAME", "")).strip() for f in res.get("DFIES_TAB", [])]
        fields = [f for f in fields if f]
    with _shape_lock:
        _shape_cache[key] = fields
    if not fields:
        log.info(f"[{system}] {table}: no field info returned -- table absent or not readable")
    return list(fields)


def _pick(available: list[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in available:
            return c
    return None


# ---------------------------------------------------------------------------
# Instances and work processes
# ---------------------------------------------------------------------------

def instances(session: SapSession) -> list[dict]:
    res = session.call("TH_SERVER_LIST")
    if not res:
        return []
    out = []
    for row in res.get("LIST", []) or res.get("LIST_IPV6", []):
        name = _s(row.get("NAME"), 64)
        if name:
            out.append({"name": name, "host": _s(row.get("HOST"), 64)})
    return out


def _is_priv(wp: dict) -> bool:
    # SM50 shows PRIV in the "Reason" column, which TH_WPINFO carries as
    # WP_WAITING on the releases seen so far. Check the status and semaphore
    # text as well: a kernel that reports it elsewhere still gets caught.
    for k in ("WP_WAITING", "WP_STATUS", "WP_SEMSTAT", "WP_IWAIT"):
        if "PRIV" in str(wp.get(k, "")).upper():
            return True
    return False


def work_processes(session: SapSession, inst: list[dict]) -> dict:
    """
    TH_WPINFO per instance. Returns
        {instance: {"total_dia", "busy_dia", "priv": [..], "long": [..]}}
    An instance that fails to answer is omitted, not zeroed.
    """
    out = {}
    targets = inst or [{"name": "", "host": ""}]
    for i in targets:
        kwargs = {"SRVNAME": i["name"]} if i["name"] else {}
        res = session.call("TH_WPINFO", **kwargs)
        if res is None and kwargs:
            res = session.call("TH_WPINFO")     # older kernel: no SRVNAME
        if not res:
            continue
        wps = res.get("WPLIST", [])
        dia = [w for w in wps if str(w.get("WP_TYP", "")).strip().upper() == "DIA"]
        busy = [w for w in dia if not str(w.get("WP_STATUS", "")).strip().lower().startswith("wait")]

        def entry(w):
            return {
                "wp_no": _s(w.get("WP_NO"), 8), "pid": _s(w.get("WP_PID"), 12),
                "user": _s(w.get("WP_BNAME"), 24), "client": _s(w.get("WP_MANDT"), 4),
                "report": _s(w.get("WP_REPORT"), 40), "action": _s(w.get("WP_ACTION"), 32),
                "table": _s(w.get("WP_TABLE"), 30),
                "elapsed_s": int(_num(w.get("WP_ELTIME")) or 0),
                "cpu": _s(w.get("WP_CPU"), 12), "status": _s(w.get("WP_STATUS"), 16),
                "reason": _s(w.get("WP_WAITING"), 16),
            }

        priv = [entry(w) for w in wps if _is_priv(w)]
        long_ = [entry(w) for w in busy if (_num(w.get("WP_ELTIME")) or 0) >= LONG_RUNNING_WP_SECONDS]
        out[i["name"] or "connected"] = {
            "total_dia": len(dia), "busy_dia": len(busy),
            "saturation_pct": round(len(busy) / len(dia) * 100) if dia else None,
            "priv": priv, "long": sorted(long_, key=lambda e: e["elapsed_s"], reverse=True),
        }
    return out


# ---------------------------------------------------------------------------
# Dialog response time from STAT records
# ---------------------------------------------------------------------------

# SAP task types (RAW1). Confirmed live on QAS: 0x66 RFC, 0x04 BTC, 0x03 spool,
# 0x06 buffer sync, 0x07 autoabap, 0x0b/0x0e/0x0f/0x12/0xfe system housekeeping.
TASK_DIALOG, TASK_UPDATE, TASK_SPOOL, TASK_BATCH, TASK_RFC, TASK_HTTP = 0x01, 0x02, 0x03, 0x04, 0x66, 0x67
TASK_LABELS = {0x01: "DIALOG", 0x02: "UPDATE", 0x03: "SPOOL", 0x04: "BTC", 0x05: "ENQUEUE",
               0x06: "BUF.SYNC", 0x07: "AUTOABAP", 0x08: "UPDATE2", 0x65: "ALE", 0x66: "RFC",
               0x67: "HTTP", 0x68: "HTTPS", 0x69: "SMTP", 0xfe: "OTHER"}


def _task_label(code) -> str:
    return TASK_LABELS.get(code, hex(code) if code is not None else "?")


def _tasktype_code(raw) -> int | None:
    if isinstance(raw, (bytes, bytearray)):
        return raw[0] if raw else None
    text = str(raw if raw is not None else "").strip()
    if text.startswith("b'") or text.startswith('b"'):        # str(repr(bytes))
        try:
            import ast
            b = ast.literal_eval(text)
            return b[0] if b else None
        except Exception:  # noqa: BLE001
            return None
    if text.upper() in ("DIALOG", "DIA"):
        return TASK_DIALOG
    try:
        return int(text, 16) if text else None
    except ValueError:
        return None


def _tasktype_is_dialog(raw) -> bool:
    return _tasktype_code(raw) == TASK_DIALOG


_last_stat_result_keys: dict[str, list] = {}

# SAP system local time, learned from the STAT frame on each poll, keyed by
# system. Lock ages are measured against THIS, not the monitoring host's
# clock: a PRD box a few minutes ahead of the laptop turned every lock age
# negative, which clamped to 0 and hid an 8-lock BGCLOUD hold.
_sap_clock: dict[str, datetime] = {}
_sap_clock_lock = threading.Lock()


# Offset of lock-owner stamps from SAP system time, minutes, per system.
# Confirmed live on PRD: every owner stamp was +5:30 ahead of SAP time --
# the app server's OS time zone differs from the SAP system time zone. The
# owner ID is stamped with OS local time; STAT records use SAP time. The
# offset is inferred (no lock can be set in the future), quantised to
# 15-minute steps because real offsets are time zones, and only ever grows
# within a process, since a fresh lock reveals the full offset and an old
# one under-reports it.
_owner_offset_min: dict[str, int] = {}


def sap_now(system: str) -> datetime:
    with _sap_clock_lock:
        return _sap_clock.get(system) or datetime.now()


def _learn_clock(system: str, got: dict, recs: list[dict]) -> None:
    """
    Frame ENDTIMESTAMP is UTC. Records carry both STARTTIME (system local)
    and STARTTIMESTAMP (UTC), which gives the local offset. Combine them.
    """
    try:
        frames = got.get("ALL_STATRECS") or []
        end_utc = next((_s(f.get("ENDTIMESTAMP"), 14) for f in frames if _s(f.get("ENDTIMESTAMP"), 14)), "")
        if len(end_utc) != 14:
            return
        end_dt = datetime.strptime(end_utc, "%Y%m%d%H%M%S")
        offset = None
        for r in recs:
            loc, utc = _s(r.get("STARTDATE"), 8) + _s(r.get("STARTTIME"), 6), _s(r.get("STARTTIMESTAMP"), 14)
            if len(loc) == 14 and len(utc) == 14 and loc.isdigit() and utc.isdigit():
                offset = datetime.strptime(loc, "%Y%m%d%H%M%S") - datetime.strptime(utc, "%Y%m%d%H%M%S")
                break
        if offset is None:
            return
        with _sap_clock_lock:
            _sap_clock[system] = end_dt + offset
    except Exception:  # noqa: BLE001 - clock learning is best-effort
        return


def _flatten_statrec(rec: dict) -> dict:
    """
    A STAT record over RFC is nested: {"MAINREC": {...}, "DBPROCREC": [...],
    "TABREC": [...], ...}. Lift MAINREC's fields to the top so the rest of
    the module can read RESPTI, TASKTYPE, ACCOUNT, MAXBYTES directly, and
    keep the sub-records alongside under their own keys.
    """
    if not isinstance(rec, dict):
        return {}
    main = rec.get("MAINREC")
    if isinstance(main, dict):
        flat = dict(main)
        for k, v in rec.items():
            if k != "MAINREC":
                flat[k] = v
        return flat
    return dict(rec)


def _frames(got: dict) -> list[tuple[str, list]]:
    """(instance, records) per frame, whatever this release calls the tables.
    Confirmed live: ALL_STATRECS -> [{INSTANCE, SYSTEMID, STATRECS: [...]}]."""
    for key in ("ALL_STATRECS", "STATRECS_ALL", "INSTANCE_RECORDS"):
        frames = got.get(key)
        if isinstance(frames, list) and frames and isinstance(frames[0], dict) and "STATRECS" in frames[0]:
            return [(_s(f.get("INSTANCE"), 64) or "connected", f.get("STATRECS") or []) for f in frames]
    for key in ("STATRECS", "STAT_RECS", "E_STATRECS", "RECORDS"):
        if isinstance(got.get(key), list):
            return [("connected", got[key])]
    lists = [(k, v) for k, v in got.items() if isinstance(v, list)]
    return [("connected", max(lists, key=lambda kv: len(kv[1]))[1])] if lists else []


def stat_records(session: SapSession, inst: list[dict], system: str = "") -> tuple[list[dict], str]:
    """
    Raw STAT records for the last RESP_WINDOW_MINUTES across all instances.
    One call: SWNC_GET_STATRECS_FRAME returns a frame per instance.
    Times are passed in the SAP system's local time, which is what the
    record STARTDATE/STARTTIME are in (STARTTIMESTAMP is UTC).
    """
    now = datetime.now()
    since = now - timedelta(minutes=RESP_WINDOW_MINUTES)
    got = session.call(
        "SWNC_GET_STATRECS_FRAME",
        READ_START_DATE=since.strftime("%Y%m%d"), READ_START_TIME=since.strftime("%H%M%S"),
        READ_END_DATE=now.strftime("%Y%m%d"), READ_END_TIME=now.strftime("%H%M%S"),
    )
    if got is None:
        return [], "SWNC_GET_STATRECS_FRAME not callable"
    _last_stat_result_keys["shape"] = [(k, len(v) if isinstance(v, list) else type(v).__name__)
                                       for k, v in got.items()]
    recs = []
    frames = _frames(got)
    for instance, records in frames:
        for r in records:
            flat = _flatten_statrec(r)
            if flat:
                flat["_instance"] = instance
                recs.append(flat)
    note = "" if any(i != "connected" for i, _ in frames) else "instance not identified in frames"
    if system:
        _learn_clock(system, got, recs)
    return recs, note


def response_summary(recs: list[dict], system: str = "") -> dict | None:
    """Per-instance and per-user dialog response, plus DB share."""
    dia = [r for r in recs if _tasktype_is_dialog(r.get("TASKTYPE"))]
    if not dia:
        if recs:
            seen = sorted({_task_label(_tasktype_code(r.get("TASKTYPE"))) for r in recs})
            known = any(_tasktype_code(r.get("TASKTYPE")) in TASK_LABELS for r in recs)
            if known:
                # Records decode fine, there just were no dialog steps: idle system.
                log.info(f"[{system}] {len(recs)} STAT records, 0 dialog steps in the window "
                         f"(task mix: {', '.join(seen)}) -- no GUI activity, metric left absent")
            else:
                log.warning(f"[{system}] {len(recs)} STAT records but no task type decoded. "
                            f"Values: {seen}. Record keys: {sorted(recs[0].keys())[:25]}")
        else:
            log.info(f"[{system}] STAT read returned 0 records in the last "
                     f"{RESP_WINDOW_MINUTES} minutes")
        return None

    def ms(r, *keys):
        return sum((_num(r.get(k)) or 0) for k in keys)

    # per_user holds every step's response, not a running total. A sum tells
    # you who was busiest; it does not tell you what anyone's screen actually
    # did, and it is the number that made a single stuck step look like a
    # system-wide outage.
    per_inst, per_user, total_resp, total_db, steps = defaultdict(list), defaultdict(list), 0.0, 0.0, 0
    slowest: list[dict] = []
    mem_user = defaultdict(lambda: {"max_bytes": 0.0, "steps": 0, "instance": ""})
    reports = defaultdict(lambda: {"steps": 0, "resp_ms": 0.0, "db_ms": 0.0, "max_mb": 0.0,
                                   "db_calls": 0, "tcodes": set(), "users": set()})
    priv_steps = 0
    for r in dia:
        resp = (_num(r.get("RESPTI")) or 0) / 1000  # RESPTI is microseconds; convert to ms
        # DBREQTIME is the per-step DB request time on this release (confirmed
        # live); older shapes carry READDIRTI/READSEQTI/CHNGTI instead.
        if "DBREQTIME" in r:
            db = (_num(r.get("DBREQTIME")) or 0) + (_num(r.get("DBPREQTIME")) or 0)
        elif "DBTIME" in r:
            db = _num(r.get("DBTIME")) or 0
        else:
            db = ms(r, "READDIRTI", "READSEQTI", "CHNGTI")
        per_inst[r["_instance"]].append(resp)
        rep = _s(r.get("REPORT"), 40)
        if rep:
            rr = reports[rep]
            rr["steps"] += 1; rr["resp_ms"] += resp; rr["db_ms"] += db
            rr["max_mb"] = max(rr["max_mb"], (_num(r.get("MAXBYTES")) or 0) / 1048576)
            rr["db_calls"] += int(_num(r.get("DSQLCNT")) or 0)
            tc = _s(r.get("TCODE"), 20)
            if tc:
                rr["tcodes"].add(tc)
            usr = _s(r.get("ACCOUNT"), 24)
            if usr:
                rr["users"].add(usr)
        user = _s(r.get("ACCOUNT") or r.get("USERNAME") or r.get("USER"), 24)
        if user:
            per_user[user].append(resp)
            slowest.append({"user": user, "resp_ms": round(resp),
                            "report": _s(r.get("REPORT"), 40),
                            "tcode": _s(r.get("TCODE"), 20),
                            "instance": r["_instance"]})
            mu = mem_user[user]
            mu["max_bytes"] = max(mu["max_bytes"], _num(r.get("MAXBYTES")) or _num(r.get("MEMSUM")) or 0)
            mu["steps"] += 1
            mu["instance"] = r["_instance"]
        if _tasktype_code(r.get("PRIVMODE")) not in (None, 0):
            priv_steps += 1
        total_resp += resp; total_db += (db or 0); steps += 1

    def pct(values: list[float], q: float) -> float:
        """Nearest-rank percentile. No numpy dependency for five numbers."""
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return ordered[idx]

    # MEDIAN, NOT MEAN, IS THE HEADLINE.
    #
    # RESPTI is per dialog step and its distribution has a long tail: a step
    # that sits behind an enqueue lock or a stuck tRFC records the whole wait
    # as response time. On a near-idle system a handful of those completely
    # determine the mean -- which is how a box with 0 users, 2 of 72 work
    # processes busy and a 46-minute lock reported a 470-second "dialog
    # response". The mean was not a unit bug and dividing it by 1000 would
    # have been wrong; it was an honest average of a distribution that no
    # single number summarises well.
    #
    # The median is what a person at a screen actually experienced. The mean,
    # p95, max and the slowest individual steps are all still carried, so the
    # tail stays visible instead of being smoothed away.
    all_resp = [v for vals in per_inst.values() for v in vals]
    inst_avg = {k: round(pct(v, 0.5)) for k, v in per_inst.items()}
    inst_mean = {k: round(sum(v) / len(v)) for k, v in per_inst.items()}
    slowest.sort(key=lambda d: d["resp_ms"], reverse=True)

    top_users = sorted(
        ((u, vals) for u, vals in per_user.items()),
        key=lambda kv: pct(kv[1], 0.5), reverse=True)[:TOP_N]
    top_mem = sorted(mem_user.items(), key=lambda kv: kv[1]["max_bytes"], reverse=True)[:TOP_N]
    mix = defaultdict(int)
    for r in recs:
        mix[_task_label(_tasktype_code(r.get("TASKTYPE")))] += 1

    def rep_rows(key):
        top = sorted(reports.items(), key=lambda kv: kv[1][key], reverse=True)[:TOP_N]
        return [{"report": n, "custom": n.upper().startswith(("Z", "Y")), "steps": d["steps"],
                 "total_resp_ms": round(d["resp_ms"]), "total_db_ms": round(d["db_ms"]),
                 "avg_resp_ms": round(d["resp_ms"] / d["steps"]), "max_mb": round(d["max_mb"], 1),
                 "db_calls": d["db_calls"], "tcodes": sorted(d["tcodes"])[:4],
                 "users": sorted(d["users"])[:4]} for n, d in top if d[key] > 0]

    return {
        "top_reports_by_response": rep_rows("resp_ms"),
        "top_reports_by_db_time": rep_rows("db_ms"),
        "top_reports_by_memory": rep_rows("max_mb"),
        "steps": steps,
        # avg_resp_ms is now the MEDIAN -- the name is kept because the
        # dashboard, history writer and trend endpoints all key on it, and
        # renaming it would silently break every stored series. The arithmetic
        # mean is still returned, as mean_resp_ms.
        "avg_resp_ms": round(pct(all_resp, 0.5)),
        "mean_resp_ms": round(total_resp / steps),
        "p95_resp_ms": round(pct(all_resp, 0.95)),
        "max_resp_ms": round(max(all_resp)) if all_resp else 0,
        "max_instance_resp_ms": max(inst_avg.values()),
        "per_instance": inst_avg,
        "per_instance_mean": inst_mean,
        "slowest_steps": slowest[:TOP_N],
        "db_time_pct": round(total_db / total_resp * 100, 1) if total_resp and total_db else None,
        # Per user: how many steps, what a typical one cost, and what the
        # worst one cost. "total_ms" is retained for anything still reading it
        # but it is no longer what the table sorts on.
        "top_users_by_total_ms": [
            {"user": u,
             "steps": len(vals),
             "median_ms": round(pct(vals, 0.5)),
             "p95_ms": round(pct(vals, 0.95)),
             "max_ms": round(max(vals)),
             "total_ms": round(sum(vals))}
            for u, vals in top_users],
        "top_users_by_memory": [{"user": u, "max_mb": round(d["max_bytes"] / 1048576, 1),
                                 "steps": d["steps"], "instance": d["instance"]} for u, d in top_mem
                                if d["max_bytes"] > 0],
        "priv_mode_steps": priv_steps,
        "task_mix": dict(mix),
    }


def _sid_from_instance(name: str) -> str:
    # 'qassrv_QAS_00' -> 'QAS'
    parts = name.split("_")
    return parts[-2] if len(parts) >= 3 else ""


def st03n_aggregate(session: SapSession, inst: list[dict], sid: str = "") -> dict | None:
    """
    ST03N's own daily aggregate per instance via SWNC_COLLECTOR_GET_AGGREGATES.
    Coarser than STAT records (today so far, not the last five minutes) but
    it is exactly what ST03N displays, accepts an instance name directly,
    and works wherever the workload collector runs. Used when STAT records
    are not obtainable.
    """
    days = [(datetime.now() - timedelta(days=d)).strftime("%Y%m%d") for d in (0, 1)]
    per_inst, top_users, total_resp, total_db, total_cnt = {}, defaultdict(float), 0.0, 0.0, 0
    for i in inst or [{"name": "TOTAL"}]:
        comp = i["name"]
        s_id = sid or _sid_from_instance(comp)
        got = None
        for day in days:
            for c in dict.fromkeys([comp, "TOTAL"]):
                got = session.call("SWNC_COLLECTOR_GET_AGGREGATES", COMPONENT=c, ASSIGNDSYS=s_id,
                                   PERIODTYPE="D", PERIODSTRT=day)
                if got:
                    break
            if got:
                break
        if not got:
            # Confirmed live: NO_DATA_FOUND on QAS -- the ST03N collector job
            # (SAP_COLLECTOR_FOR_PERFMONITOR) is not producing aggregates there.
            continue
        _last_stat_result_keys["st03n_shape"] = [(k, len(v) if isinstance(v, list) else type(v).__name__)
                                                 for k, v in got.items()]
        tasks = got.get("TASKTIMES", []) or []
        dia = [t for t in tasks if _tasktype_is_dialog(t.get("TASKTYPE"))]
        cnt = sum(_num(t.get("COUNT")) or 0 for t in dia)
        # RESPTI here is total response in MILLISECONDS across the bucket, and
        # COUNT is the number of steps in that bucket. SWNC returns one row per
        # (task type x time bucket), so a day has many dialog rows -- summing
        # RESPTI and dividing by summed COUNT gives the true per-step average.
        # (Confirmed against live probe: RESPTI 2075 over COUNT 14 = 148 ms.)
        resp = sum(_num(t.get("RESPTI")) or 0 for t in dia)
        db = sum((_num(t.get("DBTIME")) if "DBTIME" in t else
                  (_num(t.get("READDIRTI")) or 0) + (_num(t.get("READSEQTI")) or 0) + (_num(t.get("CHNGTI")) or 0))
                 or 0 for t in dia)
        if cnt:
            per_inst[comp] = round(resp / cnt)
            total_resp += resp; total_db += db; total_cnt += cnt
        for u in got.get("USERTCODE", []) or []:
            if _tasktype_is_dialog(u.get("TASKTYPE")):
                name = _s(u.get("ACCOUNT"), 24)
                if name:
                    top_users[name] += _num(u.get("RESPTI")) or 0
    if not per_inst:
        return None
    tu = sorted(top_users.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]
    return {
        "steps": int(total_cnt), "avg_resp_ms": round(total_resp / total_cnt),
        "max_instance_resp_ms": max(per_inst.values()), "per_instance": per_inst,
        "db_time_pct": round(total_db / total_resp * 100, 1) if total_resp else None,
        "top_users_by_total_ms": [{"user": u, "total_ms": round(t)} for u, t in tu],
    }


# ---------------------------------------------------------------------------
# Lock aggregation
# ---------------------------------------------------------------------------

def _lock_timestamp(e: dict) -> datetime | None:
    """
    When a lock was set. Releases with GTDATE/GTTIME say so directly; the
    others encode it in the owner ID (GUSR / GUSRVB), which begins
    YYYYMMDDHHMMSS followed by microseconds, WP number and host --
    confirmed live: '20260907164959126635000900qassrv'.
    """
    d, t = _s(e.get("GTDATE"), 8), _s(e.get("GTTIME"), 6)
    if len(d) == 8 and len(t) == 6 and d.isdigit() and t.isdigit() and d != "00000000":
        try:
            return datetime.strptime(d + t, "%Y%m%d%H%M%S")
        except ValueError:
            pass
    for k in ("GUSR", "GUSRVB"):
        owner = str(e.get(k, "") or "").strip()
        if len(owner) >= 14 and owner[:14].isdigit():
            try:
                return datetime.strptime(owner[:14], "%Y%m%d%H%M%S")
            except ValueError:
                continue
    return None


def system_hint(session) -> str:
    return getattr(session, "system", "?")


def lock_summary(session: SapSession, client: str, system: str = "") -> dict | None:
    res = session.call("ENQUE_READ2", GCLIENT=str(client), GUNAME="")
    if res is None:
        return None
    enq = res.get("ENQ", [])
    if enq and _lock_timestamp(enq[0]) is None:
        log.info(f"[{system_hint(session)}] could not derive lock timestamps; "
                 f"ENQ keys: {sorted(enq[0].keys())}")
    per_user, same_obj = defaultdict(int), defaultdict(int)
    oldest_min, oldest = 0, None
    now = sap_now(system)

    stamped = [(_lock_timestamp(e), e) for e in enq]
    stamped = [(t, e) for t, e in stamped if t]
    if stamped:
        newest = max(t for t, _ in stamped)
        ahead_min = (newest - now).total_seconds() / 60
        inferred = int(round(ahead_min / 15.0) * 15) if ahead_min > 2 else 0
        with _sap_clock_lock:
            prev = _owner_offset_min.get(system, 0)
            offset = max(prev, inferred)
            _owner_offset_min[system] = offset
        if offset and offset != prev:
            log.warning(f"[{system}] lock owner stamps run {offset} min ahead of SAP system time "
                        f"-- OS time zone on the app server differs from the SAP time zone. "
                        f"Ages corrected; tell Basis.")
    else:
        offset = _owner_offset_min.get(system, 0)
    ref = now + timedelta(minutes=offset)

    housekeeping = []
    for e in enq:
        u = _s(e.get("GUNAME"), 24)
        gname = _s(e.get("GNAME"), 30)
        per_user[u] += 1
        same_obj[(u, gname, _s(e.get("GARG"), 60))] += 1
        stamp = _lock_timestamp(e)
        if not stamp:
            continue
        age = max(0.0, (ref - stamp).total_seconds() / 60)
        if gname.startswith(LOCK_AGE_IGNORE_PREFIXES):
            housekeeping.append({"user": u, "object": gname, "age_min": round(age)})
            continue
        if age > oldest_min:
            oldest_min, oldest = age, (u, gname, _s(e.get("GARG"), 40))
    many = [(u, n) for u, n in per_user.items() if n >= LOCKS_PER_USER_MANY]
    dup = [(k, n) for k, n in same_obj.items() if n > 1]
    return {
        "total": len(enq),
        "per_user_max": max(per_user.values(), default=0),
        "top_users": sorted(per_user.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N],
        "users_with_many": sorted(many, key=lambda kv: kv[1], reverse=True),
        "same_object_dups": sorted(dup, key=lambda kv: kv[1], reverse=True)[:TOP_N],
        "oldest_minutes": round(oldest_min), "oldest": oldest,
        "clock_source": "sap" if system in _sap_clock else "host",
        "owner_clock_offset_min": _owner_offset_min.get(system, 0),
        "housekeeping_locks": housekeeping,
    }


# ---------------------------------------------------------------------------
# SQL Monitor by ABAP program
# ---------------------------------------------------------------------------

# Role -> candidate column names, most likely first. Extend from the
# DDIF log line if your release names them differently.
_SQLM_ROLES = {
    # Column names confirmed live on a NetWeaver release (SQLMD, 30 columns):
    # PROGNAME PROCNAME PROCLINE STMTKIND TABLENAME ROOTNAME XCNT RCNT
    # DBSUM DBMAX DBAVG RTSUM RTMAX DDATE DTIME. Alternatives kept for
    # releases that renamed them.
    "program":  ["PROGNAME", "PROGRAM_NAME", "PROGRAM", "ENTRY_PROGRAM"],
    "include":  ["PROCNAME", "INCLUDE_NAME", "INCLUDE"],
    "line":     ["PROCLINE", "LINE_NO", "LINE"],
    "op":       ["STMTKIND", "SQL_OPERATION", "OPERATION"],
    "tables":   ["TABLENAME", "TABLE_NAMES", "TABLE_NAME"],
    "root":     ["ROOTNAME", "ENTRY_POINT", "REQUEST_ENTRY"],
    "roottype": ["ROOTTYPE"],
    "execs":    ["XCNT", "EXEC_COUNT", "EXECUTIONS"],
    "total_us": ["DBSUM", "TOTAL_DB_TIME", "TOTAL_TIME", "SUM_TIME"],
    "rt_us":    ["RTSUM"],
    "max_us":   ["DBMAX", "MAX_TIME"],
    "rows":     ["RCNT", "TOTAL_RECORDS", "TOTAL_ROWS"],
    "date":     ["DDATE"],
}


def sqlm_summary(session: SapSession, system: str) -> dict | None:
    # SQL Monitor is either activated on a system or it is not; it does not
    # flip between polls. Once a full SQLMD scan has come back with no rows,
    # repeating it every perf read scans up to SQLM_ROWS rows to reach the
    # same "0 rows" answer -- pure cost. Remember the empty result for this
    # process and skip straight past it. reset_perf_cache() clears this too,
    # so a manual refresh after activating SQLM re-checks immediately.
    with _sqlm_off_lock:
        if system in _sqlm_off:
            return None

    have = table_fields(session, system, "SQLMD")
    if not have:
        return None
    cols = {role: _pick(have, cands) for role, cands in _SQLM_ROLES.items()}
    if not cols["program"] or not (cols["total_us"] or cols["execs"]):
        log.warning(f"[{system}] SQLMD present but essential columns not matched. "
                    f"Have: {have[:40]}. Looked for program in {_SQLM_ROLES['program']} "
                    f"and time in {_SQLM_ROLES['total_us']}.")
        return None
    fields = [c for c in cols.values() if c]
    where = ""
    if cols.get("date"):
        since = (datetime.now() - timedelta(days=SQLM_LOOKBACK_DAYS)).strftime("%Y%m%d")
        where = f"{cols['date']} >= '{since}'"
    rows = session.read_table("SQLMD", fields, where, rows=SQLM_ROWS)
    if rows is None:
        log.warning(f"[{system}] SQLMD read FAILED (RFC_READ_TABLE returned no result). "
                    f"fields={fields} where={where!r}. Try the probe with --sqlm.")
        return None
    if not rows:
        log.info(f"[{system}] SQLMD readable but 0 rows matched {where or 'no filter'} -- "
                 f"SQL Monitor (transaction SQLM) is not activated on this system. "
                 f"Skipping this read until restart or a manual refresh.")
        with _sqlm_off_lock:
            _sqlm_off.add(system)
        return None
    idx = {f: i for i, f in enumerate(fields)}

    def g(row, role):
        c = cols.get(role)
        return row[idx[c]].strip() if c and idx[c] < len(row) else ""

    by_prog = defaultdict(lambda: {"execs": 0, "total_us": 0.0, "rt_us": 0.0, "max_us": 0.0,
                                   "rows": 0.0, "stmts": 0, "tables": set(), "roots": set()})
    for r in rows:
        p = g(r, "program")
        if not p:
            continue
        a = by_prog[p]
        a["execs"] += int(_num(g(r, "execs")) or 0)
        a["total_us"] += _num(g(r, "total_us")) or 0
        a["max_us"] = max(a["max_us"], _num(g(r, "max_us")) or 0)
        a["rt_us"] += _num(g(r, "rt_us")) or 0
        a["rows"] += _num(g(r, "rows")) or 0
        a["stmts"] += 1
        t = g(r, "tables")
        if t:
            a["tables"].add(t[:30])
        root = g(r, "root")
        if root:
            a["roots"].add(root[:30])
    if not by_prog:
        return None
    ranked = sorted(by_prog.items(), key=lambda kv: kv[1]["total_us"], reverse=True)
    top = [{
        "program": p, "custom": p.upper().startswith(("Z", "Y")),
        "total_s": round(a["total_us"] / 1_000_000, 1), "execs": a["execs"],
        "avg_ms": round(a["total_us"] / a["execs"] / 1000, 2) if a["execs"] else None,
        "max_ms": round(a["max_us"] / 1000, 1), "rows": int(a["rows"]),
        "rt_s": round(a["rt_us"] / 1_000_000, 1),
        "statements": a["stmts"], "tables": sorted(a["tables"])[:6],
        "entry_points": sorted(a["roots"])[:6],
    } for p, a in ranked[:TOP_N]]
    expensive = [t for t in top if t["total_s"] >= SQLM_EXPENSIVE_TOTAL_S]
    return {"columns": cols, "rows_read": len(rows), "programs": len(by_prog),
            "top": top, "expensive": expensive, "truncated": len(rows) >= SQLM_ROWS}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_metrics(system: str, session: SapSession, client: str) -> list[MetricResult]:
    m: list[MetricResult] = []
    inst = instances(session)

    wp = work_processes(session, inst)
    if wp:
        priv = [dict(e, instance=i) for i, d in wp.items() for e in d["priv"]]
        long_ = [dict(e, instance=i) for i, d in wp.items() for e in d["long"]]
        sat = {i: d["saturation_pct"] for i, d in wp.items() if d["saturation_pct"] is not None}
        m.append(_metric("sap.sm50.priv_mode_wp", len(priv), "count", "workload", "SM50",
                         detail="; ".join(f"{e['instance']} WP{e['wp_no']} {e['user']} {e['report']} {e['elapsed_s']}s"
                                          for e in priv[:5]),
                         extra={"priv": priv, "instances": list(wp)}))
        m.append(_metric("sap.sm50.long_running_wp", len(long_), "count", "workload", "SM50",
                         detail="; ".join(f"{e['instance']} {e['user']} {e['report']} {e['elapsed_s']}s"
                                          for e in long_[:5]),
                         extra={"long_running": long_[:TOP_N], "threshold_seconds": LONG_RUNNING_WP_SECONDS}))
        if sat:
            worst = max(sat.items(), key=lambda kv: kv[1])
            m.append(_metric("sap.sm66.max_instance_saturation_pct", worst[1], "%", "workload", "SM66",
                             detail=", ".join(f"{i} {p}%" for i, p in sorted(sat.items())),
                             extra={"per_instance": sat, "worst_instance": worst[0]}))

    recs, note = stat_records(session, inst, system)
    rs = response_summary(recs, system) if recs is not None else None
    window = f"{RESP_WINDOW_MINUTES}min"
    if rs is None or rs["steps"] < MIN_DIALOG_STEPS:
        rs = st03n_aggregate(session, inst)
        if rs:
            window, note = "today (ST03N daily aggregate)", ""
            log.info(f"[{system}] response time: STAT records unavailable, using ST03N aggregates")
        else:
            log.info(f"[{system}] response time: no dialog steps in the last {RESP_WINDOW_MINUTES} min "
                     f"and no ST03N aggregate -- metric left absent (system idle or collector off)")
    if rs:
        inst_detail = ", ".join(f"{i} {v}ms" for i, v in sorted(rs["per_instance"].items()))
        # A response average over very few steps is noise, not a verdict. On an
        # idle QAS one 3712 ms step should not paint the tile CRITICAL. Below
        # MIN_DIALOG_STEPS the figure is shown for information, graded NORMAL,
        # and the detail says why -- an aggregate window (a whole day) always
        # has enough steps, so this only affects the sparse STAT window.
        low_sample = (window.endswith("min") and rs["steps"] < MIN_DIALOG_STEPS)
        sample_note = (f"only {rs['steps']} dialog step(s) in {window} — too few to grade, "
                       f"system near-idle") if low_sample else ""
        resp_status = Status.NORMAL if low_sample else None

        # Say which statistic this is, on the metric itself. A tile reading
        # "464549 ms" with no indication of whether that is a typical step or
        # an average dragged by one stuck one is unactionable, and it is what
        # made this figure untrustworthy on the wall.
        mean_ms, p95_ms, max_ms = (rs.get("mean_resp_ms"), rs.get("p95_resp_ms"),
                                   rs.get("max_resp_ms"))
        spread = ""
        if mean_ms is not None:
            spread = f"; median {rs['avg_resp_ms']}ms, mean {mean_ms}ms"
            if p95_ms is not None:
                spread += f", p95 {p95_ms}ms, worst {max_ms}ms"
            # A mean many times the median means the tail is doing the talking.
            # Name it rather than let the reader assume the system is on fire.
            if rs["avg_resp_ms"] and mean_ms > rs["avg_resp_ms"] * 5:
                worst = (rs.get("slowest_steps") or [{}])[0]
                spread += (f" — mean is skewed by a few long steps"
                           + (f" (worst: {worst.get('user')} "
                              f"{worst.get('report') or worst.get('tcode') or '?'} "
                              f"{worst.get('resp_ms')}ms)" if worst else ""))

        m.append(_metric("sap.st03.dialog_resp_ms", rs["avg_resp_ms"], "ms", "workload", "ST03N",
                         detail=f"{rs['steps']} steps/{window}; median per step; {inst_detail}"
                                + spread
                                + (f"; {sample_note}" if sample_note else "")
                                + (f"; {note}" if note else ""),
                         status=resp_status,
                         extra={"per_instance": rs["per_instance"], "window": window,
                                "steps": rs["steps"], "low_sample": low_sample,
                                "statistic": "median",
                                "mean_resp_ms": mean_ms, "p95_resp_ms": p95_ms,
                                "max_resp_ms": max_ms,
                                "per_instance_mean": rs.get("per_instance_mean", {}),
                                "slowest_steps": rs.get("slowest_steps", []),
                                "top_users_by_total_ms": rs["top_users_by_total_ms"], "note": note,
                                "task_mix": rs.get("task_mix", {}),
                                "top_reports_by_response": rs.get("top_reports_by_response", []),
                                "top_reports_by_db_time": rs.get("top_reports_by_db_time", []),
                                "top_reports_by_memory": rs.get("top_reports_by_memory", [])}))
        top_db = rs.get("top_reports_by_db_time") or []
        if top_db:
            t = top_db[0]
            m.append(_metric("sap.st03.top_report_db_ms", t["total_db_ms"], "ms", "workload", "ST03N",
                             detail=f"{t['report']} {t['steps']} steps, {t['db_calls']} DB calls, "
                                    f"users {', '.join(t['users'][:3])}; window {window}",
                             extra={"top_reports_by_db_time": top_db, "window": window}))
        top_mem = rs.get("top_users_by_memory") or []
        if top_mem:
            m.append(_metric("sap.st03.top_user_memory_mb", top_mem[0]["max_mb"], "MB", "workload", "ST03N",
                             detail="; ".join(f"{u['user']} {u['max_mb']}MB on {u['instance']}" for u in top_mem[:5]),
                             extra={"top_users_by_memory": top_mem, "window": window,
                                    "priv_mode_steps": rs.get("priv_mode_steps", 0)}))
        m.append(_metric("sap.st03.max_instance_resp_ms", rs["max_instance_resp_ms"], "ms", "workload", "ST03N",
                         detail=inst_detail + (f"; {sample_note}" if sample_note else ""),
                         status=resp_status))
        if rs["db_time_pct"] is not None:
            m.append(_metric("sap.st03.db_time_pct", rs["db_time_pct"], "%", "workload", "ST03N",
                             detail="share of dialog response spent in database calls"))
    elif note:
        log.info(f"[{system}] response time: {note}")

    ls = lock_summary(session, client, system)
    if ls:
        m.append(_metric("sap.sm12.locks_per_user_max", ls["per_user_max"], "count", "application", "SM12",
                         detail=", ".join(f"{u} {n}" for u, n in ls["top_users"][:5]),
                         extra={"top_users": ls["top_users"], "same_object_dups": ls["same_object_dups"]}))
        m.append(_metric("sap.sm12.users_with_many_locks", len(ls["users_with_many"]), "count", "application", "SM12",
                         detail=", ".join(f"{u} {n}" for u, n in ls["users_with_many"][:5]),
                         extra={"threshold": LOCKS_PER_USER_MANY}))
        m.append(_metric("sap.sm12.oldest_lock_minutes", ls["oldest_minutes"], "min", "application", "SM12",
                         detail=" ".join(ls["oldest"]) if ls["oldest"] else "",
                         extra={"oldest": ls["oldest"], "clock_source": ls["clock_source"],
                                "owner_clock_offset_min": ls["owner_clock_offset_min"],
                                "housekeeping_locks": ls["housekeeping_locks"]}))

    ss = sqlm_summary(session, system)
    if ss:
        top = ss["top"][0] if ss["top"] else None
        m.append(_metric("sap.sqlm.expensive_programs", len(ss["expensive"]), "count", "database", "SQLM",
                         detail="; ".join(f"{t['program']} {t['total_s']}s x{t['execs']}" for t in ss["expensive"][:5]),
                         extra={"top": ss["top"], "programs": ss["programs"], "rows_read": ss["rows_read"],
                                "truncated": ss["truncated"], "columns": ss["columns"]}))
        if top:
            m.append(_metric("sap.sqlm.top_program_total_s", top["total_s"], "s", "database", "SQLM",
                             detail=f"{top['program']} x{top['execs']} avg {top['avg_ms']}ms "
                                    f"tables {', '.join(top['tables'][:3])}"))
    return m


def collect_perf_metrics(system: str, cfg: dict) -> tuple[list[MetricResult], str | None]:
    """One RFC logon; (metrics, error). Same contract as collect_rfc_metrics."""
    remaining, cached = cooldown_remaining(system)
    if remaining:
        return [], f"{cached} (retrying in {remaining}s)"
    client = str((cfg.get("rfc") or {}).get("client") or cfg.get("client") or "000")
    with SapSession(system, cfg) as s:
        if not s.ok:
            return [], s.error
        try:
            metrics = build_metrics(system, s, client)
        except Exception as e:  # noqa: BLE001
            return [], f"perf read failed: {type(e).__name__}: {e}"
    if not metrics:
        return [], "connected, but no performance source was readable"
    log.info(f"[{system}] RFC perf collector: {len(metrics)} metrics")
    return metrics, None
