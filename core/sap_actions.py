"""
One-click SAP actions, with approval: restart a cancelled job, release an
old lock, restart a stuck inbound queue. Each needs a Basis click, shows
what it will touch before it touches it, and is logged with who asked, who
approved, and what SAP answered.

SWITCHED OFF UNTIL YOU SWITCH IT ON
  .env          IBO_ACTIONS_ENABLED=1
  systems.yaml  actions: [restart_job, release_lock, restart_queue]   per system
Both are needed. Try every action on a QAS system first.

APPROVAL
  IBO_ACTIONS_TWO_PERSON=0 (default)  the person who asks confirms in a
                                      dialog that shows exactly what will
                                      happen, and types a reason
  IBO_ACTIONS_TWO_PERSON=1            a second operator/admin must approve
A request not approved within 60 minutes expires.

WHAT EACH ACTION DOES ON THE SAP SIDE
  restart_job    XBP interface: BAPI_XMI_LOGON (XBP), BAPI_XBP_JOB_COPY
                 (a copy of the cancelled job, same steps and variants),
                 BAPI_XBP_JOB_START_IMMEDIATELY on the copy. The cancelled
                 job itself stays as it is, for its log. Refused unless the
                 job is cancelled (TBTCO status A).
                 RFC user needs S_XMI_PROD (XBP) and S_BTCH_ADM / S_BTCH_JOB.
  release_lock   ENQUE_READ2 to find the entries, ENQUE_DELETE to delete
                 exactly those. Refused when the lock is younger than
                 IBO_ACTION_LOCK_MIN_AGE_MIN (default 60) or the lock owner
                 has update requests waiting in VBHDR (deleting then can
                 corrupt data). RFC user needs S_ENQUE.
  restart_queue  TRFC_QIN_RESTART for an inbound qRFC queue (SMQ2). Do it
                 after the cause of the SYSFAIL is fixed.
If a function module is not remote-enabled on a release, the action fails
with that message and changes nothing; use the transaction instead.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

from core import ops_db, timeline
from utils.logger import get_logger

log = get_logger(__name__)

XMI = {"EXTCOMPANY": "InfraBeatOps", "EXTPRODUCT": "InfraBeatOps"}
EXT_USER = "INFRABEATOPS"

CATALOG = {
    "restart_job": {
        "label": "Restart cancelled job",
        "params": ["job", "job_count"],
        "what": "Creates a copy of the cancelled background job and starts the copy immediately.",
        "tcode": "SM37",
    },
    "release_lock": {
        "label": "Release old lock",
        "params": ["user", "object", "argument"],
        "what": "Deletes the matching SM12 lock entries of that user (only locks older than the "
                "minimum age, and only when the user has no update requests waiting).",
        "tcode": "SM12",
    },
    "restart_queue": {
        "label": "Restart inbound queue",
        "params": ["queue"],
        "what": "Restarts (activates) the inbound qRFC queue so its entries are processed again.",
        "tcode": "SMQ2",
    },
    "unlock_user": {
        "label": "Unlock user",
        "params": ["user"],
        "what": "Unlocks a locked SAP user (BAPI_USER_UNLOCK). The password is not changed.",
        "tcode": "SU01",
    },
    "cancel_job": {
        "label": "Cancel running job",
        "params": ["job", "job_count"],
        "what": "Cancels an active (running) background job through the XBP interface (BAPI_XBP_JOB_ABORT).",
        "tcode": "SM37",
    },
}

# Function modules each action calls, with the parameters it passes. Each is
# checked against the system's own interface (pyrfc) before anything is run,
# so a release that names a parameter differently refuses cleanly in the
# preview instead of failing half-way.
REQUIRED_FMS = {
    "restart_job": {"BAPI_XMI_LOGON": ["EXTCOMPANY", "EXTPRODUCT", "INTERFACE", "VERSION"],
                    "BAPI_XBP_JOB_COPY": ["SOURCE_JOBNAME", "SOURCE_JOBCOUNT", "TARGET_JOBNAME", "EXTERNAL_USER_NAME"],
                    "BAPI_XBP_JOB_START_IMMEDIATELY": ["JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME"]},
    "cancel_job": {"BAPI_XMI_LOGON": ["EXTCOMPANY", "EXTPRODUCT", "INTERFACE", "VERSION"],
                   "BAPI_XBP_JOB_ABORT": ["JOBNAME", "JOBCOUNT", "EXTERNAL_USER_NAME"]},
    "release_lock": {"ENQUE_READ2": ["GCLIENT", "GUNAME"], "ENQUE_DELETE": ["ENQ"]},
    "restart_queue": {"TRFC_QIN_RESTART": ["QNAME"]},
    "unlock_user": {"BAPI_USER_UNLOCK": ["USERNAME"]},
}


def _protected_users() -> set[str]:
    raw = os.getenv("IBO_ACTION_PROTECTED_USERS", "SAP*,DDIC,TMSADM")
    return {u.strip().upper() for u in raw.split(",") if u.strip()}


def _require(session, action: str) -> None:
    """Raise ValueError when a function this action needs is missing or differs on this system."""
    from collectors.extra_checks import iface
    for fm, names in REQUIRED_FMS.get(action, {}).items():
        params = iface(session, fm)
        if params is None:
            raise ValueError(f"{fm} does not exist on {session.system} -- do this in "
                             f"{CATALOG[action]['tcode']} instead.")
        missing = [n for n in names if n not in params]
        if missing:
            raise ValueError(f"{fm} on {session.system} has no parameter(s) {', '.join(missing)} -- "
                             f"not run; do this in {CATALOG[action]['tcode']}.")


# -------------------------------------------------------- approval policy

def _policy_rules() -> dict:
    try:
        import yaml
        from core.config_loader import CONFIG_DIR
        path = os.path.join(CONFIG_DIR, "action_policy.yaml")
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"config/action_policy.yaml unreadable: {exc}")
        return {}


def mode_for(system: str, action: str) -> str:
    """'confirm' (the requester's click runs it) or 'two_person' (someone else approves).

    IBO_ACTIONS_TWO_PERSON=1 is a floor: every action needs a second person,
    whatever config/action_policy.yaml says (a standing approval still applies)."""
    import fnmatch
    if two_person():
        return "two_person"
    cfg = _policy_rules()
    default = "two_person" if two_person() else str(cfg.get("default") or "confirm")
    for rule in cfg.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        systems = [str(x) for x in (rule.get("systems") or ["*"])]
        actions = [str(x) for x in (rule.get("actions") or ["*"])]
        if any(fnmatch.fnmatchcase(system, p) for p in systems) and \
                any(fnmatch.fnmatchcase(action, p) for p in actions):
            mode = str(rule.get("approval") or default)
            return mode if mode in ("confirm", "two_person") else default
    return default if default in ("confirm", "two_person") else "confirm"


def standing_approval(system: str, action: str) -> dict | None:
    """An active time-boxed approval covering this system and action."""
    try:
        now = ops_db.now()
        return ops_db.one("SELECT * FROM action_approvals WHERE revoked_at IS NULL AND starts_at<=? AND ends_at>? "
                          "AND (system=? OR system='*') AND (action=? OR action='*') ORDER BY id DESC LIMIT 1",
                          (now, now, system, action))
    except Exception:
        return None


def add_standing_approval(system: str, action: str, minutes: float, reason: str, by: str = "") -> dict:
    by = by or timeline.actor_name()
    if action != "*" and action not in CATALOG:
        raise ValueError("Unknown action.")
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        raise ValueError("minutes must be a number")
    if not 5 <= minutes <= 24 * 60:
        raise ValueError("A standing approval lasts between 5 minutes and 24 hours.")
    if not str(reason or "").strip():
        raise ValueError("Give a reason.")
    start = datetime.now()
    aid = ops_db.execute("INSERT INTO action_approvals(system, action, starts_at, ends_at, reason, created_by, "
                         "created_at) VALUES (?,?,?,?,?,?,?)",
                         (system, action, ops_db.fmt(start), ops_db.fmt(start + timedelta(minutes=minutes)),
                          reason.strip()[:300], by, ops_db.now()))
    for s_ in ([system] if system != "*" else ["IBOPS"]):
        timeline.add(s_, "action", f"Standing approval for {action if action != '*' else 'all actions'} "
                     f"until {start + timedelta(minutes=minutes):%H:%M}", reason, actor=by)
    return ops_db.one("SELECT * FROM action_approvals WHERE id=?", (aid,))


def revoke_standing_approval(aid: int, by: str = "") -> None:
    ops_db.execute("UPDATE action_approvals SET revoked_at=?, revoked_by=? WHERE id=?",
                   (ops_db.now(), by or timeline.actor_name(), int(aid)))


def standing_approvals() -> list[dict]:
    return ops_db.rows("SELECT * FROM action_approvals WHERE revoked_at IS NULL AND ends_at>? ORDER BY ends_at",
                       (ops_db.now(),))


def globally_enabled() -> bool:
    return os.getenv("IBO_ACTIONS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def two_person() -> bool:
    return os.getenv("IBO_ACTIONS_TWO_PERSON", "0").strip().lower() in ("1", "true", "yes", "on")


def _cfg(system: str) -> dict | None:
    from core.config_loader import get_all_systems
    return next((s for s in get_all_systems() if s.get("name") == system), None)


def allowed(system: str, cfg: dict | None = None) -> list[str]:
    """Actions enabled for this system (empty when switched off)."""
    if not globally_enabled():
        return []
    cfg = cfg if cfg is not None else (_cfg(system) or {})
    raw = cfg.get("actions") or []
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",")]
    return [a for a in raw if a in CATALOG]


def _check(system: str, action: str, params: dict) -> dict:
    if action not in CATALOG:
        raise ValueError("Unknown action.")
    cfg = _cfg(system)
    if cfg is None:
        raise ValueError("Unknown system.")
    if not globally_enabled():
        raise ValueError("One-click actions are switched off (IBO_ACTIONS_ENABLED=1 in .env).")
    if action not in allowed(system, cfg):
        raise ValueError(f"{CATALOG[action]['label']} is not enabled for {system} "
                         f"(add it to 'actions:' for the system on the Systems page).")
    if not (cfg.get("rfc") or {}).get("ashost"):
        raise ValueError(f"{system} has no RFC connection configured.")
    clean = {}
    for p in CATALOG[action]["params"]:
        v = str((params or {}).get(p) or "").strip()
        if p in ("job", "job_count", "queue", "user") and not v:
            raise ValueError(f"'{p}' is required.")
        if len(v) > 120 or any(c in v for c in "'\"\n\r"):
            raise ValueError(f"'{p}' contains characters that are not allowed.")
        clean[p] = v.upper() if p in ("job", "user", "queue", "object") else v
    return {"cfg": cfg, "params": clean}


def _ret_error(result) -> str | None:
    """An E/A message from a BAPI RETURN (structure or table), or None."""
    if not result:
        return None
    ret = result.get("RETURN")
    rows = ret if isinstance(ret, list) else [ret] if isinstance(ret, dict) else []
    for r in rows:
        if str(r.get("TYPE", "")).upper() in ("E", "A"):
            return str(r.get("MESSAGE") or r.get("MESSAGE_V1") or "error").strip()
    return None


def _session(system: str, cfg: dict):
    from collectors.rfc_collector import SapSession
    return SapSession(system, cfg)


def _param_fields(session, fm: str, param: str) -> set[str] | None:
    """Field names of a table parameter, from the function's interface (pyrfc)."""
    try:
        desc = session.conn.get_function_description(fm)
        for p in desc.parameters:
            if p.get("name") == param:
                td = p.get("type_description")
                return {f["name"] for f in td.fields} if td is not None else None
    except Exception:
        return None
    return None


def _has_param(session, fm: str, param: str) -> bool:
    try:
        desc = session.conn.get_function_description(fm)
        return any(p.get("name") == param for p in desc.parameters)
    except Exception:
        return False


# ----------------------------------------------------------------- previews

def _lock_age_min() -> float:
    try:
        return float(os.getenv("IBO_ACTION_LOCK_MIN_AGE_MIN", "60") or 60)
    except ValueError:
        return 60.0


def _locks(session, cfg: dict, user: str = "") -> list[dict]:
    from collectors.rfc_perf import _lock_timestamp
    try:   # lock stamps are in the app server's clock; use rfc_perf's SAP-time correction
        from collectors.rfc_perf import sap_now, _owner_offset_min
        ref = sap_now(session.system) + timedelta(minutes=_owner_offset_min.get(session.system, 0))
    except Exception:
        ref = datetime.now()
    res = session.call("ENQUE_READ2", GCLIENT=str(cfg.get("client") or (cfg.get("rfc") or {}).get("client") or "000"),
                       GUNAME=user)
    if res is None:
        raise RuntimeError(session.last_error or "ENQUE_READ2 failed")
    out = []
    for e in res.get("ENQ") or []:
        # ENQUE_READ2 filters by GUNAME on the SAP side; filter here as well so
        # a release can never reach another user's entries.
        if user and str(e.get("GUNAME", "")).strip() != user:
            continue
        stamp = _lock_timestamp(e)
        out.append({"entry": e, "user": str(e.get("GUNAME", "")).strip(), "object": str(e.get("GNAME", "")).strip(),
                    "argument": str(e.get("GARG", "")).strip(), "mode": str(e.get("GMODE", "")).strip(),
                    "age_min": max(0, round((ref - stamp).total_seconds() / 60)) if stamp else None,
                    "since": stamp.strftime("%Y-%m-%d %H:%M") if stamp else ""})
    return out


def _pending_updates(session, user: str) -> int | None:
    try:
        rows = session.read_table("VBHDR", ["VBUSR"], f"VBUSR = '{user}'", rows=50)
        return len(rows or [])
    except Exception:
        return None


def preview(system: str, action: str, params: dict) -> dict:
    """Read-only look at what the action would touch. Raises ValueError when refused."""
    chk = _check(system, action, params)
    cfg, p = chk["cfg"], chk["params"]
    with _session(system, cfg) as s:
        if not s.ok:
            raise ValueError(f"{system} is not reachable over RFC: {s.error}")
        _require(s, action)
        if action == "unlock_user":
            if p["user"] in _protected_users():
                raise ValueError(f"{p['user']} is protected (IBO_ACTION_PROTECTED_USERS) -- unlock it in SU01.")
            rows = s.read_table("USR02", ["BNAME", "UFLAG", "USTYP"], f"BNAME = '{p['user']}'", rows=1)
            if rows is None:
                raise ValueError(f"Could not read USR02: {s.last_error}")
            if not rows:
                raise ValueError(f"User {p['user']} does not exist.")
            try:
                flag = int(str(rows[0][1]).strip() or 0)
            except ValueError:
                flag = 0
            if not flag:
                raise ValueError(f"User {p['user']} is not locked.")
            why = ("locked after too many failed logons" if flag & 128 else
                   "locked by an administrator" if flag & 64 else f"locked (UFLAG {flag})")
            return {"text": f"Unlock user {p['user']} ({why}). The password is not changed."}
        if action == "cancel_job":
            rows = s.read_table("TBTCO", ["JOBNAME", "JOBCOUNT", "STATUS", "SDLUNAME", "STRTDATE", "STRTTIME"],
                                f"JOBNAME = '{p['job']}' AND JOBCOUNT = '{p['job_count']}'", rows=2)
            if rows is None:
                raise ValueError(f"Could not read TBTCO: {s.last_error}")
            if not rows:
                raise ValueError(f"Job {p['job']} ({p['job_count']}) not found.")
            row = [str(x).strip() for x in rows[0]]
            if row[2] != "R":
                raise ValueError(f"Job {p['job']} is not running (status {row[2]}).")
            return {"text": f"Cancel the running job {p['job']} (count {p['job_count']}, scheduled by "
                            f"{row[3] or '?'}, started {row[4]} {row[5]}). Work it has done is not rolled back."}
        if action == "restart_job":
            rows = s.read_table("TBTCO", ["JOBNAME", "JOBCOUNT", "STATUS", "SDLUNAME", "ENDDATE", "ENDTIME"],
                                f"JOBNAME = '{p['job']}' AND JOBCOUNT = '{p['job_count']}'", rows=2)
            if rows is None:
                raise ValueError(f"Could not read TBTCO: {s.last_error}")
            if not rows:
                raise ValueError(f"Job {p['job']} ({p['job_count']}) not found in TBTCO.")
            row = [str(x).strip() for x in rows[0]]
            if row[2] != "A":
                raise ValueError(f"Job {p['job']} is not cancelled (status {row[2]}); nothing to restart.")
            return {"text": f"Copy job {p['job']} (count {p['job_count']}, scheduled by {row[3] or '?'}, "
                            f"cancelled {row[4]} {row[5]}) and start the copy now.", "job": row}
        if action == "release_lock":
            locks = [x for x in _locks(s, cfg, p["user"])
                     if (not p.get("object") or x["object"] == p["object"])
                     and (not p.get("argument") or x["argument"].startswith(p["argument"]))]
            if not locks:
                raise ValueError("No matching lock entries (they may have been released already).")
            young = [x for x in locks if x["age_min"] is None or x["age_min"] < _lock_age_min()]
            if young:
                raise ValueError(f"{len(young)} matching lock(s) are younger than {int(_lock_age_min())} min "
                                 f"or have no timestamp -- not released. Narrow the object/argument.")
            pend = _pending_updates(s, p["user"])
            if pend is None:
                raise ValueError("Could not check VBHDR for update requests of this user -- not releasing "
                                 "(release in SM12 after checking SM13).")
            if pend:
                raise ValueError(f"User {p['user']} has {pend} update request(s) waiting (SM13). Deleting "
                                 f"the locks now can corrupt data -- fix the update first.")
            return {"text": f"Delete {len(locks)} lock entr{'y' if len(locks) == 1 else 'ies'} of user {p['user']}: "
                            + "; ".join(f"{x['object']} {x['argument'][:40]} (since {x['since']})" for x in locks[:5])
                            + (" ..." if len(locks) > 5 else ""),
                    "count": len(locks),
                    # Exactly these entries are deleted at execution time, even
                    # if approval comes an hour later and new locks appeared.
                    "entries": [_lock_id(x) for x in locks]}
        if action == "restart_queue":
            n = None
            rows = s.read_table("TRFCQIN", ["QNAME"], f"QNAME = '{p['queue']}'", rows=1000)
            n = None if rows is None else len(rows)
            if n == 0:
                raise ValueError(f"Inbound queue {p['queue']} has no entries.")
            return {"text": f"Restart inbound queue {p['queue']}" + (f" ({n} entr{'y' if n == 1 else 'ies'})"
                                                                      if n else " (entry count not readable)"),
                    "count": n}
    raise ValueError("Unknown action.")


def candidates(system: str, action: str) -> list[dict]:
    """Things the action could be applied to, for the picker on the page."""
    chk_cfg = _cfg(system)
    if chk_cfg is None or action not in allowed(system, chk_cfg):
        return []
    with _session(system, chk_cfg) as s:
        if not s.ok:
            raise ValueError(f"{system} is not reachable over RFC: {s.error}")
        if action == "restart_job":
            today = datetime.now().strftime("%Y%m%d")
            rows = s.read_table("TBTCO", ["JOBNAME", "JOBCOUNT", "SDLUNAME", "ENDTIME"],
                                f"STATUS = 'A' AND ENDDATE = '{today}'", rows=200) or []
            return [{"job": str(r[0]).strip(), "job_count": str(r[1]).strip(), "by": str(r[2]).strip(),
                     "label": f"{str(r[0]).strip()} ({str(r[1]).strip()}) cancelled {str(r[3]).strip()}"}
                    for r in rows]
        if action == "release_lock":
            old = [x for x in _locks(s, chk_cfg) if x["age_min"] is not None and x["age_min"] >= _lock_age_min()]
            old.sort(key=lambda x: -(x["age_min"] or 0))
            return [{"user": x["user"], "object": x["object"], "argument": x["argument"],
                     "label": f"{x['user']} {x['object']} {x['argument'][:40]} -- {x['age_min']} min"}
                    for x in old[:100]]
        if action == "unlock_user":
            rows = s.read_table("USR02", ["BNAME", "UFLAG"], "UFLAG <> 0", rows=200) or []
            prot = _protected_users()
            return [{"user": str(r[0]).strip(), "label": f"{str(r[0]).strip()} (UFLAG {str(r[1]).strip()})"}
                    for r in rows if str(r[0]).strip() and str(r[0]).strip() not in prot]
        if action == "cancel_job":
            rows = s.read_table("TBTCO", ["JOBNAME", "JOBCOUNT", "STRTDATE", "STRTTIME"], "STATUS = 'R'",
                                rows=200) or []
            return [{"job": str(r[0]).strip(), "job_count": str(r[1]).strip(),
                     "label": f"{str(r[0]).strip()} ({str(r[1]).strip()}) running since {str(r[2]).strip()} "
                              f"{str(r[3]).strip()}"} for r in rows]
        if action == "restart_queue":
            rows = s.read_table("TRFCQIN", ["QNAME"], "", rows=5000) or []
            counts: dict[str, int] = {}
            for r in rows:
                q = str(r[0]).strip()
                counts[q] = counts.get(q, 0) + 1
            return [{"queue": q, "label": f"{q} ({n})"} for q, n in sorted(counts.items(), key=lambda kv: -kv[1])[:100]]
    return []


# ---------------------------------------------------------------- execution

def _do_restart_job(s, p: dict) -> str:
    got = None
    for version in ("3.0", "2.0"):
        got = s.call("BAPI_XMI_LOGON", INTERFACE="XBP", VERSION=version, **XMI)
        if got is not None and not _ret_error(got):
            break
    if got is None or _ret_error(got):
        raise RuntimeError(f"XBP logon refused: {_ret_error(got) or s.last_error} (RFC user needs S_XMI_PROD for XBP)")
    try:
        copy = s.call("BAPI_XBP_JOB_COPY", SOURCE_JOBNAME=p["job"], SOURCE_JOBCOUNT=p["job_count"],
                      TARGET_JOBNAME=p["job"], EXTERNAL_USER_NAME=EXT_USER)
        if copy is None or _ret_error(copy):
            raise RuntimeError(f"job copy failed: {_ret_error(copy) or s.last_error}")
        new_count = str(copy.get("TARGET_JOBCOUNT") or copy.get("JOBCOUNT") or "").strip()
        if not new_count:
            raise RuntimeError("job copy returned no job count")
        start = s.call("BAPI_XBP_JOB_START_IMMEDIATELY", JOBNAME=p["job"], JOBCOUNT=new_count,
                       EXTERNAL_USER_NAME=EXT_USER)
        if start is None or _ret_error(start):
            raise RuntimeError(f"copy {new_count} created but could not be started: "
                               f"{_ret_error(start) or s.last_error} -- release it in SM37")
        return f"Copy {p['job']} ({new_count}) started. Check it in SM37."
    finally:
        s.call("BAPI_XMI_LOGOFF", INTERFACE="XBP")


def _xbp_logon(s) -> None:
    got = None
    for version in ("3.0", "2.0"):
        got = s.call("BAPI_XMI_LOGON", INTERFACE="XBP", VERSION=version, **XMI)
        if got is not None and not _ret_error(got):
            return
    raise RuntimeError(f"XBP logon refused: {_ret_error(got) or s.last_error} (RFC user needs S_XMI_PROD for XBP)")


def _do_cancel_job(s, p: dict) -> str:
    _xbp_logon(s)
    try:
        res = s.call("BAPI_XBP_JOB_ABORT", JOBNAME=p["job"], JOBCOUNT=p["job_count"], EXTERNAL_USER_NAME=EXT_USER)
        if res is None or _ret_error(res):
            raise RuntimeError(f"cancel refused: {_ret_error(res) or s.last_error}")
        return f"Job {p['job']} ({p['job_count']}) cancelled. Check SM37 and the job log."
    finally:
        s.call("BAPI_XMI_LOGOFF", INTERFACE="XBP")


def _do_unlock_user(s, p: dict) -> str:
    if p["user"] in _protected_users():
        raise RuntimeError("protected user")
    res = s.call("BAPI_USER_UNLOCK", USERNAME=p["user"])
    if res is None or _ret_error(res):
        raise RuntimeError(f"unlock refused: {_ret_error(res) or s.last_error}")
    return f"User {p['user']} unlocked."


def _lock_id(x: dict) -> str:
    e = x["entry"]
    return "|".join(str(e.get(k, "")).strip() for k in ("GUNAME", "GNAME", "GARG", "GMODE", "GUSR", "GUSRVB"))


def _do_release_lock(s, cfg: dict, p: dict) -> str:
    wanted = set(p.get("_entries") or [])
    locks = [x for x in _locks(s, cfg, p["user"])
             if (not p.get("object") or x["object"] == p["object"])
             and (not p.get("argument") or x["argument"].startswith(p["argument"]))
             and x["age_min"] is not None and x["age_min"] >= _lock_age_min()
             and (not wanted or _lock_id(x) in wanted)]
    if not locks:
        return "Nothing to release -- the locks are already gone."
    pend = _pending_updates(s, p["user"])
    if pend is None or pend:
        raise RuntimeError("update requests are waiting (or could not be checked) -- not released")
    fields = _param_fields(s, "ENQUE_DELETE", "ENQ")
    rows = [{k: v for k, v in x["entry"].items() if fields is None or k in fields} for x in locks]
    kwargs = {"ENQ": rows}
    if _has_param(s, "ENQUE_DELETE", "CHECK_UPD_REQUESTS"):
        kwargs["CHECK_UPD_REQUESTS"] = 1
    if _has_param(s, "ENQUE_DELETE", "SUPPRESS_SYSLOG_ENTRY"):
        kwargs["SUPPRESS_SYSLOG_ENTRY"] = " "     # keep the SM21 entry: deletions should be visible
    res = s.call("ENQUE_DELETE", **kwargs)
    if res is None:
        raise RuntimeError(f"ENQUE_DELETE failed: {s.last_error}")
    subrc = res.get("SUBRC")
    if subrc not in (None, 0, "0"):
        raise RuntimeError(f"ENQUE_DELETE returned SUBRC {subrc} -- some entries were not deleted; check SM12")
    return f"Released {len(rows)} lock entr{'y' if len(rows) == 1 else 'ies'} of user {p['user']}."


def _do_restart_queue(s, p: dict) -> str:
    res = s.call("TRFC_QIN_RESTART", QNAME=p["queue"])
    if res is None:
        raise RuntimeError(f"TRFC_QIN_RESTART failed: {s.last_error} -- restart it in SMQ2")
    return f"Inbound queue {p['queue']} restarted. Check SMQ2 in a minute."


def _execute(action_id: int) -> None:
    a = ops_db.one("SELECT * FROM sap_actions WHERE id=?", (action_id,))
    if not a:
        return
    params = ops_db.loads(a["params"], {})
    system = a["system"]
    try:
        chk = _check(system, a["action"], params)
        cfg, p = chk["cfg"], chk["params"]
        if params.get("_entries"):
            p["_entries"] = params["_entries"]
        with _session(system, cfg) as s:
            if not s.ok:
                raise RuntimeError(f"not reachable over RFC: {s.error}")
            _require(s, a["action"])
            if a["action"] == "restart_job":
                result = _do_restart_job(s, p)
            elif a["action"] == "release_lock":
                result = _do_release_lock(s, cfg, p)
            elif a["action"] == "restart_queue":
                result = _do_restart_queue(s, p)
            elif a["action"] == "unlock_user":
                result = _do_unlock_user(s, p)
            elif a["action"] == "cancel_job":
                result = _do_cancel_job(s, p)
            else:
                raise RuntimeError(f"no handler for {a['action']}")
        ops_db.execute("UPDATE sap_actions SET status='done', finished_at=?, result=? WHERE id=?",
                       (ops_db.now(), result[:2000], action_id))
        timeline.add(system, "action", f"{CATALOG[a['action']]['label']}: done", result,
                     actor=a.get("approved_by") or a["requested_by"], severity="ok", ref=f"action:{action_id}")
        log.info(f"{system}: action {action_id} {a['action']} done: {result}")
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}" if not isinstance(exc, (RuntimeError, ValueError)) else str(exc)
        ops_db.execute("UPDATE sap_actions SET status='failed', finished_at=?, error=? WHERE id=?",
                       (ops_db.now(), msg[:2000], action_id))
        timeline.add(system, "action", f"{CATALOG.get(a['action'], {}).get('label', a['action'])}: FAILED", msg,
                     actor=a.get("approved_by") or a["requested_by"], severity="warning", ref=f"action:{action_id}")
        log.warning(f"{system}: action {action_id} {a['action']} failed: {msg}")


