"""
Projects, who may do what in them, and the access audit.

PROJECT
  A named group of SAP systems, made by a manager or an admin on the
  Projects & passwords page ("CEAT" = 4 CEAT systems, "CEAT core" = 3 of
  them, "CEAT + AG" = a mix). A system may be in several projects. The
  older `customer:` of a system in systems.yaml still counts: a system with
  customer: CEAT is in project CEAT. A system in no project at all is in the
  pseudo-project "Infrabeat" (auth.DEFAULT_CUSTOMER), so nothing disappears.

  A person's projects are users.customers ('*' = every project).

ROLES (owner's rules, 27.10.2026)
  admin              everything, every audit; any number of admins
  manager            creates projects, puts systems in them, joins any
                     project himself; everything inside his projects;
                     may reveal saved passwords
  team_lead          changes anything for the systems of his projects
                     (limits, mail lists, schedules, runs, alerts) and their
                     SAP monitoring passwords; sets what each senior
                     consultant / consultant of his projects may do
  senior_consultant  only what a team leader allowed (GRANTS): which systems
  consultant           of the project, start monitoring, handle alerts,
                     change settings. Not allowed yet = view only.
  viewer / client    read only

GRANT  (username, project) -> systems ('*' or a list), run, alerts, settings
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    name        TEXT PRIMARY KEY,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS project_systems (
    project   TEXT NOT NULL,
    system    TEXT NOT NULL,
    added_by  TEXT NOT NULL DEFAULT '',
    added_at  TEXT NOT NULL,
    PRIMARY KEY (project, system)
);
CREATE TABLE IF NOT EXISTS access_grants (
    username  TEXT NOT NULL,
    project   TEXT NOT NULL,
    systems   TEXT NOT NULL DEFAULT '*',
    run       INTEGER NOT NULL DEFAULT 0,
    alerts    INTEGER NOT NULL DEFAULT 0,
    settings  INTEGER NOT NULL DEFAULT 0,
    set_by    TEXT NOT NULL DEFAULT '',
    set_at    TEXT NOT NULL,
    PRIMARY KEY (username, project)
);
CREATE TABLE IF NOT EXISTS access_audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_access_audit_at ON access_audit(at);
"""
_ready: set = set()
_lock = threading.Lock()
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+&\-]{0,39}$")
_FMT = "%Y-%m-%d %H:%M:%S"

ACTIONS = ("read", "run", "alerts", "settings")
GRANTED_ROLES = ("senior_consultant", "consultant")
LEAD_ROLES = ("team_lead", "operator")


class ProjectError(ValueError):
    """A sentence for the person; nothing was changed."""


def _db():
    from core import store
    c = store.conn()
    if store.DB_PATH not in _ready:
        with _lock:
            if store.DB_PATH not in _ready:
                c.executescript(_DDL)
                _ready.add(store.DB_PATH)
    return c


def _now() -> str:
    return datetime.now().strftime(_FMT)


def _default() -> str:
    from core import auth
    return auth.DEFAULT_CUSTOMER


# ------------------------------------------------------------------ audit

_denied: dict = {}


def audit(actor: str, action: str, target: str = "", detail: str = "") -> None:
    """One line in the access audit (admins read it). Never raises; never a password.
    Refusals: at most 30 rows per person per 10 minutes (the audit cannot be flooded)."""
    if action == "denied":
        import time
        now = time.time()
        hits = [t for t in _denied.get(actor, []) if now - t < 600]
        if len(hits) >= 30:
            return
        _denied[actor] = hits + [now]
    try:
        _db().execute("INSERT INTO access_audit(at, actor, action, target, detail) VALUES (?,?,?,?,?)",
                      (_now(), str(actor or "?")[:80], str(action)[:60], str(target or "")[:200],
                       str(detail or "")[:600]))
    except Exception as exc:  # noqa: BLE001
        log.warning(f"access audit not written: {exc}")


