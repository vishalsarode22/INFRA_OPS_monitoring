"""
HTTP endpoints and pages for the operations features:

    /ops               Operations page: maintenance, alerts on hold, on-call,
                       tickets, knowledge base, runbooks, actions, capacity,
                       learned baselines
    /timeline          one timeline per system
    /health            the monitoring chain
    /runbook           a runbook checklist
    /thresholds-page   edit thresholds
    /access/action     the confirm page behind the buttons in alert mails
                       (no login: the signed, single-use link is the key)

Writes need an operator/admin session or the X-IBO-Token (same rule as the
rest of the dashboard); system config and thresholds need admin. A user
with a customer scope can only act on systems in that scope.
"""
from __future__ import annotations

import re

import html
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter()


def _fin(value, default: float) -> float:
    """A finite float from a query value: NaN / inf / junk -> default (black-box test 28.10.2026)."""
    import math
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    return v if math.isfinite(v) else float(default)


# ----------------------------------------------------------------- guards

_SECRET_KEY = re.compile(r"(password|passwd|pwd|secret|token|authkey|api_key)", re.I)


def _env_refs(node, key: str = "") -> set:
    """{(is_secret_field, VAR)} for every ${VAR} in a payload / systems.yaml entry;
    is_secret_field = the value sits under a password/secret/token key."""
    out: set = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out |= _env_refs(v, str(k))
    elif isinstance(node, list):
        for v in node:
            out |= _env_refs(v, key)
    elif isinstance(node, str):
        for var in re.findall(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}", node):
            out.add((bool(_SECRET_KEY.search(key)), var))
    return out


def _env_refs_refused(payload: dict, current: dict) -> list[str]:
    """
    ${VAR}s a non-admin may not save: any VAR this system does not use yet,
    and any VAR moved from a password field into a shown field (notes, mail
    lists, hosts) -- that would print the stored password for everyone who
    sees the system.
    """
    new, cur = _env_refs(payload), _env_refs(current)
    cur_vars = {v for _, v in cur}
    cur_plain = {v for secret, v in cur if not secret}
    bad = set()
    for secret, var in new:
        if var not in cur_vars or (not secret and var not in cur_plain):
            bad.add(var)
    return sorted(bad)


def _all_system_cfgs() -> list:
    from core.config_loader import get_all_systems
    return get_all_systems()


def _need_write(x_ibo_token: str = Header(default="")):
    from dashboard.app import require_token
    return require_token(x_ibo_token)


def _need_admin():
    from dashboard.app import require_admin
    return require_admin()


def _visible_systems() -> list[str]:
    from core.config_loader import get_systems
    return [s.get("name") for s in get_systems() if s.get("name")]


def _scoped() -> bool:
    from core.auth import current_user
    u = current_user.get()
    return bool(u and u.get("customers") is not None)


def _check_system(system: str, allow_star: bool = False, allow_platform: bool = False) -> str:
    system = str(system or "").strip()
    if allow_star and system == "*":
        if _scoped():
            raise HTTPException(403, "Only a user who sees every customer can set a window for all systems.")
        return system
    if allow_platform and system == "IBOPS":
        return system
    if system not in _visible_systems():
        raise HTTPException(404, f"Unknown system: {system}")
    return system


def _bad(exc: Exception):
    raise HTTPException(400, str(exc))


def _page(name: str):
    from dashboard.app import _page as page
    return page(name)


# ------------------------------------------------------------------ pages

@router.get("/ops")
def ops_page():
    return _page("ops.html")


@router.get("/timeline")
def timeline_page():
    return _page("timeline.html")


@router.get("/health")
def health_page():
    return _page("health.html")


@router.get("/runbook")
def runbook_page():
    return _page("runbook.html")


@router.get("/thresholds-page")
def thresholds_page():
    return _page("thresholds.html")


@router.get("/errors")
def errors_page():
    return _page("errors.html")


# ------------------------------------------------------------------- errors

def _errors_visible() -> None:
    if _scoped():
        raise HTTPException(403, "The platform's error list is for Infrabeat users.")


@router.get("/api/errors")
def list_errors(resolved: bool = False, hours: float = 168):
    from core import errors
    _errors_visible()
    return {"errors": errors.listing(resolved, min(max(_fin(hours, 168), 1), 24 * 60)),
            "summary": errors.summary(24)}


@router.get("/api/errors/summary")
def errors_summary():
    from core import errors
    if _scoped():
        return {"open_groups": 0, "occurrences": 0, "hours": 24}
    return errors.summary(24)


@router.get("/api/errors/{sig}")
def get_error(sig: str):
    from core import errors
    _errors_visible()
    e = errors.get(sig)
    if not e:
        raise HTTPException(404, "No such error.")
    return e


@router.post("/api/errors/{sig}/resolve", dependencies=[Depends(_need_write)])
def resolve_error(sig: str):
    from core import errors, timeline
    _errors_visible()
    if not errors.resolve(sig, timeline.actor_name()):
        raise HTTPException(404, "No open error with that id.")
    return {"resolved": True}


_client_errors: dict = {}


@router.post("/api/errors/client")
async def client_error(request: Request):
    """
    JavaScript errors and failed calls from the dashboard pages (shell.js).
    Logged-in users only (the auth middleware); at most 20 a minute per
    client address, so a broken page cannot flood the table.
    """
    import time as _t
    from core import errors
    host = request.client.host if request.client else "?"
    now = _t.monotonic()
    hits = [t for t in _client_errors.get(host, []) if now - t < 60]
    if len(hits) >= 20:
        return {"recorded": False}
    hits.append(now)
    _client_errors[host] = hits
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    page = str(body.get("page") or "")[:200]
    msg = str(body.get("message") or "")[:1000]
    where = str(body.get("where") or "")[:200]
    stack = str(body.get("stack") or "")[:6000]
    kind = str(body.get("kind") or "BrowserError")[:60]
    err_id = errors.capture(None, "browser", context=f"page {page}", etype=kind, message=msg,
                            tb_text=stack, frames=[f"{page}:{where}"])
    return {"recorded": bool(err_id), "error_id": err_id}


# ----------------------------------------------------------- wall status