# ------------------------------------------------------------------ the flow

def request(system: str, action: str, params: dict, reason: str, by: str = "") -> dict:
    by = by or timeline.actor_name()
    if not str(reason or "").strip():
        raise ValueError("Give a reason -- it goes into the action log and the timeline.")
    pv = preview(system, action, params)
    p = _check(system, action, params)["params"]
    if pv.get("entries"):
        p["_entries"] = pv["entries"]
    aid = ops_db.execute(
        "INSERT INTO sap_actions(system, action, params, reason, status, requested_by, requested_at, preview) "
        "VALUES (?,?,?,?,?,?,?,?)", (system, action, ops_db.dumps(p), reason.strip()[:500], "pending", by,
                                      ops_db.now(), pv["text"][:2000]))
    timeline.add(system, "action", f"{CATALOG[action]['label']} requested", f"{pv['text']} -- {reason}",
                 actor=by, ref=f"action:{aid}")
    return get(aid)


def approve(action_id: int, by: str = "", wait: bool = False) -> dict:
    by = by or timeline.actor_name()
    a = get(action_id)
    if not a:
        raise ValueError("No such action.")
    if a["status"] != "pending":
        raise ValueError(f"This action is already {a['status']}.")
    if ops_db.parse(a["requested_at"]) < datetime.now() - timedelta(minutes=60):
        ops_db.execute("UPDATE sap_actions SET status='expired' WHERE id=?", (action_id,))
        raise ValueError("This request is older than 60 minutes and has expired. Ask again.")
    if mode_for(a["system"], a["action"]) == "two_person" and by == a["requested_by"]:
        sa = standing_approval(a["system"], a["action"])
        if sa is None:
            raise ValueError("A second person must approve this action (approval policy: two_person).")
        by = f"{by} under standing approval #{sa['id']} by {sa['created_by']}"
    n = ops_db.execute("UPDATE sap_actions SET status='running', approved_by=?, approved_at=? "
                       "WHERE id=? AND status='pending'", (by, ops_db.now(), action_id))
    if not n:
        raise ValueError("Someone else acted on this request first.")
    if wait:
        _execute(action_id)
    else:
        threading.Thread(target=_execute, args=(action_id,), daemon=True, name=f"sap-action-{action_id}").start()
    return get(action_id)


