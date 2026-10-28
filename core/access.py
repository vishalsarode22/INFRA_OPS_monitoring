"""
Which permission a dashboard request needs, and for which system.

dashboard/app.py's middleware asks decide() for every state-changing
request (and for the few reads that show settings). The rules themselves
are in core/projects.may().

  run        start / stop monitoring, Performance monitoring, live refresh,
             SAP actions
  alerts     acknowledge, snooze, maintenance, park, tickets, runbook ticks
  settings   limits, mail lists, alert teams, schedules, profiles, systems
  admin      platform settings that cover every customer: admins only
  manager    affects every system (pause all, run all): managers and admins
  self       anyone signed in (own password, access requests, error reports,
             Team & access and Projects pages -- those check inside)

A request naming no system (and not 'self') needs a manager or an admin.
"""
from __future__ import annotations

import json
import re
from urllib.parse import unquote

_P = re.compile

RUN = [_P(r"^/api/run-now(/|$)"), _P(r"^/api/run-stop$"), _P(r"^/api/remote-run-now/"),
       _P(r"^/api/profiles/[^/]+/run$"), _P(r"^/api/perf-rca/"), _P(r"^/api/rca/analyze/"),
       _P(r"^/api/live/refresh"), _P(r"^/api/ops/changes/[^/]+/snapshot$"),
       _P(r"^/api/ops/systems/[^/]+/test$"), _P(r"^/api/ops/actions/(request|preview)$"),
       # approving a SAP action EXECUTES it on SAP: the run right, not alerts
       # (security review 28.10.2026)
       _P(r"^/api/ops/actions/\d+/(approve|reject)$")]
ALERTS = [_P(r"^/api/ops/(park|unpark)$"), _P(r"^/api/ops/silence"), _P(r"^/api/ops/maintenance"),
          _P(r"^/api/ops/runbook/tick$"), _P(r"^/api/ops/tickets/"), _P(r"^/incidents/"), _P(r"^/api/incidents/"),
          _P(r"^/api/ops/job-mail-requests/"),
          _P(r"^/api/ops/owner-mail-requests/")]
SETTINGS = [_P(r"^/api/ops/thresholds"), _P(r"^/api/ops/alert-recipients$"), _P(r"^/api/ops/alert-teams$"), _P(r"^/api/ops/alert-windows$"),
            _P(r"^/api/profiles(/alerts)?$"), _P(r"^/api/schedules/"), _P(r"^/api/tcode-sets$"),
            _P(r"^/api/ops/systems/(save|validate)$"), _P(r"^/api/systems(/|$)"), _P(r"^/api/ops/oncall/"),
            _P(r"^/api/ops/kb"), _P(r"^/api/ops/alerts/test$"), _P(r"^/api/ops/mail-preview$")]
ADMIN = [_P(r"^/api/settings(/|$)"), _P(r"^/api/delivery-"), _P(r"^/api/scheduler-settings"),
         _P(r"^/api/ops/config/"), _P(r"^/api/ops/peers/"), _P(r"^/api/ops/digest/send"),
         _P(r"^/api/ops/escalation/"), _P(r"^/api/ops/health/test-mail"), _P(r"^/api/capabilities/"),
         _P(r"^/api/parser-health/"), _P(r"^/api/ops/actions/approvals"), _P(r"^/api/users"),
         _P(r"^/api/access/audit"), _P(r"^/api/peer/heartbeat$"),
         _P(r"^/api/errors/[^/]+/resolve$"), _P(r"^/api/ops/vpn-keeper/"),
         # clearing an RFC logon block spends one more failed SAP logon (locks the user)
         _P(r"^/api/logon-blocks/")]
MANAGER = [_P(r"^/api/scheduler/")]
SELF = [_P(r"^/api/errors/client$"), _P(r"^/api/access/"), _P(r"^/api/me/"), _P(r"^/api/team/"),
        _P(r"^/api/ops/ask$"), _P(r"^/access/"), _P(r"^/api/projects")]
# Reads that show settings someone may only see when he may change them.
SETTINGS_READS = [_P(r"^/api/ops/alert-teams$"), _P(r"^/api/ops/systems/[^/]+/extras$")]
ADMIN_READS = [_P(r"^/api/ops/config/export"), _P(r"^/api/access/audit"),
               # platform mail / WhatsApp settings (webhook URLs can hold a secret)
               _P(r"^/api/delivery-accounts$"), _P(r"^/api/delivery-settings$"),
               # TCP probes to arbitrary private hosts
               _P(r"^/api/reach-test$"),
               # the full API map is for admins (black-box test 28.10.2026)
               _P(r"^/docs"), _P(r"^/redoc"), _P(r"^/openapi\.json$")]