def audit_rows(limit: int = 500, actor: str = "", action: str = "") -> list[dict]:
    sql, args = "SELECT * FROM access_audit WHERE 1=1", []
    if actor:
        sql += " AND actor = ?"
        args.append(actor)
    if action:
        sql += " AND action = ?"
        args.append(action)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 5000)))
    return [dict(r) for r in _db().execute(sql, args).fetchall()]


# ------------------------------------------------------------------ projects

def _all_systems() -> list[dict]:
    from core.config_loader import get_all_systems
    try:
        return [s for s in get_all_systems() if s.get("name")]
    except Exception as exc:  # noqa: BLE001
        log.warning(f"projects: systems not readable: {exc}")
        return []


def _explicit() -> dict[str, set]:
    out: dict[str, set] = {}
    c = _db()
    for r in c.execute("SELECT name FROM projects").fetchall():
        out[r["name"]] = set()
    for r in c.execute("SELECT project, system FROM project_systems").fetchall():
        out.setdefault(r["project"], set()).add(r["system"])
    return out


def membership() -> dict[str, set]:
    """{project: {system names}} -- explicit projects, customer: projects and the default."""
    explicit = _explicit()
    out = {p: set(s) for p, s in explicit.items()}
    in_any = set().union(*explicit.values()) if explicit else set()
    for cfg in _all_systems():
        name = str(cfg["name"])
        cust = str(cfg.get("customer") or "").strip()
        if cust:
            out.setdefault(cust, set()).add(name)
        elif name not in in_any:
            out.setdefault(_default(), set()).add(name)
    known = {str(c["name"]) for c in _all_systems()}
    return {p: {s for s in systems if s in known} for p, systems in out.items()}


def system_projects(system: str, member: dict | None = None) -> set:
    member = member if member is not None else membership()
    return {p for p, systems in member.items() if system in systems}


def project_names() -> list[str]:
    return sorted(membership())


def list_projects() -> list[dict]:
    member = membership()
    explicit = _explicit()
    meta = {r["name"]: dict(r) for r in _db().execute("SELECT * FROM projects").fetchall()}
    custom = {}
    for cfg in _all_systems():
        cust = str(cfg.get("customer") or "").strip()
        if cust:
            custom.setdefault(cust, set()).add(cfg["name"])
    out = []
    for p in sorted(member):
        out.append({"name": p, "systems": sorted(member[p]),
                    "editable_systems": sorted(explicit.get(p, set())),
                    "from_systems_page": sorted(custom.get(p, set())),
                    "explicit": p in explicit, "default": p == _default() and p not in explicit,
                    "created_by": (meta.get(p) or {}).get("created_by", ""),
                    "created_at": (meta.get(p) or {}).get("created_at", "")})
    return out


def _is_manager(principal) -> bool:
    return (principal or {}).get("role") in ("admin", "manager")


def _actor(principal) -> str:
    return str((principal or {}).get("username") or "?")


def _check_systems(systems) -> list[str]:
    known = {str(c["name"]) for c in _all_systems()}
    clean = sorted({str(s).strip() for s in (systems or []) if str(s).strip()})
    bad = [s for s in clean if s not in known]
    if bad:
        raise ProjectError(f"Unknown system(s): {', '.join(bad)}")
    return clean


def create(principal, name: str, systems) -> dict:
    if not _is_manager(principal):
        raise ProjectError("Only a manager or an admin can create a project.")
    name = str(name or "").strip()
    if not _NAME.match(name):
        raise ProjectError("A project name is 1-40 letters, digits, spaces or . _ - + &")
    if name.lower() in {p.lower() for p in project_names()}:
        raise ProjectError(f"A project called {name} already exists.")
    clean = _check_systems(systems)
    c = _db()
    c.execute("INSERT INTO projects(name, created_by, created_at) VALUES (?,?,?)", (name, _actor(principal), _now()))
    for s in clean:
        c.execute("INSERT OR IGNORE INTO project_systems(project, system, added_by, added_at) VALUES (?,?,?,?)",
                  (name, s, _actor(principal), _now()))
    audit(_actor(principal), "project.create", name, ", ".join(clean))
    if principal.get("role") == "manager":
        join(principal, name)                 # the creator works on it
    return next(p for p in list_projects() if p["name"] == name)