@router.get("/api/ops/status")
def ops_status():
    """Everything the wall needs per card, in one call."""
    from core import alert_control, learned_baseline, runbooks, sap_actions, tickets
    from core.config_loader import get_systems
    try:
        from core.reachability import current_failures
        fails = current_failures()
    except Exception:
        fails = {}
    try:
        open_tickets = tickets.by_system()
    except Exception:
        open_tickets = {}
    out = {}
    for cfg in get_systems():
        name = cfg.get("name")
        st = alert_control.status(name)
        reach = fails.get(name)
        rb = None
        kind = None
        if reach:
            kind = {"SYSTEM DOWN": "system_down", "VPN NOT CONNECTED": "vpn_down", "CREDENTIALS": "credentials",
                    "NOT REACHABLE": "not_reachable"}.get(reach.get("state"), "collector_down")
            book = runbooks.for_alert(kind)
            rb = {"id": book["id"], "title": book["title"], "kind": kind} if book else None
        flags = learned_baseline.flags(name)
        sb = None
        try:
            from core import standby
            if standby.peer_of(cfg):
                sb = {"peer": standby.peer_of(cfg), "active": standby.active(cfg)}
        except Exception:
            sb = None
        out[name] = {**st, "reach": reach, "alert_kind": kind, "runbook": rb, "standby": sb,
                     "tickets": open_tickets.get(name, []),
                     "unusual": flags.get("unusual") or [], "unusual_at": flags.get("at"),
                     "actions": sap_actions.allowed(name, cfg)}
    from core import oncall
    from core.action_links import public_url
    try:
        cur = oncall.current()
    except Exception:
        cur = {"emails": [], "names": []}
    return {"systems": out, "oncall": cur, "mail_buttons": bool(public_url()),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# -------------------------------------------------------------- maintenance

@router.get("/api/ops/maintenance")
def list_maintenance(days: int = 7):
    from core import alert_control
    visible = set(_visible_systems())
    rows = [w for w in alert_control.windows(min(max(days, 0), 90))
            if w["system"] in visible or (w["system"] == "*" and not _scoped())]
    return {"windows": rows}


@router.post("/api/ops/maintenance", dependencies=[Depends(_need_write)])
def create_maintenance(payload: dict):
    from core import alert_control
    system = _check_system(payload.get("system"), allow_star=True)
    start = payload.get("starts_at") or datetime.now()
    end = payload.get("ends_at")
    if not end and payload.get("minutes"):
        try:
            base = start if isinstance(start, datetime) else (datetime.strptime(str(start)[:16].replace("T", " "),
                                                                                 "%Y-%m-%d %H:%M"))
            mins = float(payload["minutes"])
            if not 1 <= mins <= 60 * 24 * 31:
                raise ValueError
            end = base + timedelta(minutes=mins)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(400, "minutes must be a number between 1 and 44640")
    try:
        return {"window": alert_control.create_window(system, start, end, payload.get("reason", ""))}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/maintenance/{wid}/cancel", dependencies=[Depends(_need_write)])
def cancel_maintenance(wid: int):
    from core import alert_control
    w = alert_control.get_window(wid)
    if not w:
        raise HTTPException(404, "No such window.")
    _check_system(w["system"], allow_star=True)
    try:
        return {"window": alert_control.cancel_window(wid)}
    except ValueError as exc:
        _bad(exc)


# ----------------------------------------------------------------- silences

@router.get("/api/ops/silences")
def list_silences():
    from core import alert_control
    visible = set(_visible_systems()) | {"IBOPS"}
    return {"silences": [s for s in alert_control.active_silences() if s["system"] in visible],
            "parked": [p for p in alert_control.all_parked() if p["system"] in visible]}


@router.post("/api/ops/silence", dependencies=[Depends(_need_write)])
def create_silence(payload: dict):
    from core import alert_control
    system = _check_system(payload.get("system"), allow_platform=True)
    try:
        return {"silence": alert_control.silence(system, payload.get("kind") or "*", payload.get("mode") or "snooze",
                                                 payload.get("minutes"), payload.get("reason", ""))}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/silence/{sid}/clear", dependencies=[Depends(_need_write)])
def clear_silence(sid: int):
    from core import alert_control, ops_db
    s = ops_db.one("SELECT * FROM silences WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404, "No such silence.")
    _check_system(s["system"], allow_platform=True)
    alert_control.clear_silence(sid)
    return {"cleared": True}


@router.post("/api/ops/park", dependencies=[Depends(_need_write)])
def park(payload: dict):
    from core import alert_control
    system = _check_system(payload.get("system"))
    alert_control.park(system, payload.get("reason") or "parked from the wall")
    return {"parked": True}


@router.post("/api/ops/unpark", dependencies=[Depends(_need_write)])
def unpark(payload: dict):
    from core import alert_control
    system = _check_system(payload.get("system"))
    return {"unparked": alert_control.unpark(system)}


# ---------------------------------------------------------------- timeline

@router.get("/api/timeline")
def get_timeline(system: str = "", days: float = 7, category: str = "", since: str = "", until: str = "",
                 limit: int = 1000):
    from core import timeline
    days = min(max(_fin(days, 7.0) or 7, 0.01), 3660)
    limit = min(max(int(limit or 1000), 1), 5000)
    cats = [c for c in category.split(",") if c] or None
    # Platform (IBOPS) events carry internal error text: not for project-scoped
    # users (black-box test 28.10.2026).
    if system == "IBOPS" and _scoped():
        raise HTTPException(404, "Not found.")
    if system:
        _check_system(system, allow_platform=True)
        rows = timeline.query(system, days, cats, since or None, until or None, limit)
    else:
        rows = timeline.query(None, days, cats, since or None, until or None, limit,
                              systems=_visible_systems() if _scoped() else None)
    return {"events": rows, "categories": list(timeline.CATEGORIES)}


# ----------------------------------------------------------------- on call

@router.get("/api/ops/oncall")
def get_oncall(weeks: int = 8):
    from core import oncall
    if _scoped():
        raise HTTPException(403, "The on-call rotation is for Infrabeat users.")
    return {"current": oncall.current(), "schedule": oncall.schedule(min(max(weeks, 1), 26)),
            "overrides": oncall.overrides(), "configured": oncall.enabled()}


@router.post("/api/ops/oncall/override", dependencies=[Depends(_need_write)])
def add_override(payload: dict):
    from core import oncall
    try:
        return {"override": oncall.add_override(payload.get("starts_at"), payload.get("ends_at"),
                                                payload.get("emails", ""), payload.get("reason", ""))}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/oncall/override/{oid}/cancel", dependencies=[Depends(_need_write)])
def cancel_override(oid: int):
    from core import oncall
    oncall.cancel_override(oid)
    return {"cancelled": True}


# ------------------------------------------------------------------ tickets

@router.get("/api/ops/tickets")
def list_tickets(status: str = ""):
    from core import tickets
    visible = set(_visible_systems())
    return {"provider": tickets.provider(),
            "tickets": [t for t in tickets.listing(status) if t["system"] in visible]}


@router.post("/api/ops/tickets/{tid}/close", dependencies=[Depends(_need_write)])
def close_ticket(tid: int, payload: dict | None = None):
    from core import ops_db, tickets
    t = ops_db.one("SELECT * FROM tickets WHERE id=?", (tid,))
    if not t:
        raise HTTPException(404, "No such ticket.")
    _check_system(t["system"])
    try:
        return {"ticket": tickets.close(tid, resolution=(payload or {}).get("resolution", ""))}
    except ValueError as exc:
        _bad(exc)


@router.post("/access/ticket/{tid}/close")
async def ticket_webhook_close(tid: int, request: Request):
    """For the ticket tool (webhook provider): closing there clears the alert here."""
    import secrets as _secrets
    from core import tickets
    expected = (os.getenv("IBO_TICKET_WEBHOOK_SECRET") or "").strip()
    got = request.headers.get("x-ibo-ticket-secret", "")
    if not expected or not _secrets.compare_digest(got.encode("utf-8", "replace"), expected.encode("utf-8")):
        raise HTTPException(401, "bad or missing X-IBO-Ticket-Secret")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        t = tickets.close(tid, by=str(body.get("closed_by") or "ticket tool")[:80],
                          resolution=str(body.get("resolution") or ""))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"closed": True, "ticket": t["id"]}


# ---------------------------------------------------------- knowledge base

@router.get("/api/ops/kb")
def list_kb(q: str = ""):
    from core import knowledge
    visible = set(_visible_systems())
    rows = knowledge.listing(q)
    if _scoped():
        rows = [r for r in rows if not r["system"] or r["system"] in visible]
    return {"entries": rows}


@router.get("/api/ops/kb/suggest")
def kb_suggest(system: str = "", kind: str = "", text: str = ""):
    from core import knowledge
    if system:
        _check_system(system, allow_platform=True)
    return {"suggestions": knowledge.suggest(system, kind, text, limit=5)}


@router.post("/api/ops/kb", dependencies=[Depends(_need_write)])
def add_kb(payload: dict):
    from core import knowledge
    system = payload.get("system") or ""
    if system:
        _check_system(system)
    try:
        return {"entry": knowledge.add(payload.get("title", ""), payload.get("fix", ""), payload.get("rule", ""),
                                       system, "manual", actions=payload.get("actions") or [],
                                       root_cause=payload.get("root_cause", ""), context=payload.get("context", ""))}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/kb/{kid}/retire", dependencies=[Depends(_need_write)])
def retire_kb(kid: int):
    from core import knowledge
    e = knowledge.get(kid)
    if not e:
        raise HTTPException(404, "No such entry.")
    if e.get("system"):
        _check_system(e["system"])
    elif _scoped():
        raise HTTPException(403, "Entries for all systems can only be retired by an unscoped user.")
    knowledge.retire(kid)
    return {"retired": True}


# ----------------------------------------------------------------- runbooks

@router.get("/api/ops/runbooks")
def list_runbooks():
    from core import runbooks
    return {"runbooks": [{k: r[k] for k in ("id", "title", "applies_to")} | {"steps": len(r["steps"])}
                         for r in runbooks.all_runbooks()]}


@router.get("/api/ops/runbook")
def get_runbook(id: str = "", kind: str = "", metric: str = "", system: str = "", day: str = ""):
    from core import runbooks
    if system:
        _check_system(system, allow_platform=True)
    rb = runbooks.get(id) if id else runbooks.for_alert(kind, metric or None)
    if rb is None:
        raise HTTPException(404, "No runbook for that alert. Add one to config/runbooks.yaml.")
    if not system:
        return {"runbook": rb, "steps": rb["steps"], "system": ""}
    return runbooks.view(rb["id"], system, day or None)


@router.post("/api/ops/runbook/tick", dependencies=[Depends(_need_write)])
def tick_runbook(payload: dict):
    from core import runbooks
    key = str(payload.get("run_key") or "")
    parts = key.split("|")
    if len(parts) != 3:
        raise HTTPException(400, "run_key must be <runbook>|<system>|<day>")
    _check_system(parts[1], allow_platform=True)
    try:
        runbooks.tick(key, int(payload.get("step", -1)), bool(payload.get("done", True)), payload.get("note", ""))
        return runbooks.view(parts[0], parts[1], parts[2])
    except (ValueError, TypeError) as exc:
        _bad(exc)


# ------------------------------------------------------------------ actions

@router.get("/api/ops/actions")
def list_actions(system: str = ""):
    from core import sap_actions
    if system:
        _check_system(system)
    visible = set(_visible_systems())
    return {"enabled": sap_actions.globally_enabled(), "two_person": sap_actions.two_person(),
            "modes": {s: {a: sap_actions.mode_for(s, a) for a in sap_actions.allowed(s)} for s in visible},
            "catalog": sap_actions.CATALOG,
            "allowed": {s: sap_actions.allowed(s) for s in visible},
            "actions": [a for a in sap_actions.listing(system) if a["system"] in visible]}


@router.get("/api/ops/actions/approvals")
def list_standing_approvals():
    from core import sap_actions
    visible = set(_visible_systems())
    return {"approvals": [a for a in sap_actions.standing_approvals()
                          if a["system"] in visible or (a["system"] == "*" and not _scoped())]}


@router.post("/api/ops/actions/approvals", dependencies=[Depends(_need_write), Depends(_need_admin)])
def add_standing_approval(payload: dict):
    from core import sap_actions
    system = _check_system(payload.get("system") or "", allow_star=True)
    try:
        return {"approval": sap_actions.add_standing_approval(system, payload.get("action") or "*",
                                                              payload.get("minutes"), payload.get("reason", ""))}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/actions/approvals/{aid}/revoke", dependencies=[Depends(_need_write), Depends(_need_admin)])
def revoke_standing_approval(aid: int):
    from core import ops_db, sap_actions
    row = ops_db.one("SELECT system FROM action_approvals WHERE id=?", (int(aid),))
    if row is None:
        raise HTTPException(404, "No such standing approval.")
    _check_system(row["system"], allow_star=True)       # scoped admins: own customers' systems only
    sap_actions.revoke_standing_approval(aid)
    return {"revoked": True}


@router.get("/api/ops/actions/candidates", dependencies=[Depends(_need_write)])
def action_candidates(system: str, action: str):
    from core import sap_actions
    _check_system(system)
    try:
        return {"candidates": sap_actions.candidates(system, action)}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/actions/preview", dependencies=[Depends(_need_write)])
def action_preview(payload: dict):
    from core import sap_actions
    system = _check_system(payload.get("system"))
    try:
        pv = sap_actions.preview(system, payload.get("action", ""), payload.get("params") or {})
        mode = sap_actions.mode_for(system, payload.get("action", ""))
        sa = sap_actions.standing_approval(system, payload.get("action", ""))
        return {"preview": pv, "mode": mode,
                "standing_approval": None if not sa else {"id": sa["id"], "until": sa["ends_at"],
                                                          "by": sa["created_by"]}}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/actions/request", dependencies=[Depends(_need_write)])
def action_request(payload: dict):
    from core import sap_actions
    system = _check_system(payload.get("system"))
    try:
        a = sap_actions.request(system, payload.get("action", ""), payload.get("params") or {},
                                payload.get("reason", ""))
        if payload.get("approve_now") and (sap_actions.mode_for(system, a["action"]) == "confirm"
                                           or sap_actions.standing_approval(system, a["action"])):
            a = sap_actions.approve(a["id"])
        return {"action": a}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/actions/{aid}/approve", dependencies=[Depends(_need_write)])
def action_approve(aid: int):
    from core import sap_actions
    a = sap_actions.get(aid)
    if not a:
        raise HTTPException(404, "No such action.")
    _check_system(a["system"])
    try:
        return {"action": sap_actions.approve(aid)}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/actions/{aid}/reject", dependencies=[Depends(_need_write)])
def action_reject(aid: int, payload: dict | None = None):
    from core import sap_actions
    a = sap_actions.get(aid)
    if not a:
        raise HTTPException(404, "No such action.")
    _check_system(a["system"])
    try:
        return {"action": sap_actions.reject(aid, why=(payload or {}).get("why", ""))}
    except ValueError as exc:
        _bad(exc)


# ------------------------------------------------------ baselines, capacity

@router.get("/api/ops/baselines/{system}")
def baselines(system: str):
    from core import learned_baseline
    _check_system(system)
    current = {}
    try:
        from dashboard.app import _cached_live
        from core.live_metrics import metrics_from_live
        payload = _cached_live(system)
        if payload and payload.get("connected"):
            current = {m.name: m.value for m in metrics_from_live(payload) if isinstance(m.value, (int, float))}
    except Exception:
        pass
    return {"mode": learned_baseline.mode(), "metrics": learned_baseline.overview(system, current)}


@router.get("/api/ops/capacity")
def capacity(system: str = ""):
    from core import capacity as cap
    names = [_check_system(system)] if system else _visible_systems()
    out = []
    for n in names:
        try:
            out += cap.forecasts(n)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"{n}: capacity forecast failed: {exc}")
    return {"warn_days": cap.warn_days(), "forecasts": out}


