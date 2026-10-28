"""
Raw SAP readings for the custom alerts (core/custom_alerts.py).

Runs in the RFC worker process (core/rfc_proc.py) every
IBO_CUSTOM_ALERT_MINUTES. READ-ONLY: function modules that only read
(TH_SERVER_LIST, TH_WPINFO, /SDF/GET_DUMP_LOG, ENQUE_READ2) and
RFC_READ_TABLE on TBTCO, ARFCSSTATE, TRFCQIN, USR21 and ADR6. Nothing in
SAP is changed, no session is touched.

Each part is read on its own: one that fails gives {"error": "..."} for
that part and the others still count. Values are NOT graded here and no
window is applied -- the dashboard process does that with its own memory
(which dumps / queue entries it has already seen), so the result is the
same whichever worker process read it.

    read_all(session, system, cfg) -> {
        "sap_now":   "YYYY-mm-dd HH:MM:SS"  SAP system time used for ages
        "wp":        {"instances": [{"name", "bgd_total", "bgd_busy",
                                     "longest": {...}|None}], "error"}
        "dumps":     {"rows": [{date,time,user,error,exception,component,program}], "error"}
        "cancelled": {"rows": [{job, count, user, end}], "emails": {user: addr}, "error"}
        "running":   {"rows": [{job, count, user, start, minutes}], "error"}
        "locks":     {"rows": [{user, table, arg, age_min|None}], "error"}
        "sm58":      {"count", "dests": {dest: n}, "error"}
        "smq2":      {"rows": [{id, queue, state}], "id_kind": "entry"|"queue", "error"}
    }
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

# Work processes this very read runs in: not "a long runner".
_OWN_REPORTS = ("SAPLTHFB", "SAPLSDIFRUNTIME")
QRFC_ERROR_STATES = {"SYSFAIL", "CPICERR", "STOP", "WAITSTOP", "ANORETRY", "ARETRY", "RETRY", "ERROR"}
_SMQ2_ID_FIELDS = ("ARFCIPID", "ARFCPID", "ARFCTIME", "ARFCTIDCNT")
# Read only where the release has them: who sent the call, when, and SAP's error text.
_SM58_EXTRA = ("ARFCUSER", "ARFCUZEIT", "ARFCFNAM", "ARFCMSG")
_SMQ2_EXTRA = ("QRFCUSER", "ERRMESS")
# (system, table) whose wider read failed while the plain one worked: the
# extra fields are left out for WIDE_RETRY_S, then tried again (a one-off
# timeout must not hide users and error texts until the next restart).
_WIDE_REFUSED: dict = {}
WIDE_RETRY_S = 6 * 3600


def _wide_off(key) -> bool:
    at = _WIDE_REFUSED.get(key)
    if at is None:
        return False
    if time.monotonic() - at > WIDE_RETRY_S:
        _WIDE_REFUSED.pop(key, None)
        return False
    return True
ROW_LIMIT = 5000


def _s(v, n=60) -> str:
    return str(v if v is not None else "").strip()[:n]


def _num(v) -> float:
    try:
        return float(str(v).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _err(session, what: str) -> str:
    why = str(getattr(session, "last_error", "") or "").strip()
    return f"{what}: {why[:160]}" if why else f"{what}: no answer"


def read_wp(session) -> dict:
    """Background work processes on every instance, and the longest-busy work process (any type)."""
    from collectors import rfc_perf
    insts = rfc_perf.instances(session) or [{"name": "", "host": ""}]
    out, skipped = [], []
    for i in insts:
        kwargs = {"SRVNAME": i["name"]} if i["name"] else {}
        res = session.call("TH_WPINFO", **kwargs)
        if res is None and kwargs and len(insts) == 1:
            # Older kernel without SRVNAME: only safe with one instance, else
            # the connected instance would be counted under another's name.
            res = session.call("TH_WPINFO")
        if not res:
            skipped.append(i["name"] or "connected")
            continue
        wps = res.get("WPLIST", []) or []
        bgd = [w for w in wps if _s(w.get("WP_TYP")).upper() == "BGD"]
        busy_bgd = [w for w in bgd if not _s(w.get("WP_STATUS")).lower().startswith("wait")]
        longest = None
        for w in wps:
            if _s(w.get("WP_STATUS")).lower().startswith("wait"):
                continue
            report = _s(w.get("WP_REPORT"), 40)
            if report.upper() in _OWN_REPORTS:
                continue
            secs = int(_num(w.get("WP_ELTIME")))
            if longest is None or secs > longest["seconds"]:
                longest = {"seconds": secs, "type": _s(w.get("WP_TYP"), 8), "wp_no": _s(w.get("WP_NO"), 8),
                           "report": report, "user": _s(w.get("WP_BNAME"), 24),
                           "action": _s(w.get("WP_ACTION"), 32)}
        busy = sorted(({"wp_no": _s(w.get("WP_NO"), 8), "report": _s(w.get("WP_REPORT"), 40),
                        "user": _s(w.get("WP_BNAME"), 24), "seconds": int(_num(w.get("WP_ELTIME")))}
                       for w in busy_bgd), key=lambda b: -b["seconds"])[:8]
        out.append({"name": i["name"] or "connected", "bgd_total": len(bgd), "bgd_busy": len(busy_bgd),
                    "longest": longest, "busy": busy})
    return {"instances": out, "skipped": skipped, "error": "" if out else _err(session, "TH_WPINFO")}


def read_dumps(session, system: str, now: datetime) -> dict:
    """Today's dumps (and yesterday's in the first 15 minutes after midnight) with user and error."""
    from collectors.rfc_collector import read_dump_log
    days = [now.strftime("%Y%m%d")]
    if now.hour == 0 and now.minute < 15:
        days.insert(0, (now - timedelta(days=1)).strftime("%Y%m%d"))
    rows = []
    for day in days:
        got = read_dump_log(session, day)
        if got is None:
            return {"rows": [], "error": "dump details need /SDF/GET_DUMP_LOG (ST-PI): "
                                         + (_s(getattr(session, "last_error", ""), 140) or "not usable")}
        rows.extend(got)
    return {"rows": rows, "error": ""}


def _tbtco(session, fields, where):
    from collectors import extra_checks as xc
    return xc._rows(session, "TBTCO", fields, where, rows=ROW_LIMIT)


def read_cancelled(session, now: datetime, with_emails: bool = True) -> dict:
    """Cancelled jobs ending since yesterday, with who scheduled them, and those users' mail addresses."""
    from collectors import extra_checks as xc
    since = (now - timedelta(days=1)).strftime("%Y%m%d")
    rows = _tbtco(session, ["JOBNAME", "JOBCOUNT", "SDLUNAME", "ENDDATE", "ENDTIME"],
                  f"STATUS = 'A' AND ENDDATE >= '{since}'")
    if rows is None:
        return {"rows": [], "emails": {}, "error": _err(session, "TBTCO")}
    out = []
    for r in rows:
        end = xc._stamp(r.get("ENDDATE", ""), r.get("ENDTIME", ""))
        out.append({"job": r.get("JOBNAME", ""), "count": r.get("JOBCOUNT", ""),
                    "user": (r.get("SDLUNAME", "") or "").upper(),
                    "end": end.strftime("%Y-%m-%d %H:%M:%S") if end else ""})
    emails = {}
    if with_emails:
        recent = {o["user"] for o in out if o["user"] and o["end"]
                  and now - datetime.strptime(o["end"], "%Y-%m-%d %H:%M:%S") <= timedelta(minutes=20)}
        names = [n for n in (xc._ident(u) for u in recent) if n]
        if names:
            try:
                from collectors.rfc_live import _user_emails
                emails = _user_emails(session, getattr(session, "system", ""), names, force=True)
            except Exception as exc:  # noqa: BLE001
                log.info(f"user mail lookup failed: {type(exc).__name__}: {exc}")
    return {"rows": out, "emails": emails, "error": ""}