def set_systems(principal, name: str, systems) -> dict:
    if not _is_manager(principal):
        raise ProjectError("Only a manager or an admin can change which systems a project has.")
    if name not in _explicit():
        raise ProjectError(f"{name} is not a project made on this page (its systems come from the "
                           f"Systems page Project box).")
    clean = _check_systems(systems)
    c = _db()
    before = sorted(_explicit().get(name, set()))
    c.execute("DELETE FROM project_systems WHERE project=?", (name,))
    for s in clean:
        c.execute("INSERT INTO project_systems(project, system, added_by, added_at) VALUES (?,?,?,?)",
                  (name, s, _actor(principal), _now()))
    audit(_actor(principal), "project.systems", name, f"{before} -> {clean}")
    return next(p for p in list_projects() if p["name"] == name)


def delete(principal, name: str) -> None:
    if not _is_manager(principal):
        raise ProjectError("Only a manager or an admin can delete a project.")
    if name not in _explicit():
        raise ProjectError(f"{name} is not a project made on this page.")
    c = _db()
    c.execute("DELETE FROM project_systems WHERE project=?", (name,))
    c.execute("DELETE FROM projects WHERE name=?", (name,))
    c.execute("DELETE FROM access_grants WHERE project=?", (name,))
    from core import store
    for r in store.conn().execute("SELECT username, customers FROM users").fetchall():
        have = [p for p in str(r["customers"] or "").split(",") if p.strip()]
        if name in have:
            store.conn().execute("UPDATE users SET customers=? WHERE username=?",
                                 (",".join(p for p in have if p != name), r["username"]))
    audit(_actor(principal), "project.delete", name)


def join(principal, name: str, username: str | None = None) -> None:
    """A manager puts himself (or an admin anyone) on a project."""
    from core import store
    who = username or _actor(principal)
    if principal.get("role") not in ("admin", "manager") or (username and username != _actor(principal)
                                                           and principal.get("role") != "admin"):
        raise ProjectError("Only a manager can add himself to a project.")
    if name not in project_names():
        raise ProjectError(f"Unknown project: {name}")
    row = store.conn().execute("SELECT customers FROM users WHERE username=?", (who,)).fetchone()
    if row is None:
        raise ProjectError("No such user.")
    raw = str(row["customers"] or "")
    if raw.strip() == "*":
        return
    have = {p for p in raw.split(",") if p.strip()}
    if name in have:
        return
    store.conn().execute("UPDATE users SET customers=? WHERE username=?",
                         (",".join(sorted(have | {name})), who))
    audit(_actor(principal), "project.join", name, f"user {who}")


# ------------------------------------------------------------------ grants

def grant(username: str, project: str) -> dict:
    r = _db().execute("SELECT * FROM access_grants WHERE username=? AND project=?", (username, project)).fetchone()
    if r is None:
        return {"username": username, "project": project, "systems": "*", "run": False, "alerts": False,
                "settings": False, "set_by": "", "set_at": ""}
    systems = r["systems"]
    return {"username": username, "project": project,
            "systems": "*" if systems == "*" else json.loads(systems or "[]"),
            "run": bool(r["run"]), "alerts": bool(r["alerts"]), "settings": bool(r["settings"]),
            "set_by": r["set_by"], "set_at": r["set_at"]}


def leads(principal, project: str) -> bool:
    """May this principal decide what people do in `project`?"""
    role = (principal or {}).get("role")
    if role == "admin":
        return True
    if role not in ("manager",) + LEAD_ROLES:
        return False
    mine = principal.get("customers")
    return mine is None or project in mine