# ------------------------------------------------------------------- health

@router.get("/api/ops/health")
def health():
    """The chain, as seen from this machine. A customer-scoped user sees only
    the sweep rows for their systems (no tunnel, VPN or dashboard details)."""
    from core import chain_health
    rep = chain_health.report()
    if _scoped():
        visible = set(_visible_systems())
        rep["groups"] = [{**g, "rows": [r for r in g["rows"] if r.get("system") in visible]}
                         for g in rep["groups"] if g["key"] == "sweeps"]
        rows = [r for g in rep["groups"] for r in g["rows"]]
        rep["overall"] = ("bad" if any(r["level"] == "bad" for r in rows) else
                          "warn" if any(r["level"] == "warn" for r in rows) else "ok")
        rep["host"] = ""
    return rep


@router.post("/api/ops/health/test-mail", dependencies=[Depends(_need_write), Depends(_need_admin)])
def test_mail():
    from core import recipients
    from core.config_loader import get_smtp_config
    from notifications.mailer import send_html_now
    to = recipients.basis(None)
    if not to:
        raise HTTPException(400, "No Basis recipients (on-call rotation or IBO_BASIS_EMAILS).")
    try:
        smtp = get_smtp_config(None, require_recipients=False)
    except ValueError as exc:
        _bad(exc)
    ok = send_html_now(subject="[TEST] InfraBeatOps mail delivery test",
                       html=f"<p>Test mail from the Health page at {datetime.now():%Y-%m-%d %H:%M:%S}.</p>",
                       to=to, smtp_config=smtp)
    if not ok:
        raise HTTPException(502, "Sending failed -- see logs/application.log for the SMTP error.")
    return {"sent": True, "to": to}


