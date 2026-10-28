"""
Custom alerts, checked every IBO_CUSTOM_ALERT_MINUTES (default 5) for every
system this machine monitors over RFC:

    sap.sm66.bgd_saturation_pct       background work processes busy, all instances (%)
    sap.st22.user_dumps_10min         ABAP dumps by one user in 15 minutes (IBO_DUMP_WINDOW_MIN) -> team by error
    sap.sm37.user_cancelled_15min     cancelled jobs by one user in 15 min  -> Basis, with
                                      "Approve: mail the user" (core/job_user_mail.py)
    sap.sm12.user_old_locks           locks held 15 min+ by one user
    sap.sm12.lock_held_min            longest-held lock (min)
    sap.sm12.users_same_table_15min   different users locking one table, locks set in 15 min
    sap.sm58.failed_today             failed tRFC today, SYSFAIL + CPICERR
    sap.smq2.waiting_10min            inbound queue entries waiting 10 min+
    sap.smq2.error_10min              inbound queue entries in an error state, waiting 10 min+
    sap.sm37.longest_active_job_min   background job running longest (min), per job
    sap.sm66.longest_wp_min           work process busy longest on one request, all instances (min)

Limits, alert level and channels are set per system on the Thresholds &
alerts page like every other value; sending is core/alert_engine.py.

Reading SAP is collectors/custom_reads.py (read-only, in the RFC worker).
This module keeps what it has already seen (table custom_seen), so:
  * a dump counts in the 10-minute window from when it first appeared. The
    dump's own time is not used: its time zone is not certain on every
    release. After a gap of more than 15 minutes between reads, what is
    there is taken as already known (no burst of old dumps after a restart).
  * a queue entry's waiting time is counted from when it was first seen
    (resolution: one check interval).
Cancelled jobs, running jobs and lock ages use SAP's own times.

IBO_CUSTOM_ALERTS=0 switches the checks off.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


# "More than 10 dumps by one user in 15 minutes" (14 Oct 2026). The window is
# IBO_DUMP_WINDOW_MIN; the count that alerts is set on the Thresholds page.
DUMP_WINDOW_MIN = _env_int("IBO_DUMP_WINDOW_MIN", 15)
JOB_WINDOW_MIN = 15
LOCK_AGE_MIN = 15
QUEUE_WAIT_MIN = 10
REBASELINE_GAP_MIN = 15

_running = threading.Lock()
_last = {"at": 0.0}
_readings: dict = {}            # system -> {"at", "items"}
_readings_lock = threading.Lock()

LABELS = {
    "sap.sm66.bgd_saturation_pct": "Background work processes busy (all instances)",
    "sap.st22.user_dumps_10min": f"ABAP dumps by one user ({DUMP_WINDOW_MIN} min)",
    "sap.sm37.user_cancelled_15min": "Cancelled jobs by one user (15 min)",
    "sap.sm12.user_old_locks": "Locks held 15 min+ by one user",
    "sap.sm12.lock_held_min": "Longest-held lock",
    "sap.sm12.users_same_table_15min": "Users locking the same table (15 min)",
    "sap.sm58.failed_today": "Failed tRFC today (SYSFAIL + CPICERR)",
    "sap.smq2.waiting_10min": "Inbound queue entries waiting 10 min+",
    "sap.smq2.error_10min": "Inbound queue entries in error 10 min+",
    "sap.sm37.longest_active_job_min": "Longest-running background job",
    "sap.sm66.longest_wp_min": "Longest-running work process (all instances)",
}
UNITS = {"sap.sm66.bgd_saturation_pct": "%", "sap.sm12.lock_held_min": "min",
         "sap.sm37.longest_active_job_min": "min", "sap.sm66.longest_wp_min": "min"}
KEYS = tuple(LABELS)


def enabled() -> bool:
    return os.getenv("IBO_CUSTOM_ALERTS", "1").strip().lower() not in ("0", "false", "no", "off")


def interval_s() -> float:
    try:
        return max(60.0, float(os.getenv("IBO_CUSTOM_ALERT_MINUTES", "5") or 5) * 60)
    except ValueError:
        return 300.0


def _fmt(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _parse(s) -> datetime | None:
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _age_text(minutes: float) -> str:
    m = int(round(minutes))
    if m < 60:
        return f"{m} min ago"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min ago" if m else f"{h} h ago"
    return f"{h // 24} days ago"


def _hhmm(t) -> str:
    """'093015' or '09:30:15' -> '09:30'."""
    t = str(t or "").replace(":", "").strip()
    return f"{t[:2]}:{t[2:4]}" if len(t) >= 4 and t[:4].isdigit() else ""


def _since(sap_now: datetime, seconds: float) -> str:
    """Start time in SAP time with how long ago, from a duration."""
    start = sap_now - timedelta(seconds=seconds)
    return f"{start:%H:%M} ({_age_text(seconds / 60)})"


def _top(counter: dict, n: int = 3) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counter.items(), key=lambda kv: -kv[1])[:n])


# ---------------------------------------------------------------- seen memory

def _seen_last_read(system: str, kind: str) -> datetime | None:
    from core import ops_db
    row = ops_db.one("SELECT last_seen FROM custom_seen WHERE system=? AND kind=? AND ident='__read__'",
                     (system, kind))
    return _parse(row["last_seen"]) if row else None


def _mark_read(system: str, kind: str, now: datetime) -> None:
    from core import ops_db
    ops_db.execute("INSERT INTO custom_seen(system, kind, ident, first_seen, last_seen, counted) "
                   "VALUES (?,?,'__read__',?,?,0) ON CONFLICT(system, kind, ident) DO UPDATE SET "
                   "last_seen=excluded.last_seen", (system, kind, _fmt(now), _fmt(now)))


def _remember(system: str, kind: str, entries: list[tuple[str, str]], now: datetime,
              counted: int, drop_missing: bool) -> dict:
    """Insert entries not seen before; return {ident: first_seen}. drop_missing: forget entries not in this read."""
    from core import ops_db
    c = ops_db.db()
    stamp = _fmt(now)
    c.execute("BEGIN")
    try:
        c.executemany("INSERT INTO custom_seen(system, kind, ident, first_seen, last_seen, counted, info) "
                      "VALUES (?,?,?,?,?,?,?) ON CONFLICT(system, kind, ident) DO UPDATE SET "
                      "last_seen=excluded.last_seen",
                      [(system, kind, ident, stamp, stamp, counted, info) for ident, info in entries])
        if drop_missing:
            c.execute("DELETE FROM custom_seen WHERE system=? AND kind=? AND ident<>'__read__' AND last_seen<>?",
                      (system, kind, stamp))
        else:
            old = _fmt(now - timedelta(days=2))
            c.execute("DELETE FROM custom_seen WHERE system=? AND kind=? AND ident<>'__read__' AND last_seen<?",
                      (system, kind, old))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    rows = ops_db.rows("SELECT ident, first_seen, counted, info FROM custom_seen WHERE system=? AND kind=? "
                       "AND ident<>'__read__'", (system, kind))
    return {r["ident"]: r for r in rows}


# ---------------------------------------------------------------- the values

def _item(key, value, instance="", detail="", teams=None, **extra) -> dict:
    out = {"key": key, "label": LABELS[key], "value": value, "unit": UNITS.get(key, ""),
           "instance": instance, "detail": detail, "teams": list(teams or ["Basis"])}
    out.update(extra)
    return out


def _unknown(key, why) -> dict:
    return _item(key, None, detail=why, unknown=True)


def values(system: str, raw: dict, now: datetime | None = None) -> list[dict]:
    """The custom values from one raw read (collectors/custom_reads.read_all). Ungraded."""
    from core import alert_teams
    from core.alert_windows import minutes as _win
    now = now or datetime.now()
    sap_now = _parse(raw.get("sap_now")) or now
    w_dump = _win(system, "sap.st22.user_dumps_10min")
    w_job = _win(system, "sap.sm37.user_cancelled_15min")
    w_old = _win(system, "sap.sm12.user_old_locks")
    w_table = _win(system, "sap.sm12.users_same_table_15min")
    w_qwait = _win(system, "sap.smq2.waiting_10min")
    w_qerr = _win(system, "sap.smq2.error_10min")
    out: list[dict] = []

    # 1 + 9: work processes over all instances
    wp = raw.get("wp") or {}
    insts = wp.get("instances") or []
    if insts:
        total = sum(int(i.get("bgd_total") or 0) for i in insts)
        busy = sum(int(i.get("bgd_busy") or 0) for i in insts)
        detail = ", ".join(f"{i['name']} {i.get('bgd_busy', 0)}/{i.get('bgd_total', 0)}" for i in insts)
        skipped = wp.get("skipped") or []
        miss = f"; not readable: {', '.join(skipped)}" if skipped else ""
        jobs = sorted(((i["name"], b) for i in insts for b in (i.get("busy") or [])),
                      key=lambda x: -x[1]["seconds"])
        users = defaultdict(int)
        for _, b in jobs:
            users[b["user"] or "?"] += 1
        busy_txt = ""
        if jobs:
            busy_txt = ("; by user: " + _top(users, 5) + "; longest: "
                        + ", ".join(f"{b['report'] or '-'} ({b['user'] or '-'}, {n} WP {b['wp_no']}) "
                                    f"since {_since(sap_now, b['seconds'])}" for n, b in jobs[:4]))
        out.append(_item("sap.sm66.bgd_saturation_pct", round(busy / total * 100) if total else None,
                         detail=(f"{busy} of {total} background work processes busy at {sap_now:%H:%M} SAP time "
                                 f"({detail})" + busy_txt
                                 if total else "no background work processes configured") + miss))
        longest = [(i["name"], i["longest"]) for i in insts if i.get("longest")]
        if longest:
            name, lw = max(longest, key=lambda x: x[1]["seconds"])
            out.append(_item("sap.sm66.longest_wp_min", round(lw["seconds"] / 60, 1),
                             detail=f"{name} WP {lw['wp_no']} ({lw['type']}) {lw['report'] or '-'}, "
                                    f"user {lw['user'] or '-'}, busy since {_since(sap_now, lw['seconds'])}"
                                    + (f", {lw['action']}" if lw.get("action") else "")))
        else:
            out.append(_item("sap.sm66.longest_wp_min", 0, detail="no work process busy"))
    else:
        why = wp.get("error") or "work processes not readable"
        out += [_unknown("sap.sm66.bgd_saturation_pct", why), _unknown("sap.sm66.longest_wp_min", why)]

    # 2: dumps by one user in the last DUMP_WINDOW_MIN minutes
    dumps = raw.get("dumps") or {}
    if dumps.get("error"):
        out.append(_unknown("sap.st22.user_dumps_10min", dumps["error"]))
    else:
        last = _seen_last_read(system, "dump")
        baseline = last is None or now - last > timedelta(minutes=REBASELINE_GAP_MIN)
        entries = []
        for d in dumps.get("rows") or []:
            ident = "|".join(str(d.get(k, "")) for k in ("date", "time", "user", "error", "program", "host"))
            entries.append((ident, json.dumps({k: d.get(k, "") for k in
                                               ("user", "error", "exception", "component", "program")})))
        seen = _remember(system, "dump", entries, now, 0 if baseline else 1, drop_missing=False)
        _mark_read(system, "dump", now)
        if raw.get("sap_now"):
            _note_sap_clock(system, now, sap_now)
        since = now - timedelta(minutes=w_dump)
        per_user: dict = defaultdict(list)
        for r in seen.values():
            if r["counted"] and (_parse(r["first_seen"]) or now) >= since:
                info = json.loads(r["info"] or "{}")
                per_user[(info.get("user") or "?").upper()].append(info)
        for user, rows in sorted(per_user.items()):
            errs, progs, teams = defaultdict(int), defaultdict(int), []
            for d in rows:
                errs[d.get("error") or d.get("exception") or "?"] += 1
                if d.get("program"):
                    progs[d["program"]] += 1
                for t in alert_teams.teams_for_dump(system, d.get("error", ""), d.get("exception", ""),
                                                    d.get("component", "")):
                    if t not in teams:
                        teams.append(t)
            comps = sorted({d.get("component") for d in rows if d.get("component")})
            out.append(_item("sap.st22.user_dumps_10min", len(rows), instance=user, teams=teams,
                             detail=f"{len(rows)} dump(s) by {user}: {_top(errs)}"
                                    + (f"; programs {_top(progs)}" if progs else "")
                                    + (f"; components {', '.join(comps[:3])}" if comps else "")))
        out.append(_item("sap.st22.user_dumps_10min", max((len(v) for v in per_user.values()), default=0),
                         detail=f"most dumps by one user in the last {w_dump} minutes"
                                + ("; first read after a gap: existing dumps taken as known" if baseline else ""),
                         summary=True))

    # 3: cancelled jobs by one user in 15 minutes (SAP end times)
    canc = raw.get("cancelled") or {}
    if canc.get("error"):
        out.append(_unknown("sap.sm37.user_cancelled_15min", canc["error"]))
    else:
        since = sap_now - timedelta(minutes=w_job)
        per_user = defaultdict(list)
        for j in canc.get("rows") or []:
            end = _parse(j.get("end"))
            if end and end >= since:
                per_user[j.get("user") or "?"].append(j)
        emails = canc.get("emails") or {}
        for user, jobs in sorted(per_user.items()):
            names = defaultdict(int)
            for j in jobs:
                names[j.get("job") or "?"] += 1
            listed = sorted(jobs, key=lambda j: j.get("end") or "")
            when = ", ".join(f"{j.get('job') or '?'} at {str(j.get('end') or '')[11:16]}" for j in listed[:8]) \
                + (f" and {len(listed) - 8} more" if len(listed) > 8 else "")
            out.append(_item("sap.sm37.user_cancelled_15min", len(jobs), instance=user,
                             detail=f"{len(jobs)} job(s) scheduled by {user} cancelled in the last {w_job} minutes "
                                    f"(SAP time): {when}",
                             jobs=jobs, email=emails.get(user, "")))
        out.append(_item("sap.sm37.user_cancelled_15min", max((len(v) for v in per_user.values()), default=0),
                         detail=f"most cancelled jobs by one user in the last {w_job} minutes", summary=True))

    # 8: background jobs running long (per job)
    run = raw.get("running") or {}
    if run.get("error"):
        out.append(_unknown("sap.sm37.longest_active_job_min", run["error"]))
    else:
        per_job: dict = {}
        runs: dict = defaultdict(int)
        for j in run.get("rows") or []:
            runs[j.get("job") or "?"] += 1
            if j.get("minutes") is None:
                continue
            name = j.get("job") or "?"
            if name not in per_job or j["minutes"] > per_job[name]["minutes"]:
                per_job[name] = j
        for name, j in sorted(per_job.items()):
            out.append(_item("sap.sm37.longest_active_job_min", j["minutes"], instance=name,
                             detail=f"{name} (scheduled by {j.get('user') or '-'}) running since "
                                    f"{str(j.get('start') or '')[:16]} SAP time ({_age_text(j['minutes'])[:-4]})"
                                    + (f"; {runs[name]} runs of it active" if runs[name] > 1 else "")))
        top = max(per_job.values(), key=lambda j: j["minutes"], default=None)
        out.append(_item("sap.sm37.longest_active_job_min", top["minutes"] if top else 0,
                         detail=(f"{top['job']} by {top.get('user') or '-'}" if top else "no job running"),
                         summary=True))

    # 4: SM12
    locks = raw.get("locks") or {}
    keys4 = ("sap.sm12.user_old_locks", "sap.sm12.lock_held_min", "sap.sm12.users_same_table_15min")
    if locks.get("error"):
        out += [_unknown(k, locks["error"]) for k in keys4]
    else:
        rows = [r for r in locks.get("rows") or [] if not r.get("standing")]
        aged = [r for r in rows if r.get("age_min") is not None]
        if rows and not aged:
            out += [_unknown(k, "SAP gives no time for these locks on this release") for k in keys4]
        else:
            def at(r) -> str:
                """When the lock was set, in SAP time, with its age."""
                when = sap_now - timedelta(minutes=r["age_min"])
                return f"{when:%H:%M} ({_age_text(r['age_min'])})"

            # (a) one user holding many locks for 15+ minutes
            old_per_user: dict = defaultdict(list)
            for r in aged:
                if r["age_min"] >= w_old:
                    old_per_user[r["user"] or "?"].append(r)
            for user, locks in sorted(old_per_user.items()):
                tables = defaultdict(int)
                for r in locks:
                    tables[r["table"] or "?"] += 1
                first = max(locks, key=lambda r: r["age_min"])
                last = min(locks, key=lambda r: r["age_min"])
                out.append(_item("sap.sm12.user_old_locks", len(locks), instance=user,
                                 detail=f"{user} holds {len(locks)} lock(s) set {w_old}+ minutes ago, on "
                                        f"{_top(tables, 4)}; oldest set at {at(first)}, newest at {at(last)}"))
            out.append(_item("sap.sm12.user_old_locks", max((len(v) for v in old_per_user.values()), default=0),
                             detail=f"most locks held {w_old}+ minutes by one user", summary=True))
            # (b) the longest-held lock
            oldest = max(aged, key=lambda r: r["age_min"], default=None)
            out.append(_item("sap.sm12.lock_held_min", round(oldest["age_min"]) if oldest else 0,
                             detail=(f"{oldest['user']} on {oldest['table']} {oldest['arg']}".strip()
                                     + f", set at {at(oldest)}" if oldest else "no lock entries")))
            # (c) different users locking the same table within 15 minutes
            per_table: dict = defaultdict(dict)
            for r in aged:
                if r["age_min"] <= w_table:
                    u = r["user"] or "?"
                    prev = per_table[r["table"] or "?"].get(u)
                    if prev is None or r["age_min"] < prev["age_min"]:
                        per_table[r["table"] or "?"][u] = r          # each user's latest lock
            for table, users in sorted(per_table.items()):
                listed = sorted(users.values(), key=lambda r: -r["age_min"])
                out.append(_item("sap.sm12.users_same_table_15min", len(users), instance=table,
                                 detail=f"{len(users)} users locked {table} in the last {w_table} minutes: "
                                        + ", ".join(f"{r['user']} at {at(r)}" for r in listed[:8])
                                        + (f" and {len(listed) - 8} more" if len(listed) > 8 else "")))
            out.append(_item("sap.sm12.users_same_table_15min",
                             max((len(u) for u in per_table.values()), default=0),
                             detail=f"most different users on one table in the last {w_table} minutes", summary=True))

    # 6: SM58 today
    sm58 = raw.get("sm58") or {}
    if sm58.get("error") or sm58.get("count") is None:
        out.append(_unknown("sap.sm58.failed_today", sm58.get("error") or "not read"))
    else:
        detail = f"by destination: {_top(sm58.get('dests') or {})}" if sm58["count"] else "none today"
        ent = sm58.get("rows") or []
        if sm58["count"] and ent:
            users, funcs, states = defaultdict(int), defaultdict(int), defaultdict(int)
            for r in ent:
                states[r.get("state") or "?"] += 1
                if r.get("user"):
                    users[r["user"]] += 1
                if r.get("function"):
                    funcs[r["function"]] += 1
            detail += f"; {_top(states)}"
            if users:
                detail += f"; by user: {_top(users, 4)}"
            if funcs:
                detail += f"; function: {_top(funcs, 3)}"
            times = sorted(t for t in (_hhmm(r.get("time")) for r in ent) if t)
            if times:
                detail += f"; first {times[0]}, latest {times[-1]} SAP time"
            with_msg = [r for r in ent if r.get("message")]
            msg = max(with_msg, key=lambda r: str(r.get("time") or "")).get("message") if with_msg else ""
            if msg:
                detail += f"; error: {msg}"
        out.append(_item("sap.sm58.failed_today", sm58["count"],
                         detail=detail + ("; read limit reached, at least this many" if sm58.get("capped") else "")))

    # 7: SMQ2 waiting / in error for 10 minutes
    q = raw.get("smq2") or {}
    if q.get("error"):
        out += [_unknown("sap.smq2.waiting_10min", q["error"]), _unknown("sap.smq2.error_10min", q["error"])]
    else:
        rows = q.get("rows") or []
        by_id: dict = {}
        for r in rows:
            by_id.setdefault(r["id"], r)
        capped = bool(q.get("capped"))
        seen = _remember(system, "smq2", [(i, "") for i in by_id], now, 1, drop_missing=not capped)
        cutoff = now - timedelta(minutes=w_qwait)
        cutoff_e = now - timedelta(minutes=w_qerr)
        wait_q, err_q = defaultdict(int), defaultdict(int)
        waiting = errored = 0
        oldest_w = oldest_e = None
        w_users, e_users = defaultdict(int), defaultdict(int)
        e_msg = ""
        from collectors.custom_reads import QRFC_ERROR_STATES
        for r in rows:
            first = _parse((seen.get(r["id"]) or {}).get("first_seen")) or now
            if r.get("state") in QRFC_ERROR_STATES and first <= cutoff_e:
                errored += 1
                err_q[f"{r['queue']} ({r['state']})"] += 1
                oldest_e = first if oldest_e is None or first < oldest_e else oldest_e
                if r.get("user"):
                    e_users[r["user"]] += 1
                e_msg = e_msg or r.get("message") or ""
            if first <= cutoff:
                waiting += 1
                wait_q[r["queue"]] += 1
                oldest_w = first if oldest_w is None or first < oldest_w else oldest_w
                if r.get("user"):
                    w_users[r["user"]] += 1

        def _q_more(oldest, users, msg=""):
            t = ""
            if oldest is not None:
                t += f"; waiting since {oldest:%H:%M} ({_age_text((now - oldest).total_seconds() / 60)})"
            if users:
                t += f"; by user: {_top(users, 4)}"
            if msg:
                t += f"; error: {msg}"
            return t
        note = "" if q.get("id_kind") == "entry" else "; counted per queue (no entry id on this release)"
        if capped:
            note += f"; read limit of {len(rows)} entries reached, so at least this many"
        out.append(_item("sap.smq2.waiting_10min", waiting,
                         detail=(f"queues: {_top(wait_q)}{_q_more(oldest_w, w_users)}" if waiting
                                 else f"nothing waiting {w_qwait}+ minutes") + note))
        out.append(_item("sap.smq2.error_10min", errored,
                         detail=(f"queues: {_top(err_q)}{_q_more(oldest_e, e_users, e_msg)}" if errored
                                 else f"nothing in error {w_qerr}+ minutes") + note))
    from core.alert_windows import WINDOWS as _W, label as _wlabel
    for it in out:
        if it.get("key") in _W:
            it["label"] = _wlabel(system, it["key"])      # "(30 min)" when the window is 30
    return out


# ---------------------------------------------------------------- grading and alerting

# ---------------------------------------------------------------- dump breakdown

DUMP_BREAKDOWN_STALE_MIN = 30
_SAP_CLOCK_KEY = "sap_clock_offset:"


def _note_sap_clock(system: str, now: datetime, sap_now: datetime) -> None:
    """SAP's clock minus ours, in seconds -- to place 'today' and dump times in SAP time."""
    from core import ops_db
    try:
        ops_db.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                       "value=excluded.value", (_SAP_CLOCK_KEY + system,
                                                str(int(round((sap_now - now).total_seconds())))))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"{system}: SAP clock offset not stored: {exc}")