def set_grant(principal, username: str, project: str, systems, run: bool, alerts: bool,
              settings: bool) -> dict:
    from core import store
    if not leads(principal, project):
        raise ProjectError(f"Only a team leader, manager or admin of {project} can set this.")
    row = store.conn().execute("SELECT role, customers FROM users WHERE username=?", (username,)).fetchone()
    if row is None:
        raise ProjectError("No such user.")
    if row["role"] not in GRANTED_ROLES:
        raise ProjectError("Permissions are set for senior consultants and consultants only; other roles "
                           "come with their role.")
    theirs = str(row["customers"] or "")
    if theirs.strip() != "*" and project not in {p for p in theirs.split(",") if p.strip()}:
        raise ProjectError(f"{username} is not on {project}. Add the project first.")
    in_project = membership().get(project, set())
    if systems != "*":
        clean = sorted({str(s) for s in (systems or [])})
        bad = [s for s in clean if s not in in_project]
        if bad:
            raise ProjectError(f"Not in {project}: {', '.join(bad)}")
        stored = json.dumps(clean)
    else:
        clean, stored = "*", "*"
    _db().execute(
        "INSERT INTO access_grants(username, project, systems, run, alerts, settings, set_by, set_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(username, project) DO UPDATE SET systems=excluded.systems, "
        "run=excluded.run, alerts=excluded.alerts, settings=excluded.settings, set_by=excluded.set_by, "
        "set_at=excluded.set_at",
        (username, project, stored, int(bool(run)), int(bool(alerts)), int(bool(settings)),
         _actor(principal), _now()))
    audit(_actor(principal), "grant.set", f"{username} @ {project}",
          f"systems={clean} run={bool(run)} alerts={bool(alerts)} settings={bool(settings)}")
    store.conn().execute("DELETE FROM sessions WHERE username=?", (username,))   # takes effect now
    return grant(username, project)


# ------------------------------------------------------------------ decisions

def _member_projects(principal, member: dict) -> set:
    mine = (principal or {}).get("customers")
    return set(member) if mine is None else {p for p in mine if p in member}


def visible_systems(principal, member: dict | None = None) -> set | None:
    """System names this principal may see; None = all (admin, '*' leads)."""
    role = (principal or {}).get("role")
    if not principal or role == "admin":
        return None
    member = member if member is not None else membership()
    projects = _member_projects(principal, member)
    if principal.get("customers") is None and role not in GRANTED_ROLES:
        return None
    out: set = set()
    for p in projects:
        systems = member.get(p, set())
        if role in GRANTED_ROLES:
            g = grant(principal["username"], p)
            systems = systems if g["systems"] == "*" else systems & set(g["systems"])
        out |= systems
    return out


def may(principal, action: str, system: str | None, member: dict | None = None) -> bool:
    """
    May this principal do `action` (read | run | alerts | settings | password)
    on `system`? system None = not tied to one system: then only admins and
    managers (it may touch every system), except read.
    """
    role = (principal or {}).get("role")
    if not principal:
        return False
    if role == "admin":
        return True
    if system is None:
        return action == "read" or role == "manager" or (
            principal.get("customers") is None and role in LEAD_ROLES and action in ("run", "alerts", "settings"))
    member = member if member is not None else membership()
    projects = system_projects(system, member) & _member_projects(principal, member)
    if not projects:
        return False
    if action == "read":
        if role in GRANTED_ROLES:
            return any(_grant_covers(principal, p, system, None) for p in projects)
        return True
    if action == "password":
        return role == "manager" or role in LEAD_ROLES
    if action == "reveal":
        return role == "manager"
    if role == "manager" or role in LEAD_ROLES:
        return action in ("run", "alerts", "settings")
    if role in GRANTED_ROLES:
        return any(_grant_covers(principal, p, system, action) for p in projects)
    return False


def _grant_covers(principal, project: str, system: str, action: str | None) -> bool:
    g = grant(principal["username"], project)
    if g["systems"] != "*" and system not in g["systems"]:
        return False
    return True if action is None else bool(g.get(action))


def may_somewhere(principal, action: str) -> bool:
    """Has this principal `action` on at least one system (for actions with no system named)."""
    role = (principal or {}).get("role")
    if role in ("admin", "manager"):
        return True
    member = membership()
    return any(may(principal, action, s, member) for p in _member_projects(principal, member)
               for s in member.get(p, set()))
