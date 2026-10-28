"""
Who may look at what.

Until now every GET on the dashboard was open to anyone who could reach
the port: all 16 customers' dumps, lock owners and job names. The token
only guarded writes. This adds:

USERS   (table `users`, core/store.py; e-mail, sign-up and access requests
        in core/accounts.py)
    role       admin              everything: all projects, users, system config
               manager            his projects: everything an operator does, and
                                  gives access to them (team lead and below)
               team_lead          his projects: run/stop/snooze sweeps, alerts,
               senior_consultant  incident actions (= the former "operator")
               consultant         his projects, read-only
               viewer             his projects, read-only
               client             his own project, read-only
               operator           legacy name, same rights as team_lead
    customers  '*'  or  "Metro,Sarloha"  -- the PROJECT (customer) scope; an
               empty value means no project yet (a fresh sign-up). A system in
               systems.yaml carries `customer: Metro`; a user sees only
               systems whose customer is in their scope. A system with no
               customer belongs to the pseudo-customer "Infrabeat".

SESSIONS   cookie `ibo_session_<host>`, random 256-bit token stored server
           side in the `sessions` table (an opaque token, so a session can be
           revoked; no data in the cookie to forge). Idle expiry
           IBO_SESSION_IDLE_MINUTES (120) and absolute expiry
           IBO_SESSION_MAX_HOURS (12). HttpOnly, SameSite=Strict, Secure when
           served over HTTPS (X-Forwarded-Proto honoured only with
           IBO_TRUST_PROXY=1).

CSRF       every state-changing browser request carries X-CSRF-Token =
           HMAC(signing key, session); the key is generated into
           <state dir>/session_secret.key, never .env. /api/me hands it to
           the page.

LOCKOUT    5 failed logins per username (20 per source IP) in 15 minutes
           lock further attempts for the rest of the window.

API TOKEN  IBO_API_TOKEN, for non-browser automation only:
           `Authorization: Bearer <token>` (X-IBO-Token accepted as a legacy
           alias). Never handed to a browser. Counts as admin, '*' scope.

NO USERS   (or IBO_AUTH=0): only loopback clients are served, as a local
           admin, with a "no users configured" banner; anything else gets
           403 with instructions.

SCOPE ENFORCEMENT in one place: core.config_loader.get_systems() filters
by the scope stored in the request context (set by the middleware), so
every endpoint that lists systems -- live, overview, status, reports --
is scoped without touching each one. Snapshot/report lookups by name are
checked against the same list.

BOOTSTRAP: on first start with no users, IBO_ADMIN_PASSWORD creates
`admin` with that password; without it the dashboard serves loopback only
and logs how to create a user:
    python -m dashboard.manage_users add admin --role admin
    python -m dashboard.manage_users add metro-noc --role viewer --customers Metro
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timedelta

from core import store
from utils.logger import get_logger

log = get_logger(__name__)

# Per-machine cookie name. Browsers keep cookies per HOST, not per port, so
# the Windows dashboard (127.0.0.1:8000) and the Ubuntu one seen through the
# tunnel (127.0.0.1:8001) used to overwrite each other's login. The machine
# name makes them two different cookies. IBO_COOKIE_NAME overrides.
import re as _re
import socket as _socket
COOKIE = (os.getenv("IBO_COOKIE_NAME") or
          "ibo_session_" + _re.sub(r"[^A-Za-z0-9]", "_", _socket.gethostname())[:40])
# Sessions: an IDLE timeout (slides on every request) and an ABSOLUTE cap
# from login. The old 12 h sliding expiry meant a session polled by a wall
# display never expired at all.
SESSION_IDLE_MINUTES = int(os.getenv("IBO_SESSION_IDLE_MINUTES", "120") or 120)
SESSION_MAX_HOURS = int(os.getenv("IBO_SESSION_MAX_HOURS", "12") or 12)
SESSION_HOURS = SESSION_MAX_HOURS          # cookie max-age (kept for callers)
MIN_PASSWORD_LENGTH = 10
DEFAULT_CUSTOMER = "Infrabeat"
ROLES = ("admin", "manager", "team_lead", "senior_consultant", "consultant", "viewer", "client",
         "operator")
ROLE_LABELS = {"admin": "Admin", "manager": "Manager", "team_lead": "Team leader",
               "senior_consultant": "Senior consultant", "consultant": "Consultant",
               "viewer": "Viewer", "client": "Client", "operator": "Team leader (legacy: operator)"}
# What each role may do inside its projects; route checks use the rank.
#   3 admin   2 act (operator level)   1 read only
ROLE_LEVEL = {"admin": 3, "manager": 2, "team_lead": 2, "senior_consultant": 2, "operator": 2,
              "consultant": 1, "viewer": 1, "client": 1}
# Roles a manager may hand out (inside his own projects). Admin may give any.
MANAGER_GRANTS = ("team_lead", "senior_consultant", "consultant", "viewer", "client")

# Per-request principal: {"username", "role", "customers": set|None (None = all)}
# (plain assignment, not an annotated one: tests/test_rfc_collector.py's
# import-integrity check only sees plain module-level assignments)
current_user = contextvars.ContextVar("ibo_user", default=None)


def enabled() -> bool:
    """
    IBO_AUTH=0 no longer means "open to everyone": with logins off the
    dashboard serves ONLY loopback clients, as a local admin (see
    dashboard/app.py _auth_middleware) -- the same as running with no users.
    """
    return str(os.getenv("IBO_AUTH", "1")).strip().lower() not in ("0", "false", "no", "off")


def users_configured() -> bool:
    """True when at least one enabled login exists."""
    try:
        return store.conn().execute("SELECT COUNT(*) FROM users WHERE disabled=0").fetchone()[0] > 0
    except Exception:
        return False


def login_required() -> bool:
    """Logins are enforced (otherwise: loopback-only local admin mode)."""
    return enabled() and users_configured()


def local_admin_principal() -> dict:
    return {"username": "local-admin", "role": "admin", "customers": None, "via": "local"}


# Route levels: "viewer" = read, "operator" = act, "admin" = configure.
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


def role_level(role: str | None) -> int:
    return ROLE_LEVEL.get(str(role or ""), 0)


def has_role(principal: dict | None, needed: str) -> bool:
    if not principal:
        return False
    return role_level(principal.get("role")) >= ROLE_RANK.get(needed, 99)


def grantable_roles(principal: dict | None) -> tuple:
    """Roles this principal may give to someone else."""
    role = (principal or {}).get("role")
    if role == "admin":
        return tuple(r for r in ROLES if r != "operator")
    if role == "manager":
        return MANAGER_GRANTS
    return ()


# ------------------------------------------------------- CSRF / signing key

def _secret_path() -> str:
    explicit = os.getenv("IBO_SESSION_SECRET_FILE", "").strip()
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), "session_secret.key")


_secret_cache = {}


def signing_key() -> bytes:
    """
    Server-side HMAC key, generated on first use into a file next to the
    state database (never into .env). Mode 0600 where the OS supports it.
    """
    path = _secret_path()
    if path in _secret_cache:
        return _secret_cache[path]
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
    except OSError:
        key = b""
    if len(key) < 32:
        key = secrets.token_hex(32).encode()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    _secret_cache[path] = key
    return key


def csrf_token(principal: dict | None, session_token: str | None = None) -> str:
    """Per-session CSRF token: HMAC(key, session). Stateless, never stored."""
    if principal and principal.get("via") == "local":
        basis = "local-admin"
    elif session_token:
        basis = "s:" + session_token
    else:
        return ""
    return hmac.new(signing_key(), basis.encode(), hashlib.sha256).hexdigest()


def csrf_ok(principal: dict | None, session_token: str | None, presented: str | None) -> bool:
    expected = csrf_token(principal, session_token)
    return bool(expected and presented) and hmac.compare_digest(expected, str(presented))


# ------------------------------------------------------ login rate limiting

LOCK_AFTER_USER = int(os.getenv("IBO_LOGIN_MAX_FAILURES", "5") or 5)
# Per address: high, because a whole office (NAT) or the reverse proxy shares
# one address -- at 20, five colleagues mistyping locked everyone out. It is
# there against one machine trying passwords across many accounts; the
# per-username lock (5) protects each account.
LOCK_AFTER_IP = int(os.getenv("IBO_LOGIN_MAX_FAILURES_IP", "100") or 100)
LOCK_WINDOW_S = int(os.getenv("IBO_LOGIN_LOCK_SECONDS", "900") or 900)
_failures = {}
_failures_lock = threading.Lock()


def _recent(key: str, now: float) -> list:
    hits = [t for t in _failures.get(key, []) if now - t < LOCK_WINDOW_S]
    _failures[key] = hits
    return hits


# A username is locked per (username, address): five wrong passwords from one
# machine no longer lock the admin out everywhere (security review 28.10.2026).
# A much higher per-username ceiling still stops a spread-out guessing run.
LOCK_AFTER_USER_ANY_IP = int(os.getenv("IBO_LOGIN_LOCK_USER_ANY_IP", "50") or 50)


def _ukey(username: str) -> str:
    return "u:" + (username or "").strip().lower()


def login_locked(username: str, remote: str) -> bool:
    now = time.time()
    with _failures_lock:
        return (len(_recent(_ukey(username) + "|" + (remote or ""), now)) >= LOCK_AFTER_USER
                or len(_recent(_ukey(username), now)) >= LOCK_AFTER_USER_ANY_IP
                or len(_recent("ip:" + (remote or ""), now)) >= LOCK_AFTER_IP)


def _note_failure(username: str, remote: str) -> None:
    now = time.time()
    with _failures_lock:
        for key in (_ukey(username) + "|" + (remote or ""), _ukey(username), "ip:" + (remote or "")):
            _recent(key, now).append(now)


def _clear_failures(username: str) -> None:
    with _failures_lock:
        for k in [k for k in _failures if k == _ukey(username) or k.startswith(_ukey(username) + "|")]:
            _failures.pop(k, None)


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2$200000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, rounds, salt_b64, digest_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


# -------------------------------------------------------------------- users

def _parse_customers(raw: str) -> set[str] | None:
    """'*' = every project (None); '' = no project yet (empty set)."""
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw == "*":
        return None
    return {c.strip() for c in raw.split(",") if c.strip()}


def check_password_policy(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def valid_username(username: str) -> str:
    import re
    name = (username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._@+-]{0,63}", name):
        raise ValueError("username: 1-64 characters, letters, digits, . _ @ + -")
    return name


def add_user(username: str, password: str, role: str = "viewer", customers: str = "*") -> None:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    customers = "*" if customers is None else customers
    store.conn().execute(
        "INSERT INTO users(username, pw_hash, role, customers, created_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash, role=excluded.role, "
        "customers=excluded.customers, disabled=0",
        (username.strip().lower(), hash_password(password), role, customers,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def set_password(username: str, password: str) -> None:
    """New password; every open session of that user ends (a reset after a
    suspected break-in must log the intruder out too)."""
    name = username.strip().lower()
    store.conn().execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_password(password), name))
    store.conn().execute("DELETE FROM sessions WHERE username=?", (name,))


def remove_user(username: str) -> bool:
    name = username.strip().lower()
    cur = store.conn().execute("DELETE FROM users WHERE username=?", (name,))
    store.conn().execute("DELETE FROM sessions WHERE username=?", (name,))
    return (cur.rowcount or 0) > 0


def disable_user(username: str) -> None:
    store.conn().execute("UPDATE users SET disabled=1 WHERE username=?", (username.strip().lower(),))
    store.conn().execute("DELETE FROM sessions WHERE username=?", (username.strip().lower(),))


def list_users() -> list[dict]:
    rows = store.conn().execute(
        "SELECT username, role, customers, disabled, created_at FROM users ORDER BY username").fetchall()
    return [dict(r) for r in rows]


def user_count() -> int:
    return store.conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def bootstrap_admin() -> None:
    if not enabled() or user_count():
        return
    pw = os.getenv("IBO_ADMIN_PASSWORD", "").strip()
    if pw:
        add_user("admin", pw, "admin", "*")
        log.warning("auth: created user 'admin' from IBO_ADMIN_PASSWORD. Remove that line from "
                    ".env now and change the password with scripts/ibops_user.py.")
    else:
        log.warning("auth: no users exist and IBO_ADMIN_PASSWORD is not set -- the dashboard is "
                    "locked. Until one exists only loopback (127.0.0.1) clients are served, as a local admin. Create one: python -m dashboard.manage_users add admin --role admin")


# ----------------------------------------------------------------- sessions

class LoginLocked(Exception):
    """Too many failed attempts for this username or source address."""


class EmailNotVerified(Exception):
    """Right password, but the sign-up e-mail was never confirmed."""


_DUMMY_HASH = "pbkdf2$200000$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def login(username: str, password: str, remote: str = "") -> str | None:
    """
    A session token, or None for a wrong username/password. Raises
    LoginLocked after LOCK_AFTER_USER failures for a username (or
    LOCK_AFTER_IP from one address) within LOCK_WINDOW_S -- checked BEFORE
    the password, so a locked account cannot be probed.
    """
    from core import accounts
    accounts.ensure_schema()
    username = accounts.username_for_login((username or "")[:128])   # e-mail or name: one counter
    if login_locked(username, remote):
        log.warning(f"auth: login for '{username[:64]}' from {remote} refused: locked out")
        raise LoginLocked()
    name = username
    row = store.conn().execute("SELECT pw_hash, disabled, email_verified FROM users WHERE username=?",
                               (name,)).fetchone()
    if row is None:
        verify_password(password, _DUMMY_HASH)     # same cost: do not reveal which users exist
    if row is None or row["disabled"] or not verify_password(password, row["pw_hash"]):
        _note_failure(username, remote)
        log.warning(f"auth: failed login for '{username[:64]}' from {remote}")
        return None
    _clear_failures(username)
    if not row["email_verified"]:
        raise EmailNotVerified()
    return create_session(name, remote)


def create_session(username: str, remote: str = "", how: str = "password") -> str:
    """A new session token for an existing, enabled user (password login or SSO)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    store.conn().execute(
        "INSERT INTO sessions(token, username, created_at, expires_at, remote) VALUES (?,?,?,?,?)",
        (token, username.strip().lower(), now.strftime("%Y-%m-%d %H:%M:%S"),
         (now + timedelta(minutes=SESSION_IDLE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S"), remote[:80]))
    store.conn().execute("DELETE FROM sessions WHERE expires_at < ?", (now.strftime("%Y-%m-%d %H:%M:%S"),))
    log.info(f"auth: '{username}' logged in from {remote} ({how})")
    return token


def logout(token: str) -> None:
    store.conn().execute("DELETE FROM sessions WHERE token=?", (token,))


def principal_for_token(token: str | None) -> dict | None:
    """Resolve a session cookie to a principal, sliding the expiry."""
    if not token:
        return None
    now = datetime.now()
    row = store.conn().execute(
        "SELECT s.username, s.created_at, s.expires_at, u.role, u.customers, u.disabled "
        "FROM sessions s JOIN users u ON u.username = s.username WHERE s.token=?",
        (token,)).fetchone()
    if row is None or row["disabled"]:
        return None
    fmt = "%Y-%m-%d %H:%M:%S"
    idle_expired = datetime.strptime(row["expires_at"], fmt) < now
    absolute_expired = (datetime.strptime(row["created_at"], fmt)
                        + timedelta(hours=SESSION_MAX_HOURS)) < now
    if idle_expired or absolute_expired:
        store.conn().execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    store.conn().execute("UPDATE sessions SET expires_at=? WHERE token=?",
                         ((now + timedelta(minutes=SESSION_IDLE_MINUTES)).strftime(fmt), token))
    principal = {"username": row["username"], "role": row["role"],
                 "customers": _parse_customers(row["customers"]), "via": "session"}
    try:
        from core import projects
        member = projects.membership()
        principal["systems"] = projects.visible_systems(principal, member)
        principal["systems_known"] = set().union(*member.values()) if member else set()
    except Exception as exc:  # noqa: BLE001 -- fall back to the project (customer) rule
        log.warning(f"auth: visible systems not computed: {exc}")
    return principal


def api_token_principal() -> dict:
    return {"username": "api-token", "role": "admin", "customers": None, "via": "token"}


# -------------------------------------------------------------------- scope

def system_customer(system_cfg: dict) -> str:
    return str(system_cfg.get("customer") or DEFAULT_CUSTOMER).strip()


def in_scope(system_cfg: dict) -> bool:
    """True when the current request may see this system (core/projects.visible_systems)."""
    user = current_user.get()
    if user is None:
        return True
    name = str(system_cfg.get("name") or "")
    if "systems" in user and name in (user.get("systems_known") or set()):
        return user["systems"] is None or name in user["systems"]
    if user.get("customers") is None:
        return True
    return system_customer(system_cfg) in user["customers"]


def filter_systems(systems: list[dict]) -> list[dict]:
    user = current_user.get()
    if user is None or (user.get("customers") is None and user.get("systems") is None):
        return systems
    return [s for s in systems if in_scope(s)]


def may_write() -> bool:
    """Level 2+ roles, or anyone core/access.decide allowed this request (a granted consultant)."""
    user = current_user.get()
    return user is not None and (role_level(user.get("role")) >= 2 or bool(user.get("access_ok")))


def is_admin() -> bool:
    user = current_user.get()
    return user is not None and user.get("role") == "admin"


def may_configure() -> bool:
    """Admin, or core/access.decide allowed this settings/run/alerts request for its system."""
    user = current_user.get()
    return user is not None and (user.get("role") == "admin"
                                 or user.get("access_ok") in ("settings", "manager", "manager_only"))