def _sap_clock(system: str) -> int:
    from core import ops_db
    try:
        row = ops_db.one("SELECT value FROM meta WHERE key=?", (_SAP_CLOCK_KEY + system,))
        return int(float(row["value"])) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _dump_time(ident: str) -> datetime | None:
    parts = str(ident or "").split("|")
    if len(parts) < 2:
        return None
    d, t = parts[0].strip().replace("-", ""), parts[1].strip().replace(":", "")
    try:
        return datetime.strptime(d[:8] + t[:6].ljust(6, "0"), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _counts(counter: dict, n: int = 5, more: str = "") -> str:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    text = ", ".join(f"{k} {v}" for k, v in items[:n])
    if len(items) > n and more:
        text += f" (+{len(items) - n} more {more})"
    return text


def dump_breakdown(system: str, user: str = "", now: datetime | None = None) -> list[tuple[str, str]]:
    """
    Extra lines for an ABAP dump alert mail, from the dumps the custom check
    has seen (table custom_seen, kind 'dump'):

        Last 10 minutes      20 dump(s) by 2 user(s): U1 14, U2 6 · teams: Basis 12, ABAP 8 · errors ...
        Busiest 10 minutes   01:10-01:20 SAP time: 31 dump(s) (U1 25, U2 6)
        Today by hour        00h 3 · 01h 45 (peak) · 02h 7 ...
        Today by team        ABAP 101, Basis 28
        Top users today      U1 60, U2 30, ...

    "Last 10 minutes" counts dumps by when the check first saw them (like
    the per-user alert), so it is right whatever clock the dump log uses.
    The hour and busiest-10-minute lines use the dump log's own times; when
    those prove to be off SAP's clock by a whole half-hour step (a log in
    UTC), they are moved onto SAP's clock and the line says so.
    `user` limits everything to one user (the per-user alert).
    Empty when the dumps have not been read recently (not authorised, or the
    check is off) -- the mail then shows only the counter's own detail.
    """
    from core.alert_windows import minutes as _win
    _wd = _win(system, "sap.st22.user_dumps_10min")
    from core import alert_teams, ops_db
    now = now or datetime.now()
    last = _seen_last_read(system, "dump")
    if last is None or now - last > timedelta(minutes=DUMP_BREAKDOWN_STALE_MIN):
        return []
    off = timedelta(seconds=_sap_clock(system))
    sap_now = now + off
    rows = []
    teams_memo: dict = {}
    # Dumps still in the dump log at the last read (today's, SAP day): every
    # read refreshes last_seen, so older days drop out of this query.
    for r in ops_db.rows("SELECT ident, first_seen, counted, info FROM custom_seen WHERE system=? "
                         "AND kind='dump' AND ident<>'__read__' AND last_seen>=?",
                         (system, _fmt(last - timedelta(minutes=1)))):
        try:
            info = json.loads(r.get("info") or "{}")
        except ValueError:
            info = {}
        u = str(info.get("user") or "?").upper()
        if user and u != str(user).upper():
            continue
        tk = (info.get("error", ""), info.get("exception", ""), info.get("component", ""))
        if tk not in teams_memo:
            teams_memo[tk] = alert_teams.teams_for_dump(system, *tk)
        teams = teams_memo[tk]
        rows.append({"at": _dump_time(r["ident"]), "first": _parse(r["first_seen"]),
                     "counted": int(r.get("counted") or 0), "user": u, "teams": teams,
                     "error": info.get("error") or info.get("exception") or "?"})

    # The dump log's clock against SAP's: a dump first seen by a regular read
    # appeared shortly before that read.
    deltas = sorted(((x["first"] + off) - x["at"]).total_seconds() for x in rows
                    if x["counted"] and x["at"] and x["first"])
    shift = timedelta(0)
    if deltas:
        med = deltas[len(deltas) // 2]
        if abs(med) > 25 * 60:
            shift = timedelta(seconds=round(med / 1800) * 1800)
    for x in rows:
        x["sap_at"] = (x["at"] + shift) if x["at"] else None

    out: list[tuple[str, str]] = []
    who = f" by {user}" if user else ""

    recent = [x for x in rows if x["counted"] and x["first"] and x["first"] >= now - timedelta(minutes=_wd)]
    if recent:
        users, teams, errs = defaultdict(int), defaultdict(int), defaultdict(int)
        for x in recent:
            users[x["user"]] += 1
            errs[x["error"]] += 1
            for t in x["teams"]:
                teams[t] += 1
        parts = [f"{len(recent)} new dump(s)" + (f"{who}" if user else
                                                  f" by {len(users)} user(s): {_counts(users, 5, 'users')}")]
        parts.append(f"teams: {_counts(teams, 8)}" + (" (a dump can count for more than one team)"
                                                       if sum(teams.values()) > len(recent) else ""))
        parts.append(f"errors: {_counts(errs, 3, 'errors')}")
        out.append((f"Last {_wd} minutes", " · ".join(parts)))
    else:
        out.append((f"Last {_wd} minutes", f"no new dumps{who}"))

    today = [x for x in rows if x["sap_at"] and x["sap_at"].date() == sap_now.date()]
    if today:
        moved = (f" (dump log times moved {'+' if shift > timedelta(0) else '-'}"
                 f"{abs(shift.total_seconds()) / 3600:g} h onto SAP time)" if shift else "")
        # busiest 10 minutes: the window starting at each dump
        times = sorted(x["sap_at"] for x in today)
        best_n, best_i, j = 0, 0, 0
        for i, t in enumerate(times):
            while times[j] < t - timedelta(minutes=_wd) + timedelta(seconds=1):
                j += 1
            if i - j + 1 > best_n:
                best_n, best_i = i - j + 1, i
        if best_n:
            end = times[best_i]
            start = end - timedelta(minutes=_wd)
            inside = [x for x in today if start < x["sap_at"] <= end]
            busiest_users = defaultdict(int)
            for x in inside:
                busiest_users[x["user"]] += 1
            out.append((f"Busiest {_wd} minutes today",
                        f"{start:%H:%M}-{end:%H:%M} SAP time: {best_n} dump(s)"
                        + ("" if user else f" ({_counts(busiest_users, 3, 'users')})") + moved))
        hours = defaultdict(int)
        for x in today:
            hours[x["sap_at"].hour] += 1
        peak = max(hours.values())
        peak_h = min(h for h, n in hours.items() if n == peak)       # the first hour it peaked
        out.append((f"Today by hour{' (' + user + ')' if user else ''}",
                    " · ".join(f"{h:02d}h {n}" + (" (peak)" if h == peak_h and peak > 1 else "")
                               for h, n in sorted(hours.items()))
                    + f" -- {len(today)} today, first {times[0]:%H:%M}, latest {times[-1]:%H:%M} SAP time"))
        tteams, tusers = defaultdict(int), defaultdict(int)
        for x in today:
            tusers[x["user"]] += 1
            for t in x["teams"]:
                tteams[t] += 1
        out.append(("Today by team", _counts(tteams, 8) + (" (a dump can count for more than one team)"
                                                          if sum(tteams.values()) > len(today) else "")))
        if not user:
            out.append(("Top users today", _counts(tusers, 5, "users")))
    return out


def grade(system: str, items: list[dict]) -> list[dict]:
    from core import grading as g
    out = []
    for it in items:
        gr = g.grade_number(it["key"], it.get("value"), system)
        it = {**it, **{k: gr[k] for k in ("warning", "critical", "direction", "status")}}
        if it.get("unknown"):
            it["status"] = g.UNKNOWN
        out.append(it)
    return out


def _approvals(system: str, items: list[dict]) -> None:
    """A waiting "mail the user" request for each user whose cancelled jobs alert."""
    from core import alert_engine, job_user_mail
    rule = alert_engine.rule_for(system, "sap.sm37.user_cancelled_15min")
    for it in items:
        if it["key"] != "sap.sm37.user_cancelled_15min" or not it.get("instance") or it.get("summary"):
            continue
        if not alert_engine._triggers(rule, it["status"]):
            continue
        if job_user_mail.excluded(it["instance"]):
            it["detail"] += f" ({it['instance']} is a system account: no user mail)"
            continue
        req = job_user_mail.raise_request(system, it["instance"], it.get("email", ""), it.get("jobs") or [])
        if req:
            it["approval_id"] = req["id"]


def _close_missing(system: str, items: list[dict]) -> list[dict]:
    """Per-user / per-table / per-job values that are no longer there are back to normal."""
    from core import ops_db
    present = {(it["key"], it.get("instance") or "") for it in items}
    readable = {it["key"] for it in items if not it.get("unknown")}
    extra = []
    for key in readable:
        # Rows already back to NORMAL are kept only to remember the last
        # raise (core/alert_engine.py); they need no closing.
        for r in ops_db.rows("SELECT skey, teams FROM alert_state WHERE system=? AND skey LIKE ? "
                             "AND status<>'NORMAL'", (system, key + "@%")):
            inst = r["skey"][len(key) + 1:]
            if (key, inst) not in present:
                teams = [t for t in str(r.get("teams") or "").split(",") if t] or ["Basis"]
                extra.append({"key": key, "label": LABELS.get(key, key), "value": 0, "unit": UNITS.get(key, ""),
                              "instance": inst, "status": "NORMAL", "detail": "no longer seen", "teams": teams})
    return extra


def process(system_cfg: dict, raw: dict, now: datetime | None = None, sender=None) -> list[dict]:
    """Values -> graded -> alert engine. Returns the graded items (also kept for the Thresholds page)."""
    from core import alert_engine
    system = str(system_cfg.get("name") or "")
    items = grade(system, values(system, raw, now))
    _approvals(system, items)
    # Per-user / per-table / per-job keys alert per instance; the summary line
    # of such a key is for the Thresholds page only.
    per_instance = {"sap.st22.user_dumps_10min", "sap.sm37.user_cancelled_15min", "sap.sm12.user_old_locks",
                    "sap.sm12.users_same_table_15min", "sap.sm37.longest_active_job_min"}
    feed = [it for it in items if not (it["key"] in per_instance and not it.get("instance"))]
    feed += _close_missing(system, items)
    alert_engine.observe(system_cfg, feed, source="custom", sender=sender)
    with _readings_lock:
        _readings[system] = {"at": _fmt(now or datetime.now()), "items": items}
    return items


def readings(system: str) -> dict:
    with _readings_lock:
        return dict(_readings.get(system) or {})


def maybe_start() -> bool:
    if not enabled():
        return False
    if time.monotonic() - _last["at"] < interval_s() or not _running.acquire(blocking=False):
        return False
    _last["at"] = time.monotonic()

    def _go():
        try:
            run_all()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"custom alerts failed: {type(exc).__name__}: {exc}")
        finally:
            _running.release()
    threading.Thread(target=_go, daemon=True, name="custom-alerts").start()
    return True


def _skipped(system: str, reason: str) -> None:
    """Remember why a system was not checked, so the Thresholds page says it instead of a bare dash."""
    log.info(f"{system}: custom alerts not checked: {reason}")
    with _readings_lock:
        _readings[system] = {"at": _fmt(datetime.now()), "items": [], "skipped": reason or "not checked"}


def _why_not_eligible(cfg: dict) -> str:
    name = cfg.get("name")
    try:
        from core import alert_control
        why = alert_control.sweep_block_reason(name)
        if why:
            return f"not checked: {why}"
    except Exception:  # noqa: BLE001
        pass
    try:
        from core import standby
        if not standby.active(cfg):
            return "not checked: the system is on standby"
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.reachability import current_failures
        if name in current_failures():
            return "not checked: the system is not reachable"
    except Exception:  # noqa: BLE001
        pass
    return "not checked"


def run_all() -> int:
    from collectors.checks_job import collect_custom_job
    from core import rfc_proc
    from core.check_alerts import _eligible
    from core.config_loader import get_all_systems
    done = 0
    for cfg in get_all_systems():
        name = cfg.get("name")
        if not name or not (cfg.get("rfc") or {}).get("ashost"):
            continue
        if not _eligible(cfg):
            _skipped(name, _why_not_eligible(cfg))
            continue
        try:
            from core import reachability
            vpn = reachability.precheck(name, cfg)
            if vpn:
                _skipped(name, vpn)
                continue
            raw = rfc_proc.call(collect_custom_job, name, cfg, timeout=300)
        except Exception as exc:  # noqa: BLE001
            log.info(f"{name}: custom alerts read failed: {type(exc).__name__}: {exc}")
            _skipped(name, f"read failed: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        if not raw.get("ok"):
            log.info(f"{name}: custom alerts: RFC logon failed: {raw.get('error', '')}")
            _skipped(name, f"RFC logon failed: {str(raw.get('error', ''))[:160]}")
            continue
        try:
            process(cfg, raw["data"])
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(f"{name}: custom alerts processing failed: {type(exc).__name__}: {exc}")
    return done