@router.post("/api/ops/peers/{name}/forget", dependencies=[Depends(_need_write), Depends(_need_admin)])
def forget_peer(name: str):
    from core import peer_watch
    return {"forgotten": peer_watch.forget(name)}


@router.post("/api/peer/heartbeat", dependencies=[Depends(_need_write)])
def peer_heartbeat(payload: dict):
    from core import peer_watch
    detail = payload.get("detail") or {}
    if not isinstance(detail, dict):
        raise HTTPException(400, "detail must be an object.")
    peer_watch.receive(str(payload.get("name") or "peer")[:60], detail)
    return {"ok": True}


# ----------------------------------------------------- systems, thresholds

@router.post("/api/ops/systems/validate", dependencies=[Depends(_need_write), Depends(_need_admin)])
def validate_system(payload: dict):
    from core import config_editor
    errors, warnings = config_editor.validate_system(payload, bool(payload.get("editing")))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


@router.post("/api/ops/systems/save", dependencies=[Depends(_need_write), Depends(_need_admin)])
def save_system(payload: dict):
    from core import config_editor, auth
    me = auth.current_user.get() or {}
    if me.get("role") != "admin" and me.get("via") not in ("local", "token"):
        # A ${VAR} names a stored secret; pointing a visible field (mail list,
        # host) at one would show it to everyone who sees the system. Only an
        # admin may add a reference that this system does not already have.
        cur_raw = config_editor.existing_entry(str(payload.get("name") or "")) or {}
        added = _env_refs_refused(payload, cur_raw)
        if added:
            raise HTTPException(403, "Only an admin can point a setting at a stored password or token "
                                     f"({', '.join(added)}).")
    if me.get("role") not in ("admin", "manager") and me.get("via") not in ("local", "token"):
        cur = next((c for c in _all_system_cfgs() if c.get("name") == payload.get("name")), None)
        if cur is None:
            raise HTTPException(403, "Only a manager or an admin can add a system.")
        if str(payload.get("customer") or "").strip() != str(cur.get("customer") or "").strip():
            raise HTTPException(403, "Only a manager or an admin can move a system to another project.")
    try:
        res = config_editor.save_system(payload, bool(payload.get("editing")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.error(f"system save failed: {type(exc).__name__}: {exc}")
        raise HTTPException(500, "Could not save the system; the previous systems.yaml is in config/backups.")
    try:
        from collectors.rfc_collector import clear_cooldown
        clear_cooldown(payload.get("name"))
    except Exception:
        pass
    return res


@router.get("/api/ops/systems/{name}/extras", dependencies=[Depends(_need_admin)])
def system_extras(name: str):
    from core import config_editor
    _check_system(name)
    return config_editor.system_extras(name)


@router.post("/api/ops/systems/{name}/test", dependencies=[Depends(_need_write)])
def test_system(name: str):
    from core import config_editor
    _check_system(name)
    return config_editor.test_connection(name)


@router.get("/api/ops/thresholds")
def get_thresholds(system: str = ""):
    """
    thresholds   -- config/thresholds.yaml (the standard for all systems)
    defaults     -- every graded metric with the limits built into the code
                    (shown for metrics not in thresholds.yaml yet)
    systems      -- the systems this user sees
    overrides    -- the selected system's own limits (config/thresholds_systems.yaml)
    """
    from core import config_editor
    from core import thresholds as thr
    from core import alert_engine
    out = {"thresholds": config_editor.read_thresholds(), "defaults": thr.catalogue(),
           "systems": _visible_systems(), "system": "", "overrides": {}}
    if system:
        out["system"] = _check_system(system)
        out["overrides"] = thr.overrides(out["system"])
    # The value NOW next to its limits, and the last alert per value.
    try:
        out["now"] = _now_values(out["system"] or None)
    except Exception as exc:  # noqa: BLE001 -- the page must load without it
        out["now"], out["now_error"] = {}, f"{type(exc).__name__}: {exc}"
    try:
        out["last_alerts"] = alert_engine.last_sends(out["system"] or None, systems=out["systems"])
    except Exception as exc:  # noqa: BLE001
        out["last_alerts"] = {}
    admin = _is_admin() or (bool(out["system"]) and _may_settings(out["system"]))
    out["can_edit"] = admin
    if not admin:
        # receivers are personal data: shown to those who may change them
        for row in out["last_alerts"].values():
            row["recipients"] = ""
            for c in row.get("channels") or []:
                c["recipients"] = ""
    out["channel_status"] = alert_engine.channel_status()
    try:
        from core import alert_windows
        out["windows"] = alert_windows.describe(out["system"] or None)
    except Exception as exc:  # noqa: BLE001
        out["windows"] = {}
        log.warning(f"alert windows not read: {exc}")
    if out["system"] and admin:
        from core.config_loader import get_systems
        cfg = next((c for c in get_systems() if c.get("name") == out["system"]), {})
        out["recipients"] = alert_engine.recipients(cfg)
        try:
            from core import alert_teams
            out["recipients_source"] = alert_teams.basis_for(cfg)[1]
        except Exception:  # noqa: BLE001
            out["recipients_source"] = ""
    return out


def _is_admin() -> bool:
    try:
        _need_admin()
        return True
    except Exception:  # noqa: BLE001
        return False


def _may_settings(system: str) -> bool:
    """May the signed-in person change this system's settings (core/projects.may)?"""
    from core import auth, projects
    user = auth.current_user.get()
    if user is None or user.get("via") in ("local", "token"):
        return True
    try:
        return projects.may(user, "settings", system)
    except Exception:  # noqa: BLE001
        return False


_RANK = {"CRITICAL": 3, "WARNING": 2, "NORMAL": 1, "UNKNOWN": 0}


def _now_values(system: str | None) -> dict:
    """
    {metric: {"value", "status", "system", "instance", "at", "source"}}: the
    last live reading (graded with that system's limits), else the last sweep
    -- for one system, or the WORST system for "All systems".
    """
    from core.config_loader import get_systems
    from core.status_snapshot import load_snapshot
    from dashboard import app as _app
    names = [system] if system else _visible_systems()
    best: dict = {}

    def offer(key, entry):
        cur = best.get(key)
        if cur is None or _RANK.get(entry["status"], 0) > _RANK.get(cur["status"], 0):
            best[key] = entry

    for name in names:
        snap = load_snapshot(name) or {}
        sat = str(snap.get("cycle_timestamp") or "")
        for m in snap.get("metrics") or []:
            if m.get("name"):
                offer(m["name"], {"value": m.get("display_value"), "status": str(m.get("status") or "UNKNOWN"),
                                  "system": name, "instance": "", "at": sat, "source": "last sweep"})
        payload = _app._cached_live(name)
        if not payload or not payload.get("connected"):
            continue
        graded = _app._grade_live(dict(payload))
        at = str(payload.get("read_at") or "")
        live: dict = {}
        for it in graded.get("graded_items") or []:
            key = it.get("key")
            if not key or it.get("stale"):
                continue
            v = it.get("value")
            if isinstance(v, float):
                v = round(v) if abs(v) >= 10 else round(v, 1)
            unit = (it.get("unit") or "").strip()
            text = str(v).replace(" count", "") if v is not None else "—"
            if unit and v is not None and unit not in text:
                text = f"{text} {unit}"
            e = {"value": text, "status": str(it.get("status") or "UNKNOWN"), "system": name,
                 "instance": it.get("instance") or "", "at": at, "source": "live"}
            cur = live.get(key)                    # per-instance values: the worst instance
            if cur is None or _RANK.get(e["status"], 0) > _RANK.get(cur["status"], 0):
                live[key] = e
        for key, e in live.items():
            if system:
                best[key] = e                       # one system: live beats the sweep
            else:
                offer(key, e)
    # Custom alerts (every 5 minutes): the summary line of a per-user value,
    # else the worst instance.
    try:
        from core import custom_alerts
        for name in names:
            got = custom_alerts.readings(name)
            per: dict = {}
            if got.get("skipped") and not got.get("items"):
                # Not checked this round: say why on every custom row of this system.
                for key in custom_alerts.LABELS:
                    offer(key, {"value": "not checked", "status": "UNKNOWN", "system": name, "instance": "",
                                "at": got.get("at", ""), "source": "5-min check", "detail": got["skipped"]})
                continue
            for it in got.get("items") or []:
                v = it.get("value")
                unit = (it.get("unit") or "").strip()
                text = "—" if v is None else (f"{v} {unit}".strip() if unit else str(v))
                e = {"value": text, "status": str(it.get("status") or "UNKNOWN"), "system": name,
                     "instance": it.get("instance") or "", "at": got.get("at", ""), "source": "5-min check",
                     "detail": it.get("detail") or ""}
                cur = per.get(it["key"])
                if cur is None or _RANK.get(e["status"], 0) > _RANK.get(cur["status"], 0) or \
                        (_RANK.get(e["status"], 0) == _RANK.get(cur["status"], 0) and it.get("summary")):
                    per[it["key"]] = e
            for key, e in per.items():
                offer(key, e)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"custom readings for the Now column failed: {exc}")
    return best


@router.post("/api/ops/thresholds", dependencies=[Depends(_need_write), Depends(_need_admin)])
def save_threshold(payload: dict):
    """Without "system": the standard (thresholds.yaml). With "system": that system only."""
    from core import config_editor, timeline
    from core import thresholds as thr
    from core import metric_catalogue as cat
    system = str(payload.get("system") or "").strip()
    key = str(payload.get("key") or "").strip()
    known = set(thr.catalogue()) | set(config_editor.read_thresholds())
    if key and key not in known and not key.startswith("disk_"):
        _bad(ValueError(f"No reader produces a metric called {key!r}, so a limit for it would never "
                        f"be used. Pick it from the list."))
    args = dict(warning=payload.get("warning"), critical=payload.get("critical"))
    extra = dict(severity=payload.get("severity"), enabled=payload.get("enabled"),
                 alert=payload.get("alert"), channels=payload.get("channels"))
    try:
        if system:
            system = _check_system(system)
            return {"saved": thr.save_override(system, key, args["warning"], args["critical"],
                                               payload.get("inclusive"), by=timeline.actor_name(),
                                               **extra), "system": system}
        saved = config_editor.save_threshold(key, args["warning"], args["critical"],
                                             payload.get("inclusive"), **extra)
        # Two names for one figure (ST22): keep the standard of both in step.
        alias = cat.ALIASES.get(key)
        if alias:
            config_editor.save_threshold(alias, args["warning"], args["critical"],
                                         payload.get("inclusive"), **extra)
        return {"saved": saved}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/thresholds/batch", dependencies=[Depends(_need_write), Depends(_need_admin)])
def save_thresholds_batch(payload: dict):
    """
    "Save all changes" on the Thresholds page: {"system": "" | name, "items": [{key, ...fields}]}.
    Each row is saved on its own; the answer lists every row as saved or with its error,
    so one bad row never loses the others.
    """
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        _bad(ValueError("Nothing to save."))
    if len(items) > 200:
        _bad(ValueError("Too many rows in one save."))
    results = []
    for it in items:
        if not isinstance(it, dict):
            continue
        body = dict(it)
        body["system"] = payload.get("system") or ""
        try:
            got = save_threshold(body)
            results.append({"key": it.get("key"), "ok": True, "saved": got.get("saved")})
        except HTTPException as exc:
            results.append({"key": it.get("key"), "ok": False, "error": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001 -- one bad row never loses the others' results
            log.warning(f"threshold save {it.get('key')}: {type(exc).__name__}: {exc}")
            results.append({"key": it.get("key"), "ok": False,
                            "error": f"could not be written ({type(exc).__name__}); nothing changed for this row"})
    return {"system": payload.get("system") or "", "results": results,
            "saved": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}


@router.post("/api/ops/alert-windows", dependencies=[Depends(_need_write), Depends(_need_admin)])
def save_alert_window(payload: dict):
    """{"system": "" | name, "key": metric, "minutes": n | null} -- the time window of a timed alert."""
    from core import alert_windows, timeline
    system = str(payload.get("system") or "").strip()
    if system:
        system = _check_system(system)
    try:
        return alert_windows.save(system, str(payload.get("key") or ""), payload.get("minutes"),
                                  by=timeline.actor_name())
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/alert-recipients", dependencies=[Depends(_need_write), Depends(_need_admin)])
def save_alert_recipients(payload: dict):
    """WhatsApp / SMS / call numbers for one system (config/alert_recipients.yaml)."""
    from core import alert_engine, timeline
    system = _check_system(payload.get("system"))
    try:
        return {"system": system, "saved": alert_engine.save_phone_numbers(
            system, {k: payload.get(k) for k in ("whatsapp", "sms", "call")}, by=timeline.actor_name())}
    except ValueError as exc:
        _bad(exc)


@router.get("/api/ops/alert-teams", dependencies=[Depends(_need_admin)])
def get_alert_teams(system: str = ""):
    """Teams and addresses: the standard list, and one system's own (Thresholds & alerts page)."""
    from core import alert_teams
    from core.config_loader import get_systems
    system = _check_system(system) if system else ""
    data = alert_teams.load()
    out = {"system": system, "standard": data["standard"], "own": {}, "names": alert_teams.team_names(system),
           "effective": {}}
    if system:
        out["own"] = data["systems"].get(system) or {}
        cfg = next((c for c in get_systems() if c.get("name") == system), {"name": system})
        for t in out["names"]:
            addrs, notes = alert_teams.resolve(cfg, [t])
            out["effective"][t] = {"to": addrs, "note": "; ".join(notes)}
    return out


@router.post("/api/ops/alert-teams", dependencies=[Depends(_need_write), Depends(_need_admin)])
def save_alert_teams(payload: dict):
    """{"system": "" for the standard, "teams": {"Basis": "a@x, b@y", "PP": ""}}."""
    from core import alert_teams, timeline
    system = _check_system(payload.get("system")) if payload.get("system") else ""
    teams = payload.get("teams")
    if not isinstance(teams, dict):
        _bad(ValueError("teams must be a list of team names with their addresses"))
    try:
        return {"system": system, "saved": alert_teams.save(system or None, teams, by=timeline.actor_name())}
    except ValueError as exc:
        _bad(exc)


@router.get("/api/ops/job-mail-requests", dependencies=[Depends(_need_write)])
def job_mail_requests():
    """'Mail the user about cancelled jobs' requests waiting for approval (visible systems only)."""
    from core import job_user_mail
    rows = job_user_mail.waiting(_visible_systems())
    admin = _is_admin()
    for r in rows:
        r["has_email"] = bool(r.get("email"))
        if not admin:
            r["email"] = ""                   # addresses are personal data: admins only
    return {"requests": rows}


@router.get("/api/ops/owner-mail-requests", dependencies=[Depends(_need_write)])
def owner_mail_requests():
    """Mails to job owners / dump-owning teams waiting for Basis approval (visible systems only)."""
    from core import owner_mail
    rows = owner_mail.waiting(_visible_systems())
    if not _is_admin():
        for r in rows:
            r["to"] = [f"{len(r['to'])} address(es)"]          # addresses are personal data: admins only
    return {"requests": rows}


@router.post("/api/ops/owner-mail-requests/{rid}/{decision}", dependencies=[Depends(_need_write)])
def owner_mail_decide(rid: int, decision: str):
    from core import owner_mail, timeline
    if decision not in ("approve", "reject"):
        raise HTTPException(404, "Not found.")
    r = owner_mail.row(rid)
    if not r:
        raise HTTPException(404, "No such request.")
    _check_system(r["system"])
    try:
        return {"message": owner_mail.decide(int(rid), decision == "approve", by=timeline.actor_name())}
    except ValueError as exc:
        _bad(exc)


@router.post("/api/ops/job-mail-requests/{rid}/approve", dependencies=[Depends(_need_write)])
def job_mail_approve(rid: int):
    from core import job_user_mail, ops_db, timeline
    row = ops_db.one("SELECT system FROM job_user_requests WHERE id=?", (int(rid),))
    if not row:
        raise HTTPException(404, "No such request.")
    _check_system(row["system"])
    try:
        msg = job_user_mail.approve(int(rid), by=timeline.actor_name())
    except ValueError as exc:
        _bad(exc)
    if not _is_admin():
        msg = "Done. The user has been sent the list of their cancelled jobs, Basis in copy."
    return {"message": msg}


@router.post("/api/ops/alerts/test", dependencies=[Depends(_need_write), Depends(_need_admin)])
def send_test_alert(payload: dict):
    """Send a test alert on one channel to one system's receivers, now."""
    from core import alert_engine
    from core.config_loader import get_systems
    system = _check_system(payload.get("system"))
    cfg = next((c for c in get_systems() if c.get("name") == system), None)
    try:
        return alert_engine.send_test(cfg, str(payload.get("channel") or "email"))
    except ValueError as exc:
        _bad(exc)


@router.get("/api/ops/alerts")
def alert_history(system: str = "", days: int = 7):
    """Every alert sent, failed or held: time, system, value, level, channel, receivers, result."""
    from core import alert_engine
    if system:
        system = _check_system(system)
    visible = _visible_systems()
    rows = alert_engine.history(system or None, days, systems=visible)
    from utils.display_time import to_display
    from core import mail_explain
    cfgs = _system_configs()
    role_cache: dict = {}
    admin = _is_admin()
    for r in rows:
        r["at_display"] = to_display(r.get("at"))      # Indian Standard Time, labelled
        name = r.get("system") or ""
        if name not in role_cache:
            role_cache[name] = mail_explain.roles(cfgs.get(name) or {"name": name})
        r["explain"] = mail_explain.explain(r, role_cache[name])
        if not admin:
            # Roles stay visible (who was told: Manager / Basis / Client), addresses do not.
            for p in r["explain"]["people"]:
                p["address"] = ""
            r["recipients"] = ""
    return {"system": system, "days": days, "rows": rows, "systems": visible,
            "recipients_hidden": not admin}


@router.get("/api/ops/mail-routing")
def mail_routing(system: str = ""):
    """Who gets which mail for one system (or every visible system), in words."""
    from core import mail_explain
    visible = _visible_systems()
    if system:
        system = _check_system(system)
    cfgs = _system_configs()
    names = [system] if system else visible
    admin = _is_admin()
    out = []
    for n in names:
        info = mail_explain.routing(cfgs.get(n) or {"name": n})
        if not admin:
            for lst in info["lists"]:
                lst["count"] = len(lst["addresses"])
                lst["addresses"] = []
        out.append(info)
    return {"systems": out, "recipients_hidden": not admin}


@router.post("/api/ops/mail-preview")
async def mail_preview(request: Request):
    """
    Who would get which mail for a system with the values typed in its form
    (not saved yet), and where each list comes from. Body:
    {"system": "<existing name or empty>", "fields": {"basis_emails", "manager_receivers",
     "alert_receivers", "alert_receivers_cc", "name"}}.
    """
    from core import mail_explain
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = body if isinstance(body, dict) else {}
    name = str(body.get("system") or "").strip()
    cfg = {}
    if name:
        name = _check_system(name)
        cfg = _system_configs().get(name) or {}
    fields = {k: str(v)[:2000] for k, v in (body.get("fields") or {}).items()
              if k in ("basis_emails", "manager_receivers", "alert_receivers", "alert_receivers_cc", "name",
                       "recovery_emails", "down_emails", "access_emails")}
    timing = body.get("alert_timing")
    if isinstance(timing, dict):
        fields["alert_timing"] = {str(k)[:40]: str(v)[:12] for k, v in list(timing.items())[:20]}
    out = mail_explain.preview(cfg or {"name": name}, fields)
    if not _is_admin():
        for k in ("basis", "manager", "report_to", "report_cc", "recovery", "down", "access"):
            if k not in out:
                continue
            out[k]["count"] = len(out[k]["addresses"])
            out[k]["addresses"] = []
        for p in out["profiles"]:
            p["to"], p["cc"] = [], []
        out["recipients_hidden"] = True
    return out


def _system_configs() -> dict:
    try:
        from core.config_loader import get_systems
        return {s.get("name"): s for s in get_systems() if s.get("name")}
    except Exception:  # noqa: BLE001
        return {}


@router.get("/alerts-page")
def alerts_page():
    return _page("alerts.html")


@router.post("/api/ops/thresholds/reset", dependencies=[Depends(_need_write), Depends(_need_admin)])
def reset_threshold(payload: dict):
    """Drop one system's own limit for one metric: it uses the standard again."""
    from core import timeline
    from core import thresholds as thr
    system = _check_system(payload.get("system"))
    key = str(payload.get("key") or "").strip()
    if not key:
        _bad(ValueError("Which metric?"))
    return {"removed": thr.remove_override(system, key, by=timeline.actor_name()), "system": system, "key": key}


# ---------------------------------------------------- mail-link confirm page

_ACCESS_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>InfraBeatOps</title>
<style>body{font-family:Segoe UI,system-ui,Arial;background:#f1f5f9;color:#0f172a;margin:0;padding:24px}
.card{max-width:520px;margin:6vh auto;background:#fff;border-radius:12px;padding:24px;
box-shadow:0 8px 24px rgba(15,23,42,.08)}h1{font-size:18px;margin:0 0 12px}p{line-height:1.5}
label{display:block;font-size:13px;color:#475569;margin:14px 0 4px}input{width:100%;box-sizing:border-box;
padding:9px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px}button{margin-top:16px;width:100%;
padding:11px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-weight:600;font-size:15px;cursor:pointer}
.err{color:#b91c1c}.ok{color:#15803d}.muted{color:#64748b;font-size:13px}</style></head>
<body><div class="card">{{BODY}}</div></body></html>"""


def _access(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_ACCESS_HTML.replace("{{BODY}}", body), status_code=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/access/action")
def access_confirm(t: str = ""):
    from core import action_links
    try:
        body = action_links.verify(t)
        if action_links.used(body["n"]):
            raise ValueError("This link has already been used.")
    except ValueError as exc:
        return _access(f"<h1>Link not valid</h1><p class='err'>{html.escape(str(exc))}</p>", 400)
    return _access(
        f"<h1>Confirm</h1><p>{html.escape(action_links.describe(body))}</p>"
        f"<form method='post' action='/access/action'><input type='hidden' name='t' value='{html.escape(t, quote=True)}'>"
        f"<label>Your name (for the timeline, optional)</label><input name='name' maxlength='60' autocomplete='name'>"
        f"<button type='submit'>Confirm</button></form>"
        f"<p class='muted'>Opening this page did nothing yet. The link works once.</p>")


@router.post("/access/action")
async def access_do(request: Request):
    from core import action_links
    form = await request.form()
    try:
        msg = action_links.perform(str(form.get("t", "")), str(form.get("name", "")))
    except ValueError as exc:
        return _access(f"<h1>Not done</h1><p class='err'>{html.escape(str(exc))}</p>", 400)
    return _access(f"<h1>Done</h1><p class='ok'>{html.escape(msg)}</p>"
                   f"<p class='muted'>It is recorded on the system's timeline.</p>")


# ------------------------------------------------- insights, reports, ask

@router.get("/api/ops/noise")
def noise(days: float = 30):
    from core import insights
    return {"days": days, "rows": insights.noise(min(max(_fin(days, 30), 1), 180), _visible_systems())}


@router.get("/api/ops/availability")
def availability(days: float = 7):
    from core import insights
    return {"days": days, "rows": insights.availability(min(max(_fin(days, 7), 1), 180), _visible_systems())}


def _report_args(system: str, start: str, end: str, audience: str):
    from core import ops_db
    _check_system(system)
    s = ops_db.parse(start) if start else None
    e = ops_db.parse(end) if end else None
    if (start and s is None) or (end and e is None):
        raise HTTPException(400, "start / end must be YYYY-MM-DD HH:MM")
    if s and e and e <= s:
        raise HTTPException(400, "end must be after start")
    if audience not in ("internal", "customer"):
        raise HTTPException(400, "audience must be internal or customer")
    return s, e


@router.get("/report/incident")
def incident_report_html(system: str, start: str = "", end: str = "", audience: str = "internal"):
    from fastapi.responses import HTMLResponse
    from core import incident_report
    s, e = _report_args(system, start, end, audience)
    r = incident_report.build(system, s, e, audience)
    return HTMLResponse(incident_report.to_html(r), headers={"Cache-Control": "no-store"})


@router.get("/report/incident.pdf")
def incident_report_pdf(system: str, start: str = "", end: str = "", audience: str = "internal"):
    import tempfile
    from fastapi.responses import FileResponse
    from core import incident_report
    s, e = _report_args(system, start, end, audience)
    r = incident_report.build(system, s, e, audience)
    path = os.path.join(tempfile.mkdtemp(prefix="ibops-report-"),
                        f"incident-{''.join(c if c.isalnum() else '_' for c in system)}-{r['start'][:10]}.pdf")
    import shutil
    from starlette.background import BackgroundTask
    try:
        incident_report.to_pdf(r, path)
    except ImportError:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        raise HTTPException(501, "reportlab is not installed here -- use the HTML report and print it to PDF.")
    return FileResponse(path, media_type="application/pdf", filename=os.path.basename(path),
                        background=BackgroundTask(shutil.rmtree, os.path.dirname(path), True))


@router.post("/api/ops/ask")
def ask(payload: dict):
    from core import ask as _ask
    try:
        return _ask.answer(str(payload.get("question") or ""), _visible_systems(), platform=not _scoped())
    except ValueError as exc:
        _bad(exc)


@router.get("/api/ops/digest/preview")
def digest_preview():
    from fastapi.responses import HTMLResponse
    from core import digest
    if _scoped():
        raise HTTPException(403, "The digest covers every customer.")
    return HTMLResponse(digest.to_html(digest.build(7)))


@router.post("/api/ops/digest/send", dependencies=[Depends(_need_write), Depends(_need_admin)])
def digest_send():
    from core import digest
    if not digest.send(force=True):
        raise HTTPException(502, "Not sent -- no recipients, or the mail failed (see the Errors page).")
    return {"sent": True}


@router.get("/api/ops/changes/{system}")
def changes(system: str, hours: float = 168):
    from core import change_tracking
    _check_system(system)
    snap = change_tracking.load(system)
    return {"snapshot_at": snap.get("at"), "kernel": snap.get("kernel"), "components": snap.get("components"),
            "unavailable": snap.get("unavailable"), "recent": change_tracking.recent(system, min(hours, 24 * 90))}


@router.post("/api/ops/changes/{system}/snapshot", dependencies=[Depends(_need_write)])
def changes_snapshot(system: str):
    from concurrent.futures import TimeoutError as _FuturesTimeout
    from collectors.checks_job import change_snapshot_job
    from core import rfc_proc
    from core.config_loader import get_all_systems
    _check_system(system)
    cfg = next((c for c in get_all_systems() if c.get("name") == system), None)
    try:
        # In the RFC worker process: an RFC call here would freeze the web server.
        found = rfc_proc.call(change_snapshot_job, system, cfg or {}, timeout=300)
    except RuntimeError as exc:
        raise HTTPException(502, f"{system}: {exc}")
    except (TimeoutError, _FuturesTimeout):
        raise HTTPException(504, f"{system}: the snapshot is taking longer than 5 minutes -- see the Timeline later")
    return {"changes": [{"title": t, "detail": d} for t, d in found]}


@router.get("/api/ops/escalation")
def escalation_status():
    from core import escalation_time
    from notifications import sms
    cfg = escalation_time.config()
    return {"configured": bool(cfg) and cfg.get("enabled") is not False,
            "steps": cfg.get("steps") or escalation_time.DEFAULT_STEPS,
            "states": cfg.get("states") or ["SYSTEM DOWN"], "sms": sms.sms_configured(),
            "voice": sms.voice_configured()}


@router.post("/api/ops/escalation/test-sms", dependencies=[Depends(_need_write), Depends(_need_admin)])
def escalation_test_sms(payload: dict):
    from notifications import sms
    phone = str(payload.get("phone") or "")
    if not sms.normalise(phone):
        raise HTTPException(400, "Give a mobile number, e.g. +91 98xxxxxxxx")
    ok, detail = sms.send_sms([phone], {"system": "TEST", "state": "TEST MESSAGE", "minutes": 0, "message": "test"})
    if not ok:
        raise HTTPException(502, f"SMS not sent: {detail}")
    return {"sent": True, "detail": detail}


@router.get("/api/ops/checks/{system}")
def checks_for(system: str):
    """The extra checks' last stored result for a system (shared by all processes)."""
    from collectors import extra_checks
    _check_system(system)
    metrics, age = extra_checks.stored(system)
    return {"age_seconds": None if age is None else round(age), "checks": extra_checks.to_rows(metrics)}


# ------------------------------------------------------ config as code

@router.get("/api/ops/config/export", dependencies=[Depends(_need_admin)])
def config_export():
    from fastapi.responses import Response
    from core import config_bundle
    data, summary = config_bundle.export_zip()
    from datetime import datetime as _dt
    return Response(content=data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="ibops-config-{_dt.now():%Y%m%d-%H%M}.zip"',
        "X-IBO-Secrets-Removed": str(summary["secrets_removed"])})


@router.post("/api/ops/config/import", dependencies=[Depends(_need_write), Depends(_need_admin)])
async def config_import(request: Request, apply: bool = False):
    from core import config_bundle
    form = await request.form()
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        raise HTTPException(400, "Choose the zip file to import.")
    data = await up.read()
    if not apply:
        res = config_bundle.check_import(data)
        for f in res["files"]:
            f.pop("text", None)
        return res
    try:
        return config_bundle.apply_import(data)
    except ValueError as exc:
        _bad(exc)


# ------------------------------------------------------------ settings page
# Features on/off, the global alert mail lists, and Connect / Disconnect for
# the client VPN (core/live_settings.py, core/vpn_control.py). Reading needs
# a login without a customer scope; every change needs admin.

@router.get("/settings")
def settings_page():
    return _page("settings.html")


def _remote_vpns() -> list[dict]:
    """VPNs this dashboard reaches through another machine (check: remote:<source>/<vpn>)."""
    from core.config_loader import get_all_systems
    from core.reachability import _remote_check
    pairs: dict[tuple[str, str], list[str]] = {}
    for cfg in get_all_systems():
        rc = _remote_check(cfg)
        if rc and rc[0] and rc[1]:
            pairs.setdefault(rc, []).append(cfg.get("name"))
    out = []
    if not pairs:
        return out
    from dashboard.remote_sources import load_sources, remote_vpn_status
    sources = {s["name"]: s for s in load_sources()}
    for (source, vpn), systems in sorted(pairs.items()):
        st = remote_vpn_status(source, vpn, max_age=0) or {}
        src = sources.get(source) or {}
        out.append({"name": vpn, "source": source, "where": f"{src.get('label') or source} (remote)",
                    "state": st.get("state") or ("unreachable" if src else "unknown-collector"),
                    "reason": st.get("reason") or st.get("detail") or
                    ("" if st else f"could not ask {source}"),
                    "since": st.get("since"), "next_attempt": st.get("next_attempt"),
                    "systems": [s for s in systems if s], "manual_off": None,
                    "controllable": bool(src.get("token"))})
    return out


@router.get("/api/settings")
def settings_read():
    from core import live_settings, vpn_control, auth
    if _scoped():
        raise HTTPException(403, "Settings cover every customer.")
    data = live_settings.read()
    vpns = []
    try:
        vpns += vpn_control.status_all()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"settings: local VPN status failed: {exc}")
    try:
        vpns += _remote_vpns()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"settings: remote VPN status failed: {exc}")
    data["vpns"] = vpns
    data["can_edit"] = auth.is_admin() or not auth.enabled()
    try:
        from core import secrets_store
        data["secrets"] = secrets_store.status() if data["can_edit"] else []   # names + set/where only
    except Exception as exc:  # noqa: BLE001
        log.warning(f"settings: secret status failed: {exc}")
        data["secrets"] = []
    return data


@router.post("/api/settings/secret", dependencies=[Depends(_need_write), Depends(_need_admin)])
def settings_secret(payload: dict):
    """Set one password / token. Write-only: the value is never returned or logged."""
    from core import secrets_store, timeline
    if _scoped():
        raise HTTPException(403, "Settings cover every customer.")
    value, again = str(payload.get("value") or ""), str(payload.get("again") or "")
    if value != again:
        raise HTTPException(400, "The two values are not the same -- nothing was changed.")
    try:
        return secrets_store.save(str(payload.get("key") or ""), value, by=timeline.actor_name())
    except ValueError as exc:
        _bad(exc)


@router.post("/api/settings", dependencies=[Depends(_need_write), Depends(_need_admin)])
def settings_save(payload: dict):
    from core import live_settings, timeline
    if _scoped():
        raise HTTPException(403, "Settings cover every customer.")
    try:
        return live_settings.save(payload.get("features") or {}, payload.get("mail_lists") or {},
                                  by=timeline.actor_name())
    except ValueError as exc:
        _bad(exc)


def _actor(by_param: str) -> str:
    from core import timeline
    me = timeline.actor_name()
    clean = "".join(ch for ch in str(by_param or "") if ch.isalnum() or ch in "@._- ")[:60].strip()
    if me in ("api-token", "system") and clean:
        return f"{clean} (via the other dashboard)"
    return me


@router.post("/api/settings/vpn/{name}/{action}", dependencies=[Depends(_need_write), Depends(_need_admin)])
def settings_vpn(name: str, action: str, payload: dict, source: str = "", by: str = ""):
    """A JSON body is required (the page always sends one), so a plain HTML form cannot trigger it."""
    from core import vpn_control
    if _scoped():
        raise HTTPException(403, "Settings cover every customer.")
    if action not in ("connect", "disconnect"):
        raise HTTPException(404, "Unknown action.")
    who = _actor(by)
    if source:
        return _remote_vpn_action(source, name, action, who)
    try:
        return vpn_control.connect(name, who) if action == "connect" else vpn_control.disconnect(name, who)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))


