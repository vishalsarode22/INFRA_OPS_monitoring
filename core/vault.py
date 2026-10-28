"""
Saved passwords, per project (owner's rules, 27.10.2026).

1. SAP MONITORING LOGINS -- the user/password IBOPS itself logs on with
   (the ${VAR}s of a system in systems.yaml, stored by core/secrets_store).
     change   a team leader (or manager / admin) of a project that contains
              EVERY system using that password
     reveal   admin, or a manager of such a project -- audited
     others   see only that it is set

2. TEAM VAULT -- SAP logins the team keeps here (system, SAP user, client,
   password, note), each in one project.
     add      anyone on the project (not viewer / client)
     change   the person who saved it, or a team leader / manager of the
              project, or an admin
     reveal   the person who saved it, admin, a manager of the project --
              audited; everyone else on the project sees system + SAP user
              only (password ••••••)
   Passwords are encrypted (Fernet); the key is in the Windows credential
   store ("infrabeatops" / "vault-key"), else in config/vault.key
   (IBO_VAULT_KEY_FILE), never in the database.

Every change and every reveal goes to the access audit (core/projects.audit).
"""
from __future__ import annotations

import os
import threading
from datetime import datetime

from core import projects
from utils.logger import get_logger

log = get_logger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS vault (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    system      TEXT NOT NULL,
    sap_user    TEXT NOT NULL,
    client      TEXT NOT NULL DEFAULT '',
    secret      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    owner       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_vault_project ON vault(project);
"""
_ready: set = set()
_lock = threading.Lock()
_key_lock = threading.Lock()
_fernet = None
NO_ADD = ("viewer", "client")


class VaultError(ValueError):
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
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _key_file() -> str:
    explicit = (os.getenv("IBO_VAULT_KEY_FILE") or "").strip()
    if explicit:
        return explicit
    from core.config_loader import CONFIG_DIR
    return os.path.join(CONFIG_DIR, "vault.key")


def _cipher():
    global _fernet
    if _fernet is not None:
        return _fernet
    from cryptography.fernet import Fernet
    with _key_lock:
        if _fernet is not None:
            return _fernet
        key = None
        kr = None
        try:
            from core.secrets_store import _keyring
            kr = _keyring()
            if kr is not None:
                key = kr.get_password("infrabeatops", "vault-key")
        except Exception:  # noqa: BLE001
            kr = None
        path = _key_file()
        if not key and os.path.isfile(path):
            key = open(path, encoding="ascii").read().strip()
        if not key:
            key = Fernet.generate_key().decode()
            stored = False
            if kr is not None:
                try:
                    kr.set_password("infrabeatops", "vault-key", key)
                    stored = True
                except Exception:  # noqa: BLE001
                    stored = False
            if not stored:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as fh:
                    fh.write(key)
            log.info("vault: new encryption key created")
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        return _fernet


def _enc(text: str) -> str:
    return _cipher().encrypt(text.encode("utf-8")).decode("ascii")


def _dec(token: str) -> str:
    return _cipher().decrypt(token.encode("ascii")).decode("utf-8")


def _me(principal) -> str:
    return str((principal or {}).get("username") or "?")


def _on(principal, project: str) -> bool:
    mine = (principal or {}).get("customers")
    return (principal or {}).get("role") == "admin" or mine is None or project in mine


def _row(entry_id: int):
    r = _db().execute("SELECT * FROM vault WHERE id=?", (int(entry_id),)).fetchone()
    if r is None:
        raise VaultError("No such entry.")
    return r


def may_reveal(principal, row) -> bool:
    role = (principal or {}).get("role")
    return role == "admin" or row["owner"] == _me(principal) or (role == "manager" and _on(principal, row["project"]))


def may_change(principal, row) -> bool:
    role = (principal or {}).get("role")
    if role == "admin" or row["owner"] == _me(principal):
        return True
    return role in ("manager",) + projects.LEAD_ROLES and _on(principal, row["project"])


def _clean(text, n: int) -> str:
    t = str(text or "").strip()
    if any(c in t for c in "\r\n\x00"):
        raise VaultError("One line only, please.")
    return t[:n]


def entries(principal) -> list[dict]:
    rows = _db().execute("SELECT * FROM vault ORDER BY project, system, sap_user").fetchall()
    out = []
    visible = (principal or {}).get("systems")
    for r in rows:
        if not _on(principal, r["project"]):
            continue
        if visible is not None and r["system"] not in visible and r["owner"] != _me(principal):
            continue
        out.append({"id": r["id"], "project": r["project"], "system": r["system"], "sap_user": r["sap_user"],
                    "client": r["client"], "note": r["note"], "owner": r["owner"],
                    "updated_at": r["updated_at"], "updated_by": r["updated_by"],
                    "can_reveal": may_reveal(principal, r), "can_change": may_change(principal, r)})
    return out


def add(principal, project: str, system: str, sap_user: str, client: str, password: str, note: str = "") -> dict:
    if (principal or {}).get("role") in NO_ADD:
        raise VaultError("Viewers and clients cannot save passwords.")
    if not _on(principal, project) or project not in projects.project_names():
        raise VaultError(f"You are not on the project {project}.")
    if system not in projects.membership().get(project, set()) or not projects.may(principal, "read", system):
        raise VaultError(f"{system} is not in {project}.")
    sap_user = _clean(sap_user, 40)
    if not sap_user or not password:
        raise VaultError("SAP user and password are needed.")
    if len(password) > 256 or any(c in password for c in "\r\n\x00"):
        raise VaultError("The password must be one line, at most 256 characters.")
    cur = _db().execute(
        "INSERT INTO vault(project, system, sap_user, client, secret, note, owner, created_at, updated_at, updated_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (project, system, sap_user, _clean(client, 3), _enc(password), _clean(note, 200), _me(principal),
         _now(), _now(), _me(principal)))
    projects.audit(_me(principal), "vault.add", f"{project} / {system} / {sap_user}")
    return {"id": cur.lastrowid}


def change(principal, entry_id: int, password: str | None = None, sap_user: str | None = None,
           client: str | None = None, note: str | None = None) -> None:
    r = _row(entry_id)
    if not _on(principal, r["project"]) or not may_change(principal, r):
        projects.audit(_me(principal), "denied", f"vault change #{entry_id}", "not allowed")
        raise VaultError("Only the person who saved it, a team leader or manager of the project, or an admin "
                         "can change it.")
    fields, args = [], []
    if password is not None:
        if not password or len(password) > 256 or any(c in password for c in "\r\n\x00"):
            raise VaultError("The password must be one line, 1-256 characters.")
        fields.append("secret=?")
        args.append(_enc(password))
    if sap_user is not None:
        fields.append("sap_user=?")
        args.append(_clean(sap_user, 40))
    if client is not None:
        fields.append("client=?")
        args.append(_clean(client, 3))
    if note is not None:
        fields.append("note=?")
        args.append(_clean(note, 200))
    if not fields:
        return
    _db().execute(f"UPDATE vault SET {', '.join(fields)}, updated_at=?, updated_by=? WHERE id=?",
                  (*args, _now(), _me(principal), int(entry_id)))
    projects.audit(_me(principal), "vault.change", f"{r['project']} / {r['system']} / {r['sap_user']}",
                   "password changed" if password is not None else "details changed")


def remove(principal, entry_id: int) -> None:
    r = _row(entry_id)
    if not _on(principal, r["project"]) or not may_change(principal, r):
        projects.audit(_me(principal), "denied", f"vault delete #{entry_id}", "not allowed")
        raise VaultError("You may not delete this entry.")
    _db().execute("DELETE FROM vault WHERE id=?", (int(entry_id),))
    projects.audit(_me(principal), "vault.delete", f"{r['project']} / {r['system']} / {r['sap_user']}")


def reveal(principal, entry_id: int) -> str:
    r = _row(entry_id)
    if not _on(principal, r["project"]) or not may_reveal(principal, r):
        projects.audit(_me(principal), "denied", f"vault reveal #{entry_id}", "not allowed")
        raise VaultError("Only the person who saved it, a manager of the project or an admin can see it.")
    projects.audit(_me(principal), "vault.reveal", f"{r['project']} / {r['system']} / {r['sap_user']}")
    return _dec(r["secret"])


# ------------------------------------------------------- monitoring logins

def monitoring_vars() -> dict[str, set]:
    """{VAR: {systems using it}} for every password-like ${VAR} in systems.yaml."""
    from core import secrets_store
    out: dict[str, set] = {}
    try:
        import yaml
        from core.config_loader import CONFIG_DIR
        with open(os.path.join(CONFIG_DIR, "systems.yaml"), "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001
        return out

    def walk(node, system, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, system, path + [str(k)])
        elif isinstance(node, list):
            for v in node:
                walk(v, system, path)
        elif isinstance(node, str):
            for var in secrets_store._REF.findall(node):
                if secrets_store._SECRET_WORD.search(var) or (path and secrets_store._SECRET_WORD.search(path[-1])):
                    out.setdefault(var, set()).add(system)
    for entry in (data.get("systems") or []) if isinstance(data, dict) else []:
        if isinstance(entry, dict) and entry.get("name"):
            walk(entry, str(entry["name"]), [])
    platform = set(secrets_store.PLATFORM) | set(secrets_store.NEVER)
    return {k: v for k, v in out.items()
            if k not in platform and not k.startswith(("IBO_", "ALERT_", "WHATSAPP", "GEMINI", "GROK", "SMTP_"))}


def monitoring_logins(principal) -> list[dict]:
    """The monitoring passwords of the systems this person sees: set or not, may change, may reveal."""
    from core import secrets_store
    status = {s["key"]: s for s in secrets_store.status()}
    labels = secrets_store.catalogue()
    member = projects.membership()
    out = []
    for var, systems in sorted(monitoring_vars().items()):
        visible = sorted(s for s in systems if projects.may(principal, "read", s, member))
        if not visible:
            continue
        out.append({"key": var, "label": labels.get(var, var), "systems": visible,
                    "shared_with_other_systems": len(visible) < len(systems),
                    "set": bool((status.get(var) or {}).get("set")),
                    "can_change": all(projects.may(principal, "password", s, member) for s in systems),
                    "can_reveal": all(projects.may(principal, "reveal", s, member) for s in systems)})
    return out


def change_monitoring(principal, var: str, password: str) -> dict:
    from core import secrets_store
    systems = monitoring_vars().get(var)
    if not systems:
        raise VaultError("Unknown password.")
    member = projects.membership()
    if not all(projects.may(principal, "password", s, member) for s in systems):
        projects.audit(_me(principal), "denied", f"monitoring change {var}", "not allowed")
        raise VaultError("Only a team leader or manager of every project using this password, or an admin, "
                         "can change it.")
    try:
        res = secrets_store.save(var, password, by=_me(principal))
    except ValueError as exc:
        raise VaultError(str(exc)) from None
    projects.audit(_me(principal), "monitoring.change", var, ", ".join(sorted(systems)))
    return res


def reveal_monitoring(principal, var: str) -> str:
    systems = monitoring_vars().get(var)
    if not systems:
        raise VaultError("Unknown password.")
    member = projects.membership()
    if not all(projects.may(principal, "reveal", s, member) for s in systems):
        projects.audit(_me(principal), "denied", f"monitoring reveal {var}", "not allowed")
        raise VaultError("Only an admin, or a manager of every project using it, can see this password.")
    from core.config_loader import env_or_keyring
    value = str(env_or_keyring(var, "") or "")
    projects.audit(_me(principal), "monitoring.reveal", var, ", ".join(sorted(systems)))
    return value