def reject(action_id: int, by: str = "", why: str = "") -> dict:
    by = by or timeline.actor_name()
    a = get(action_id)
    if not a or a["status"] != "pending":
        raise ValueError("Only a pending request can be rejected.")
    ops_db.execute("UPDATE sap_actions SET status='rejected', approved_by=?, approved_at=?, error=? WHERE id=?",
                   (by, ops_db.now(), str(why or "")[:500], action_id))
    timeline.add(a["system"], "action", f"{CATALOG[a['action']]['label']} rejected", why, actor=by,
                 ref=f"action:{action_id}")
    return get(action_id)


def get(action_id: int) -> dict | None:
    a = ops_db.one("SELECT * FROM sap_actions WHERE id=?", (int(action_id),))
    if a:
        a["params"] = ops_db.loads(a["params"], {})
        a["label"] = CATALOG.get(a["action"], {}).get("label", a["action"])
    return a


def listing(system: str = "", limit: int = 200) -> list[dict]:
    if system:
        rows = ops_db.rows("SELECT * FROM sap_actions WHERE system=? ORDER BY id DESC LIMIT ?", (system, limit))
    else:
        rows = ops_db.rows("SELECT * FROM sap_actions ORDER BY id DESC LIMIT ?", (limit,))
    for a in rows:
        a["params"] = ops_db.loads(a["params"], {})
        a["label"] = CATALOG.get(a["action"], {}).get("label", a["action"])
    return rows


def expire_old() -> int:
    cutoff = ops_db.fmt(datetime.now() - timedelta(minutes=60))
    return ops_db.execute("UPDATE sap_actions SET status='expired' WHERE status='pending' AND requested_at<?",
                          (cutoff,))