def _remote_vpn_action(source: str, name: str, action: str, who: str) -> dict:
    """Forward Connect / Disconnect to the dashboard that runs the VPN (the Ubuntu VM), with its token."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request
    from dashboard.remote_sources import load_sources, _vpn_cache
    src = next((s for s in load_sources() if s["name"] == source), None)
    if src is None or not src.get("token"):
        raise HTTPException(400, f"No remote source {source!r} with a token in config/remote_sources.yaml.")
    url = (f"{src['url']}/api/settings/vpn/{urllib.parse.quote(name, safe='')}/{action}?"
           + urllib.parse.urlencode({"by": who}))
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "X-IBO-Token": src["token"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    _vpn_cache.pop((source, name), None)
    label = src.get("label") or source
    try:
        # Stopping the VPN waits for openfortivpn to exit: allow a minute.
        with opener.open(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8") or "{}").get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        raise HTTPException(exc.code if 400 <= exc.code < 600 else 502, detail or f"{label} answered {exc.code}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(504, f"No answer from {label} ({type(exc).__name__}). It may still have done it -- "
                                 f"refresh this page in a minute.")
    finally:
        _vpn_cache.pop((source, name), None)
    if action == "disconnect":
        try:
            from core import reachability
            from core.config_loader import get_all_systems
            for cfg in get_all_systems():
                if reachability._remote_check(cfg) == (source, name):
                    reachability.forget(cfg.get("name"))
        except Exception:
            pass
    try:
        from core import timeline
        timeline.add("IBOPS", "config", f"VPN {name} on {label}: {action}", actor=who)
    except Exception:
        pass
    return body if isinstance(body, dict) else {"ok": True}


# ---- VPN keeper (Windows FortiClient auto-reconnect, core/vpn_keeper.py) ----

@router.get("/api/ops/vpn-keeper")
def vpn_keeper_status():
    from core import vpn_keeper
    return {"configured": vpn_keeper.configured(), "client": vpn_keeper.find_exe(),
            "vpns": vpn_keeper.status()}


@router.post("/api/ops/vpn-keeper/{name}/reset", dependencies=[Depends(_need_write)])
def vpn_keeper_reset(name: str):
    from core import vpn_keeper
    if not vpn_keeper.reset(name):
        raise HTTPException(404, f"No VPN {name} in config/vpn_keeper.yaml (or not checked yet).")
    return {"ok": True}