# WHERE each system-bound write takes its system from -- exactly the field
# the handler acts on (security review 27.10.2026: a ?system= the handler
# ignored used to unlock global writes). Not listed = no system = manager+.
#   ("path",)       path parameter
#   ("body", key)   JSON body key
#   ("row", table)  id in the path -> that table's system column
#   ("runkey",)     body run_key "<runbook>|<system>|<day>"
#   ("profile",)    profile id in the path -> the profile's system
#   ("incident",)   incident id in the path -> its system
#   ("running",)    the system being swept now
_SOURCES = [
    (_P(r"^/api/run-now/([^/]+)$"), ("path",)),
    (_P(r"^/api/remote-run-now/([^/]+)$"), ("path",)),
    (_P(r"^/api/perf-rca/([^/]+)$"), ("path",)),
    (_P(r"^/api/rca/analyze/([^/]+)$"), ("path",)),
    (_P(r"^/api/ops/changes/([^/]+)/snapshot$"), ("path",)),
    (_P(r"^/api/ops/systems/([^/]+)/test$"), ("path",)),
    (_P(r"^/api/ops/systems/([^/]+)/extras$"), ("path",)),
    (_P(r"^/api/schedules/([^/]+)$"), ("path",)),
    (_P(r"^/api/profiles/([^/]+)/run$"), ("profile",)),
    (_P(r"^/api/run-stop$"), ("running",)),
    (_P(r"^/api/ops/actions/(request|preview)$"), ("body", "system")),
    (_P(r"^/api/ops/(park|unpark)$"), ("body", "system")),
    (_P(r"^/api/ops/silence$"), ("body", "system")),
    (_P(r"^/api/ops/maintenance$"), ("body", "system")),
    (_P(r"^/api/ops/silence/(\d+)/clear$"), ("row", "silences")),
    (_P(r"^/api/ops/maintenance/(\d+)/cancel$"), ("row", "maintenance")),
    (_P(r"^/api/ops/tickets/(\d+)/close$"), ("row", "tickets")),
    (_P(r"^/api/ops/actions/(\d+)/(approve|reject)$"), ("row", "sap_actions")),
    (_P(r"^/api/ops/job-mail-requests/(\d+)/approve$"), ("row", "job_user_requests")),
    (_P(r"^/api/ops/owner-mail-requests/(\d+)/(approve|reject)$"), ("row", "owner_mail_requests")),
    (_P(r"^/api/ops/runbook/tick$"), ("runkey",)),
    (_P(r"^/api/incidents/([^/]+)/action$"), ("incident",)),
    (_P(r"^/incidents/([^/]+)/action$"), ("incident",)),
    (_P(r"^/api/ops/thresholds$"), ("body", "system")),
    (_P(r"^/api/ops/thresholds/batch$"), ("body", "system")),
    (_P(r"^/api/ops/thresholds/reset$"), ("body", "system")),
    (_P(r"^/api/ops/alert-recipients$"), ("body", "system")),
    (_P(r"^/api/ops/alert-teams$"), ("body", "system")),
    (_P(r"^/api/ops/alert-windows$"), ("body", "system")),
    (_P(r"^/api/ops/alerts/test$"), ("body", "system")),
    (_P(r"^/api/ops/kb$"), ("body", "system")),
    (_P(r"^/api/ops/mail-preview$"), ("body", "system")),
    (_P(r"^/api/tcode-sets$"), ("body", "system")),
    (_P(r"^/api/ops/systems/(save|validate)$"), ("body", "name")),
]
_SYSTEM_KEYS = ("system", "system_name", "name")
AMBIGUOUS = "This request names more than one system; nothing was done."


NOT_FOUND = "Not found."          # a system outside your projects is not shown to exist


def category(path: str, method: str) -> str:
    safe = method.upper() in ("GET", "HEAD", "OPTIONS")
    if safe:
        if any(p.match(path) for p in ADMIN_READS):
            return "admin"
        if any(p.match(path) for p in SETTINGS_READS):
            return "settings"
        return "read"
    if path.startswith("/api/systems"):
        return "manager_only"              # adding or deleting a system touches every project
    for name, pats in (("self", SELF), ("admin", ADMIN), ("manager", MANAGER), ("run", RUN),
                       ("alerts", ALERTS), ("settings", SETTINGS)):
        if any(p.match(path) for p in pats):
            return name
    return "manager"                       # unknown write: the safe side


def _body(body: bytes | None) -> dict:
    if not body:
        return {}
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