def read_running(session, now: datetime) -> dict:
    """Active background jobs (SM37 'active') with how long they have been running."""
    from collectors import extra_checks as xc
    rows = _tbtco(session, ["JOBNAME", "JOBCOUNT", "SDLUNAME", "STRTDATE", "STRTTIME"], "STATUS = 'R'")
    if rows is None:
        return {"rows": [], "error": _err(session, "TBTCO")}
    out = []
    for r in rows:
        start = xc._stamp(r.get("STRTDATE", ""), r.get("STRTTIME", ""))
        minutes = round(max(0.0, (now - start).total_seconds() / 60), 1) if start else None
        out.append({"job": r.get("JOBNAME", ""), "count": r.get("JOBCOUNT", ""),
                    "user": (r.get("SDLUNAME", "") or "").upper(),
                    "start": start.strftime("%Y-%m-%d %H:%M:%S") if start else "", "minutes": minutes})
    return {"rows": out, "error": ""}


def read_locks(session, system: str, client: str, now: datetime | None = None) -> dict:
    """SM12 entries with owner, table and age in minutes (None when SAP gives no time)."""
    from collectors import rfc_perf
    res = session.call("ENQUE_READ2", GCLIENT=str(client), GUNAME="")
    if res is None:
        return {"rows": [], "error": _err(session, "ENQUE_READ2")}
    enq = res.get("ENQ", []) or []
    ref = rfc_perf.lock_reference(enq, system, now)
    out = []
    for e in enq:
        stamp = rfc_perf._lock_timestamp(e)
        table = _s(e.get("GNAME"), 30)
        out.append({"user": _s(e.get("GUNAME"), 24).upper(), "table": table, "arg": _s(e.get("GARG"), 60),
                    "age_min": round(max(0.0, (ref - stamp).total_seconds() / 60), 1) if stamp else None,
                    "standing": table.startswith(rfc_perf.LOCK_AGE_IGNORE_PREFIXES)})
    return {"rows": out, "error": ""}


