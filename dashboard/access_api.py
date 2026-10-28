"""
Projects & passwords page API (core/projects.py, core/vault.py).

Every handler checks the rules itself (the middleware lets /api/projects*
and /api/access/* through as 'self'); answers never carry a password except
the reveal calls, which are audited and not cached.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from utils.logger import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["access"])
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _me() -> dict:
    from core import auth
    user = auth.current_user.get()
    if user is None:
        raise HTTPException(401, "login required")
    return user


def _bad(exc: Exception):
    raise HTTPException(400, str(exc))


def _forbid(exc: Exception):
    raise HTTPException(403, str(exc))


# ------------------------------------------------------------------ projects

@router.get("/api/projects")
def list_projects():
    from core import projects, auth
    from core.config_loader import get_all_systems
    me = _me()
    role = me.get("role")
    mine = me.get("customers")
    rows = projects.list_projects()
    if role not in ("admin", "manager"):
        rows = [r for r in rows if mine is None or r["name"] in mine]
        visible = me.get("systems")
        if visible is not None:
            for r in rows:
                r["systems"] = [s for s in r["systems"] if s in visible]
    for r in rows:
        r["member"] = mine is None or r["name"] in (mine or set())
    all_systems = sorted(str(s.get("name")) for s in get_all_systems() if s.get("name")) \
        if role in ("admin", "manager") else sorted(s.get("name") for s in auth.filter_systems(get_all_systems()))
    return {"projects": rows, "systems": all_systems, "role": role,
            "can_manage_projects": role in ("admin", "manager"),
            "can_see_audit": role == "admin"}


@router.post("/api/projects")
def create_project(payload: dict):
    from core import projects
    try:
        return projects.create(_me(), str(payload.get("name") or ""), payload.get("systems") or [])
    except projects.ProjectError as exc:
        _bad(exc)


@router.post("/api/projects/{name}/systems")
def project_systems(name: str, payload: dict):
    from core import projects
    try:
        return projects.set_systems(_me(), name, payload.get("systems") or [])
    except projects.ProjectError as exc:
        _bad(exc)


@router.post("/api/projects/{name}/delete")
def delete_project(name: str):
    from core import projects
    try:
        projects.delete(_me(), name)
        return {"deleted": name}
    except projects.ProjectError as exc:
        _bad(exc)


@router.post("/api/projects/{name}/join")
def join_project(name: str):
    from core import projects
    try:
        projects.join(_me(), name)
        return {"joined": name, "note": "Sign in again to see its systems everywhere."}
    except projects.ProjectError as exc:
        _bad(exc)


@router.get("/api/projects/grants")
def grants_of(username: str):
    """What a senior consultant / consultant may do, per project the caller leads."""
    from core import projects, store
    me = _me()
    row = store.conn().execute("SELECT username, role, customers FROM users WHERE username=?",
                               (username,)).fetchone()
    if row is None or me.get("role") not in ("admin", "manager", "team_lead", "operator"):
        raise HTTPException(404, "No such user.")
    theirs = str(row["customers"] or "")
    names = projects.project_names() if theirs.strip() == "*" else [p for p in theirs.split(",") if p.strip()]
    member = projects.membership()
    out = []
    for p in names:
        if not projects.leads(me, p):
            continue
        g = projects.grant(username, p)
        g["project_systems"] = sorted(member.get(p, set()))
        out.append(g)
    if not out and me.get("role") != "admin":
        raise HTTPException(404, "No such user.")         # shares no project you lead
    return {"username": username, "role": row["role"],
            "grantable": row["role"] in projects.GRANTED_ROLES, "grants": out}


@router.post("/api/projects/grants")
def set_grant(payload: dict):
    from core import projects
    systems = payload.get("systems")
    if systems not in ("*", None) and not isinstance(systems, list):
        raise HTTPException(400, "systems must be '*' or a list")
    try:
        return projects.set_grant(_me(), str(payload.get("username") or ""), str(payload.get("project") or ""),
                                  "*" if systems in ("*", None) else systems, bool(payload.get("run")),
                                  bool(payload.get("alerts")), bool(payload.get("settings")))
    except projects.ProjectError as exc:
        _bad(exc)


# ------------------------------------------------------------------ passwords

@router.get("/api/access/passwords")
def passwords():
    from core import vault
    me = _me()
    try:
        monitoring = vault.monitoring_logins(me)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"monitoring logins not listed: {type(exc).__name__}: {exc}")
        monitoring = []
    return {"monitoring": monitoring, "vault": vault.entries(me), "role": me.get("role")}


@router.post("/api/access/passwords/monitoring")
def change_monitoring(payload: dict):
    from core import vault
    value, again = str(payload.get("value") or ""), str(payload.get("again") or "")
    if value != again:
        raise HTTPException(400, "The two values are not the same -- nothing was changed.")
    try:
        return vault.change_monitoring(_me(), str(payload.get("key") or ""), value)
    except vault.VaultError as exc:
        _forbid(exc) if "Only" in str(exc) else _bad(exc)


@router.post("/api/access/passwords/monitoring/reveal")
def reveal_monitoring(payload: dict):
    from core import vault
    try:
        value = vault.reveal_monitoring(_me(), str(payload.get("key") or ""))
    except vault.VaultError as exc:
        _forbid(exc)
    return JSONResponse({"value": value}, headers=_NO_STORE)


@router.post("/api/access/vault")
def vault_add(payload: dict):
    from core import vault
    try:
        return vault.add(_me(), str(payload.get("project") or ""), str(payload.get("system") or ""),
                         str(payload.get("sap_user") or ""), str(payload.get("client") or ""),
                         str(payload.get("password") or ""), str(payload.get("note") or ""))
    except vault.VaultError as exc:
        _bad(exc)


@router.post("/api/access/vault/{entry_id}")
def vault_change(entry_id: int, payload: dict):
    from core import vault
    try:
        vault.change(_me(), entry_id,
                     password=payload.get("password") if payload.get("password") else None,
                     sap_user=payload.get("sap_user"), client=payload.get("client"), note=payload.get("note"))
        return {"ok": True}
    except vault.VaultError as exc:
        _forbid(exc) if "Only" in str(exc) or "may not" in str(exc) else _bad(exc)


@router.post("/api/access/vault/{entry_id}/delete")
def vault_delete(entry_id: int):
    from core import vault
    try:
        vault.remove(_me(), entry_id)
        return {"ok": True}
    except vault.VaultError as exc:
        _forbid(exc)


@router.post("/api/access/vault/{entry_id}/reveal")
def vault_reveal(entry_id: int):
    from core import vault
    try:
        value = vault.reveal(_me(), entry_id)
    except vault.VaultError as exc:
        _forbid(exc)
    return JSONResponse({"value": value}, headers=_NO_STORE)


# ------------------------------------------------------------------ audit

@router.get("/api/access/audit")
def audit(limit: int = 500, actor: str = "", action: str = ""):
    from core import projects
    if _me().get("role") != "admin":
        raise HTTPException(403, "Only an admin can read the access audit.")
    return {"rows": projects.audit_rows(limit, actor, action)}


@router.get("/access")
def access_page():
    from dashboard.app import _page
    return _page("access.html")