_ROW_TABLES = {"silences", "maintenance", "tickets", "sap_actions", "job_user_requests", "owner_mail_requests"}


def _row_system(table: str, rid: str) -> str | None:
    if table not in _ROW_TABLES:
        return None
    try:
        from core import ops_db
        r = ops_db.one(f"SELECT system FROM {table} WHERE id=?", (int(rid),))
        return str(r["system"]) if r and r.get("system") else None
    except Exception:  # noqa: BLE001
        return None


def _incident_system(iid: str) -> str | None:
    try:
        from core.incident_store import load_incidents
        from core.status_snapshot import list_snapshot_systems
        for name in list_snapshot_systems():
            if any(str(i.incident_id) == str(iid) for i in load_incidents(name)):
                return name
    except Exception:  # noqa: BLE001
        return None
    return None


def target_system(path: str, query: dict | None, body: bytes | None) -> str | None:
    """
    The one system this request acts on, read exactly where its handler reads
    it. None = not tied to one system (global). Raises ValueError(AMBIGUOUS)
    when the request also names a DIFFERENT system anywhere else, or names a
    system where its handler does not look.
    """
    data = _body(body)
    found = None
    for pat, src in _SOURCES:
        m = pat.match(path)
        if not m:
            continue
        kind = src[0]
        if kind == "path":
            found = unquote(m.group(1))
        elif kind == "body":
            v = data.get(src[1])
            found = v.strip() if isinstance(v, str) and v.strip() else None
        elif kind == "row":
            found = _row_system(src[1], m.group(1))
        elif kind == "runkey":
            parts = str(data.get("run_key") or "").split("|")
            found = parts[1] if len(parts) == 3 and parts[1] else None
        elif kind == "profile":
            try:
                from core.profiles import load_profiles
                pid = unquote(m.group(1))
                found = next((p["system"] for p in load_profiles() if p["id"] == pid), None)
            except Exception:  # noqa: BLE001
                found = None
        elif kind == "incident":
            found = _incident_system(unquote(m.group(1)))
        elif kind == "running":
            found = current_run_system()
        break
    mentioned = [str((query or {}).get(k)).strip() for k in _SYSTEM_KEYS if (query or {}).get(k)]
    mentioned += [data.get(k).strip() for k in _SYSTEM_KEYS
                  if isinstance(data.get(k), str) and data.get(k).strip()]
    if found is None:
        if mentioned:
            raise ValueError(AMBIGUOUS)    # named where the handler does not look
        return None
    if any(m != found for m in mentioned):
        raise ValueError(AMBIGUOUS)
    return found


def current_run_system() -> str | None:
    try:
        from dashboard.app import _run_state
        return _run_state.get("current_system")
    except Exception:  # noqa: BLE001
        return None


def decide(principal: dict, path: str, method: str, query: dict | None = None,
           body: bytes | None = None) -> tuple[bool, str]:
    """(allowed, reason). Admins may do everything."""
    from core import projects
    role = (principal or {}).get("role")
    cat = category(path, method)
    if role == "admin" or cat in ("read", "self"):
        return True, ""
    if cat == "admin":
        return False, "Only an admin can do this (it covers every customer)."
    if cat == "manager_only":
        return (role == "manager"), "Only a manager or an admin can add or delete a system."
    if cat == "manager":
        every = principal.get("customers") is None and role in projects.LEAD_ROLES
        return (role == "manager" or every), "Only a manager or an admin can do this (it affects every system)."
    try:
        system = target_system(path, query, body)
    except ValueError as exc:
        return False, str(exc)
    if system == "*":
        system = None                      # every system (a maintenance window for all)
    if system is None:
        # Global writes (pause all, on-call, alert routing ...) cover every
        # customer: only people who see every project. A manager limited to
        # some projects acts per system.
        if principal.get("customers") is None and role in ("manager",) + tuple(projects.LEAD_ROLES):
            return True, ""
        if path == "/api/profiles" and projects.may_somewhere(principal, "settings"):
            return True, ""                # the handler keeps everyone else's profiles
        if path == "/api/live/refresh" and projects.may_somewhere(principal, "run"):
            return True, ""                # reads every visible system now; results stay scoped
        return False, "This affects more than one system: a manager or an admin can do it."
    if projects.may(principal, cat, system):
        return True, ""
    if not projects.may(principal, "read", system):
        if path == "/api/run-stop":
            return False, "The sweep running now is on a system outside your customers' projects."
        return False, NOT_FOUND
    what = {"run": "start monitoring", "alerts": "handle alerts", "settings": "change settings"}[cat]
    return False, f"You may not {what} on {system}. Your team leader can allow it on Team & access."