def read_sm58(session, now: datetime) -> dict:
    """Failed tRFC (SYSFAIL and CPICERR) from today, per destination."""
    today = now.strftime("%Y%m%d")
    # Who and when, where this release has the fields (asked, not assumed).
    try:
        from collectors import extra_checks as xc
        have = xc.table_fields(session, "ARFCSSTATE") or set()
    except Exception:  # noqa: BLE001
        have = set()
    wide_key = (getattr(session, "system", ""), "ARFCSSTATE")
    extra = [f for f in _SM58_EXTRA if f in have] if not _wide_off(wide_key) else []
    cols = ["ARFCDEST", "ARFCSTATE"] + extra
    where = f"ARFCDATUM = '{today}' AND ( ARFCSTATE = 'SYSFAIL' OR ARFCSTATE = 'CPICERR' )"
    rows = session.read_table("ARFCSSTATE", cols, where, ROW_LIMIT)
    if rows is None and extra:
        cols = ["ARFCDEST", "ARFCSTATE"]                  # the extra fields were refused: the count still matters
        rows = session.read_table("ARFCSSTATE", cols, where, ROW_LIMIT)
        if rows is not None:
            _WIDE_REFUSED[wide_key] = time.monotonic()
    if rows is None:
        return {"count": None, "dests": {}, "error": _err(session, "ARFCSSTATE")}
    dests: dict = {}
    entries = []
    for r in rows:
        vals = {c: (str(r[i]).strip() if i < len(r) else "") for i, c in enumerate(cols)}
        d = vals.get("ARFCDEST") or "?"
        dests[d] = dests.get(d, 0) + 1
        entries.append({"dest": d, "state": vals.get("ARFCSTATE", ""), "user": vals.get("ARFCUSER", "").upper(),
                        "time": vals.get("ARFCUZEIT", ""), "function": vals.get("ARFCFNAM", ""),
                        "message": vals.get("ARFCMSG", "")[:120]})
    return {"count": len(rows), "dests": dests, "rows": entries, "capped": len(rows) >= ROW_LIMIT, "error": ""}


def read_smq2(session) -> dict:
    """Inbound qRFC entries (TRFCQIN) with an identity, so the dashboard can tell how long each has waited."""
    from collectors import extra_checks as xc
    fields = xc.table_fields(session, "TRFCQIN")
    if not fields or "QNAME" not in fields:
        return {"rows": [], "id_kind": "", "error": _err(session, "TRFCQIN")}
    id_fields = [f for f in _SMQ2_ID_FIELDS if f in fields]
    if len(id_fields) == len(_SMQ2_ID_FIELDS):
        kind = "entry"
    elif "QCOUNT" in fields:
        id_fields, kind = ["QCOUNT"], "entry"
    else:
        id_fields, kind = [], "queue"
    base = ["QNAME"] + (["QSTATE"] if "QSTATE" in fields else []) + id_fields
    wide_key = (getattr(session, "system", ""), "TRFCQIN")
    cols = base + ([f for f in _SMQ2_EXTRA if f in fields] if not _wide_off(wide_key) else [])
    rows = session.read_table("TRFCQIN", cols, "", ROW_LIMIT)
    if rows is None and cols != base:
        cols = base
        rows = session.read_table("TRFCQIN", cols, "", ROW_LIMIT)
        if rows is not None:
            _WIDE_REFUSED[wide_key] = time.monotonic()
    if rows is None:
        return {"rows": [], "id_kind": kind, "error": _err(session, "TRFCQIN")}
    out = []
    for r in rows:
        vals = {c: (str(r[i]).strip() if i < len(r) else "") for i, c in enumerate(cols)}
        ident = vals["QNAME"] + ("|" + "|".join(vals[f] for f in id_fields) if id_fields else "")
        out.append({"id": ident, "queue": vals["QNAME"], "state": vals.get("QSTATE", "").upper(),
                    "user": vals.get("QRFCUSER", "").upper(), "message": vals.get("ERRMESS", "")[:120]})
    return {"rows": out, "id_kind": kind, "capped": len(rows) >= ROW_LIMIT, "error": ""}


def read_all(session, system: str, cfg: dict) -> dict:
    from collectors import extra_checks as xc
    now = xc._sap_now(system)
    client = str((cfg.get("rfc") or {}).get("client") or cfg.get("client") or "000")
    out = {"sap_now": now.strftime("%Y-%m-%d %H:%M:%S")}
    parts = (("wp", lambda: read_wp(session)),
             ("dumps", lambda: read_dumps(session, system, now)),
             ("cancelled", lambda: read_cancelled(session, now)),
             ("running", lambda: read_running(session, now)),
             ("locks", lambda: read_locks(session, system, client, now)),
             ("sm58", lambda: read_sm58(session, now)),
             ("smq2", lambda: read_smq2(session)))
    for name, fn in parts:
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 -- one part never stops the others
            out[name] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    return out
