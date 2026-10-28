"""
Accounts: e-mail sign-in, sign-up, password reset, invitations, and
project access requests.

ROLES (core/auth.py)
    admin              every project, users, system configuration
    manager            his projects: everything a team leader does, and gives
                       access to them (team leader and below)
    team_lead          his projects: act (run sweeps, alerts, incidents)
    senior_consultant  same rights as team_lead
    consultant         his projects, read-only
    viewer             his projects, read-only
    client             his own project, read-only (created by admin/manager)

A PROJECT is the `customer:` of a system in systems.yaml (CEAT, CARFOUR, ...);
a system without one belongs to "Infrabeat". users.customers holds a user's
projects: '*' = all, '' = none yet.

SIGN-UP     Only addresses in IBO_SIGNUP_DOMAINS (default infrabeat.com) may
            create their own account. It starts as IBO_SIGNUP_ROLE (default
            consultant) with NO project, and cannot sign in until the link in
            the confirmation mail is opened. Then the person asks for access
            to projects; the managers of each project (and the admins) get a
            mail and approve or reject on the "Team & access" page.
INVITE      An admin or manager creates the account (any address -- this is
            how clients get one); the person gets a "set your password" link.
RESET       "Forgot password" mails a one-time link (IBO_RESET_MINUTES, 30).
            Opening it also confirms the address. Every answer is the same
            whether or not the address exists, so the form cannot be used to
            find out who has an account.
TOKENS      random, sent once by mail; only their SHA-256 is stored; one use.
"""
from __future__ import annotations

import hashlib
import html as _html
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

from core import auth, store
from utils.logger import get_logger

log = get_logger(__name__)

_FMT = "%Y-%m-%d %H:%M:%S"
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _now() -> datetime:
    return datetime.now()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def signup_domains() -> list[str]:
    raw = os.getenv("IBO_SIGNUP_DOMAINS", "infrabeat.com")
    return [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]


def signup_role() -> str:
    r = (os.getenv("IBO_SIGNUP_ROLE", "consultant") or "consultant").strip().lower()
    return r if r in auth.ROLES and r not in ("admin", "manager") else "consultant"


class AccountError(ValueError):
    """A message safe to show to the person."""


# ------------------------------------------------------------------- schema

_DDL = """
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash  TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    purpose     TEXT NOT NULL,            -- verify | reset
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS access_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL,
    project     TEXT NOT NULL,
    role_wanted TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | withdrawn
    created_at  TEXT NOT NULL,
    decided_by  TEXT NOT NULL DEFAULT '',
    decided_at  TEXT NOT NULL DEFAULT '',
    decided_role TEXT NOT NULL DEFAULT '',
    decision_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS access_requests_status ON access_requests(status, project);
"""
_USER_COLUMNS = (
    ("email", "TEXT NOT NULL DEFAULT ''"),
    ("email_verified", "INTEGER NOT NULL DEFAULT 1"),   # existing logins count as verified
    ("full_name", "TEXT NOT NULL DEFAULT ''"),
    ("created_by", "TEXT NOT NULL DEFAULT ''"),
)
_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema() -> None:
    """Add the e-mail columns and the two tables once per connection."""
    c = store.conn()
    key = (store.DB_PATH, id(c))
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        have = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        for name, decl in _USER_COLUMNS:
            if name not in have:
                try:
                    c.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
                except Exception as exc:  # noqa: BLE001 -- another thread added it
                    if "duplicate column" not in str(exc).lower():
                        raise
        c.executescript(_DDL)
        _ready.add(key)


# ------------------------------------------------------------------ helpers

def normalise_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 120:
        raise AccountError("Enter a valid e-mail address.")
    return email


def email_domain_allowed(email: str) -> bool:
    return email.rsplit("@", 1)[-1] in signup_domains()


def username_for_login(text: str) -> str:
    """The account a sign-in name refers to: a username, or an account's e-mail."""
    name = (text or "").strip().lower()
    ensure_schema()
    c = store.conn()
    if c.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
        return name
    row = c.execute("SELECT username FROM users WHERE email=? AND email<>''", (name,)).fetchone()
    return row["username"] if row else name


def _user(username: str):
    ensure_schema()
    return store.conn().execute("SELECT * FROM users WHERE username=?",
                                ((username or "").strip().lower(),)).fetchone()


def _user_by_email(email: str):
    ensure_schema()
    return store.conn().execute(
        "SELECT * FROM users WHERE email=? OR username=?", (email, email)).fetchone()


def _mail_of(row) -> str:
    if row is None:
        return ""
    if row["email"]:
        return row["email"]
    return row["username"] if _EMAIL_RE.match(row["username"] or "") else ""


def projects_of(row) -> set | None:
    """None = every project."""
    return auth._parse_customers(row["customers"])


def _join(projects) -> str:
    return ",".join(sorted({p for p in projects if p}))


def all_projects() -> list[str]:
    """Every project: made on the Projects page, from a system's Project box, or Infrabeat."""
    try:
        from core import projects
        return projects.project_names()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"accounts: could not list projects: {exc}")
        return []


def public_base() -> str:
    from core.action_links import public_url
    return public_url() or "http://127.0.0.1:8000"


# ------------------------------------------------------------------- tokens

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(username: str, purpose: str, minutes: int) -> str:
    ensure_schema()
    raw = secrets.token_urlsafe(32)
    now = _now()
    c = store.conn()
    # one live link per purpose: a new one replaces the old
    c.execute("UPDATE auth_tokens SET used=1 WHERE username=? AND purpose=? AND used=0", (username, purpose))
    c.execute("INSERT INTO auth_tokens(token_hash, username, purpose, created_at, expires_at) VALUES (?,?,?,?,?)",
              (_hash(raw), username, purpose, now.strftime(_FMT), (now + timedelta(minutes=minutes)).strftime(_FMT)))
    c.execute("DELETE FROM auth_tokens WHERE expires_at < ?", ((now - timedelta(days=7)).strftime(_FMT),))
    return raw


def peek_token(raw: str, purpose: str) -> str | None:
    """The username a live token belongs to, without using it up."""
    ensure_schema()
    row = store.conn().execute(
        "SELECT username, expires_at, used FROM auth_tokens WHERE token_hash=? AND purpose=?",
        (_hash(raw or ""), purpose)).fetchone()
    if row is None or row["used"] or row["expires_at"] < _now().strftime(_FMT):
        return None
    return row["username"]


def use_token(raw: str, purpose: str) -> str | None:
    username = peek_token(raw, purpose)
    if username is None:
        return None
    cur = store.conn().execute("UPDATE auth_tokens SET used=1 WHERE token_hash=? AND used=0", (_hash(raw),))
    return username if (cur.rowcount or 0) == 1 else None


# ------------------------------------------------------------- rate limits

_hits: dict = {}
_hits_lock = threading.Lock()


def _allow(key: str, limit: int, window_s: int = 3600) -> bool:
    now = time.time()
    with _hits_lock:
        hits = [t for t in _hits.get(key, []) if now - t < window_s]
        if len(hits) >= limit:
            _hits[key] = hits
            return False
        hits.append(now)
        _hits[key] = hits
        return True


def _throttled(email: str, remote: str) -> bool:
    """3 mails an hour per address, 20 an hour per source address."""
    return not (_allow("ip:" + (remote or ""), 20) and _allow("mail:" + email, 3))


# -------------------------------------------------------------------- mail

def _page(title: str, body: str, button: tuple | None = None, foot: str = "") -> str:
    btn = ""
    if button:
        btn = (f'<p style="margin:22px 0"><a href="{_html.escape(button[1], quote=True)}" '
               f'style="background:#1E88E5;color:#fff;text-decoration:none;padding:11px 20px;'
               f'border-radius:8px;font-weight:600;display:inline-block">{_html.escape(button[0])}</a></p>'
               f'<p style="font-size:12px;color:#5b6b83">If the button does not work, copy this link:<br>'
               f'<span style="word-break:break-all">{_html.escape(button[1])}</span></p>')
    return (f'<div style="font-family:Segoe UI,Arial,sans-serif;max-width:560px;color:#17304F;line-height:1.5">'
            f'<h2 style="font-weight:600;color:#1565C0">{_html.escape(title)}</h2>{body}{btn}'
            f'<p style="font-size:12px;color:#8AA2BD;margin-top:26px">{foot or "InfraBeatOps · SAP Basis monitoring"}</p></div>')


def send_mail(to: str, subject: str, html: str) -> bool:
    """One account mail. kind='' on purpose: these go to the person named, client or not."""
    if not to:
        return False
    try:
        from notifications import mailer
        return bool(mailer.send_html_now(subject, html, [to], mailer._default_smtp()))
    except Exception as exc:  # noqa: BLE001 -- never fail the request over mail
        log.warning(f"accounts: mail to {to} failed: {type(exc).__name__}: {exc}")
        return False


def _verify_mail(row, token: str) -> bool:
    link = f"{public_base()}/verify?token={token}"
    name = row["full_name"] or row["username"]
    return send_mail(_mail_of(row), "Confirm your InfraBeatOps account", _page(
        "Confirm your e-mail",
        f"<p>Hello {_html.escape(name)},</p><p>Confirm this address to finish creating your "
        f"InfraBeatOps account. The link works once and for "
        f"{_env_int('IBO_VERIFY_HOURS', 24)} hours.</p>"
        f"<p>After that, sign in and ask for access to your projects; your manager approves it.</p>",
        ("Confirm e-mail", link)))


def _reset_mail(row, token: str, invited_by: str = "", minutes: int = 30) -> bool:
    link = f"{public_base()}/reset?token={token}"
    name = row["full_name"] or row["username"]
    if invited_by:
        title, intro, subj = ("You have an InfraBeatOps account",
                              f"<p>Hello {_html.escape(name)},</p><p>{_html.escape(invited_by)} created an "
                              f"InfraBeatOps account for you as <b>{_html.escape(auth.ROLE_LABELS.get(row['role'], row['role']))}</b>"
                              f"{(' on ' + _html.escape(', '.join(sorted(projects_of(row) or [])))) if projects_of(row) else ''}. "
                              f"Choose your password to start. The link works once, for {minutes // 60} hours.</p>",
                              "Your InfraBeatOps account")
    else:
        title, intro, subj = ("Reset your password",
                              f"<p>Hello {_html.escape(name)},</p><p>Someone (hopefully you) asked to reset "
                              f"the InfraBeatOps password for <b>{_html.escape(row['username'])}</b>. The link "
                              f"works once and for {minutes} minutes. If it was not you, ignore this mail; "
                              f"your password stays as it is.</p>", "Reset your InfraBeatOps password")
    return send_mail(_mail_of(row), subj, _page(title, intro, ("Choose a password", link)))


# ------------------------------------------------------------------ sign-up

def signup(email: str, full_name: str, password: str, remote: str = "") -> dict:
    """
    Create a self-service account (allowed domains only). Returns
    {"created": bool, "mailed": bool}. Raises AccountError for input the
    person must fix; an address that already has an account gets a mail
    saying so instead of an error (no account discovery).
    """
    ensure_schema()
    email = normalise_email(email)
    if not email_domain_allowed(email):
        raise AccountError("Only " + ", ".join("@" + d for d in signup_domains()) +
                           " addresses can create their own account. Ask your manager to invite you.")
    auth.check_password_policy(password)
    full_name = re.sub(r"\s+", " ", (full_name or "").strip())[:80]
    if _throttled(email, remote):
        raise AccountError("Too many requests. Try again in an hour.")
    pw_hash = auth.hash_password(password)          # always: same time whether or not the address exists
    existing = _user_by_email(email)
    if existing is not None:
        if not existing["email_verified"]:
            # Nobody proved this mailbox yet. The latest sign-up's password is
            # the one the confirmation page will ask for -- so whoever typed
            # the address first gains nothing: only the mailbox owner gets
            # the link, and it needs the password she chose.
            store.conn().execute("UPDATE users SET pw_hash=?, full_name=? WHERE username=?",
                                 (pw_hash, full_name or existing["full_name"], existing["username"]))
            return {"created": False, "mailed": _verify_mail(existing, new_token(
                existing["username"], "verify", _env_int("IBO_VERIFY_HOURS", 24) * 60))}
        mailed = send_mail(email, "Your InfraBeatOps account", _page(
            "You already have an account",
            "<p>Someone tried to create an InfraBeatOps account with this address, which already has "
            "one. If that was you, sign in, or use <i>Forgot password</i> on the sign-in page.</p>",
            ("Sign in", f"{public_base()}/login")))
        return {"created": False, "mailed": mailed}
    try:
        store.conn().execute(
            "INSERT INTO users(username, pw_hash, role, customers, disabled, created_at, email, email_verified, "
            "full_name, created_by) VALUES (?,?,?,?,0,?,?,0,?,?)",
            (email, pw_hash, signup_role(), "", _now().strftime(_FMT), email, full_name, "self-signup"))
    except Exception as exc:  # noqa: BLE001 -- a name clash: answer as for an existing address
        log.warning(f"accounts: sign-up {email} clashed with an existing account: {exc}")
        return {"created": False, "mailed": True}
    log.info(f"accounts: self sign-up {email} from {remote}")
    row = _user(email)
    mailed = _verify_mail(row, new_token(email, "verify", _env_int("IBO_VERIFY_HOURS", 24) * 60))
    return {"created": True, "mailed": mailed}


def verify(token: str, password: str = "") -> str | None:
    """
    Confirm the address. Needs the password chosen at sign-up: only the
    mailbox owner gets the link, and only the person who chose the password
    knows it -- someone who signed up with another person's address first
    cannot finish. A mail scanner opening the link (GET) confirms nothing;
    the page posts the password.
    """
    username = peek_token(token, "verify")
    if not username:
        return None
    row = _user(username)
    if row is None or not auth.verify_password(password or "", row["pw_hash"]):
        raise AccountError("That is not the password chosen when the account was created. "
                           "If you did not create it, sign up again with your own password; "
                           "a new link replaces this one.")
    username = use_token(token, "verify")
    if username:
        store.conn().execute("UPDATE users SET email_verified=1 WHERE username=?", (username,))
        log.info(f"accounts: {username} confirmed the e-mail address")
    return username


def forgot(text: str, remote: str = "") -> None:
    """Mail a reset link if `text` is an account's e-mail or username. Says nothing either way."""
    ensure_schema()
    name = username_for_login(text)
    row = _user(name)
    email = _mail_of(row)
    if row is None or row["disabled"] or not email:
        log.info(f"accounts: password reset asked for unknown/unmailable '{(text or '')[:60]}' from {remote}")
        return
    if _throttled(email, remote):
        log.warning(f"accounts: password reset for {name} throttled ({remote})")
        return
    minutes = _env_int("IBO_RESET_MINUTES", 30)
    _reset_mail(row, new_token(row["username"], "reset", minutes), minutes=minutes)


def reset_password(token: str, password: str) -> str:
    auth.check_password_policy(password)
    username = use_token(token, "reset")
    if not username:
        raise AccountError("This link has expired or was already used. Ask for a new one.")
    auth.set_password(username, password)          # also ends every open session
    store.conn().execute("UPDATE users SET email_verified=1 WHERE username=?", (username,))
    log.info(f"accounts: password set for {username} through a mailed link")
    return username


def change_password(username: str, current: str, new: str) -> None:
    row = _user(username)
    if row is None or not auth.verify_password(current or "", row["pw_hash"]):
        raise AccountError("The current password is wrong.")
    auth.check_password_policy(new)
    auth.set_password(username, new)


# ------------------------------------------------------- who may manage whom

def _is_person(principal) -> bool:
    return bool(principal) and principal.get("via") in ("session", "local", "token")


_LEADS = ("team_lead", "operator")
_GRANTED = ("senior_consultant", "consultant")


def manages_project(principal, project: str) -> bool:
    """Admin; a manager or a team leader ON that project (a team leader only for
    senior consultants / consultants -- may_edit / update_user check that)."""
    role = (principal or {}).get("role")
    if role == "admin":
        return True
    if role not in ("manager",) + _LEADS:
        return False
    scope = principal.get("customers")
    return scope is None or project in scope


def _within(principal, projects) -> bool:
    """Every project in `projects` is one the principal manages (None = all)."""
    if (principal or {}).get("role") == "admin":
        return True
    if projects is None:
        return False
    return all(manages_project(principal, p) for p in projects)


def may_edit(principal, row) -> bool:
    """May change this account at all (role, projects, enable, reset)."""
    if row is None or not principal:
        return False
    if principal.get("role") == "admin":
        return True
    role = principal.get("role")
    if role not in ("manager",) + _LEADS:
        return False
    if row["role"] in ("admin", "manager"):
        return False
    if role in _LEADS and row["role"] not in _GRANTED:
        return False                      # a team leader looks after consultants only
    theirs, mine = projects_of(row), principal.get("customers")
    return theirs is None or mine is None or bool(theirs & mine)


def describe(row, principal=None) -> dict:
    projects = projects_of(row)
    return {"username": row["username"], "email": _mail_of(row), "full_name": row["full_name"],
            "role": row["role"], "role_label": auth.ROLE_LABELS.get(row["role"], row["role"]),
            "projects": "*" if projects is None else sorted(projects),
            "disabled": bool(row["disabled"]), "email_verified": bool(row["email_verified"]),
            "created_at": row["created_at"], "created_by": row["created_by"],
            "editable": may_edit(principal, row) if principal else False,
            "role_editable": bool(principal) and may_edit(principal, row) and _within(principal, projects)}


def visible_users(principal) -> list[dict]:
    ensure_schema()
    rows = store.conn().execute("SELECT * FROM users ORDER BY username").fetchall()
    role = (principal or {}).get("role")
    out = []
    for r in rows:
        if role == "admin":
            out.append(describe(r, principal))
            continue
        if role not in ("manager",) + _LEADS:
            continue
        projects = projects_of(r)
        mine = principal.get("customers")
        if r["role"] == "admin":
            continue
        if role in _LEADS and r["role"] not in _GRANTED and r["username"] != principal.get("username"):
            continue
        if projects is None or mine is None or (projects & mine):
            out.append(describe(r, principal))
    return out


def update_user(principal, username: str, *, role: str | None = None, add=(), remove=(),
                all_projects_flag: bool | None = None, disabled: bool | None = None,
                full_name: str | None = None) -> dict:
    row = _user(username)
    if row is None:
        raise AccountError("No such user.")
    if not may_edit(principal, row):
        raise AccountError("You may not change this account.")
    me = (principal or {}).get("username")
    projects = projects_of(row)
    c = store.conn()
    if principal.get("role") in _LEADS and ((role is not None and role != row["role"])
                                            or (disabled is not None and bool(disabled) != bool(row["disabled"]))
                                            or (full_name is not None
                                                and full_name.strip() != (row["full_name"] or ""))
                                            or all_projects_flag is not None):
        raise AccountError("A team leader can add or remove his projects and set permissions; a manager "
                           "changes roles and accounts.")
    if all_projects_flag is not None:
        if principal.get("role") != "admin":
            raise AccountError("Only an admin can give access to every project.")
        projects = None if all_projects_flag else set()
    if (add or remove) and projects is None and principal.get("role") != "admin":
        raise AccountError("This person has access to every project; ask an admin to change it.")
    if full_name is not None and full_name.strip() != (row["full_name"] or "") and not _within(principal, projects):
        raise AccountError("This person also works on projects you do not manage; ask an admin.")
    for p in list(add or []) + list(remove or []):
        if p not in all_projects() and p not in (projects or set()):
            raise AccountError(f"Unknown project: {p}")
        if not manages_project(principal, p):
            raise AccountError(f"You do not manage the project {p}.")
    if add or remove:
        if projects is None:
            if remove:
                projects = set(all_projects())
            else:
                add = ()
        if projects is not None:
            projects = (projects | set(add or ())) - set(remove or ())
    if role is not None and role != row["role"]:
        if role not in auth.grantable_roles(principal):
            raise AccountError(f"You may not give the role {auth.ROLE_LABELS.get(role, role)}.")
        if not _within(principal, projects_of(row)):
            raise AccountError("This person also works on projects you do not manage; ask an admin "
                               "to change the role.")
        if username == me:
            raise AccountError("You cannot change your own role.")
    if disabled is not None and username == me:
        raise AccountError("You cannot disable your own account.")
    if disabled is not None and not _within(principal, projects_of(row)):
        raise AccountError("This person also works on projects you do not manage; ask an admin.")
    c.execute("UPDATE users SET customers=?, role=?, full_name=? WHERE username=?",
              ("*" if projects is None else _join(projects), role or row["role"],
               row["full_name"] if full_name is None else full_name.strip()[:80], row["username"]))
    if disabled is not None:
        if disabled:
            auth.disable_user(row["username"])
        else:
            c.execute("UPDATE users SET disabled=0 WHERE username=?", (row["username"],))
    if (role and role != row["role"]) or remove:
        # rights went down: end the open sessions so they take effect now
        c.execute("DELETE FROM sessions WHERE username=?", (row["username"],))
    log.info(f"accounts: {me} changed {row['username']}: role={role} +{list(add or [])} -{list(remove or [])} "
             f"disabled={disabled}")
    return describe(_user(row["username"]), principal)


def invite(principal, email: str, full_name: str, role: str, projects: list[str]) -> dict:
    """Create an account for someone else and mail a set-password link."""
    ensure_schema()
    email = normalise_email(email)
    if role not in auth.grantable_roles(principal):
        raise AccountError(f"You may not give the role {auth.ROLE_LABELS.get(role, role)}.")
    projects = [p for p in (projects or []) if p]
    if role != "admin" and not projects:
        raise AccountError("Pick at least one project.")
    for p in projects:
        if p not in all_projects():
            raise AccountError(f"Unknown project: {p}")
        if not manages_project(principal, p):
            raise AccountError(f"You do not manage the project {p}.")
    if _user_by_email(email) is not None:
        raise AccountError("That address already has an account; give it access from the list instead.")
    store.conn().execute(
        "INSERT INTO users(username, pw_hash, role, customers, disabled, created_at, email, email_verified, "
        "full_name, created_by) VALUES (?,?,?,?,0,?,?,0,?,?)",
        (email, auth.hash_password(secrets.token_urlsafe(24)), role, "*" if role == "admin" else _join(projects),
         _now().strftime(_FMT), email, (full_name or "").strip()[:80], principal.get("username", "")))
    row = _user(email)
    hours = _env_int("IBO_INVITE_HOURS", 72)
    token = new_token(email, "reset", hours * 60)
    inviter = principal.get("username", "An administrator")
    mailed = _reset_mail(row, token, invited_by=inviter, minutes=hours * 60)
    log.info(f"accounts: {inviter} invited {email} as {role} on {projects}")
    out = {"user": describe(row, principal), "mailed": mailed}
    if not mailed:
        out["link"] = f"{public_base()}/reset?token={token}"   # hand it over yourself
    return out


def send_reset_for(principal, username: str) -> dict:
    row = _user(username)
    if not may_edit(principal, row) or not _within(principal, projects_of(row)):
        raise AccountError("Only an admin can reset this password: the person also works on projects "
                           "you do not manage.")
    if not _mail_of(row):
        raise AccountError("This account has no e-mail address.")
    hours = _env_int("IBO_INVITE_HOURS", 72)
    token = new_token(row["username"], "reset", hours * 60)
    mailed = _reset_mail(row, token, minutes=hours * 60)
    out = {"mailed": mailed}
    if not mailed:
        if _within(principal, projects_of(row)):
            out["link"] = f"{public_base()}/reset?token={token}"
        else:
            # the link would let you sign in as someone who also works elsewhere
            store.conn().execute("UPDATE auth_tokens SET used=1 WHERE token_hash=?", (_hash(token),))
            raise AccountError("The mail could not be sent; ask an admin to reset this password.")
    return out


# --------------------------------------------------------- access requests

def _approvers(project: str) -> list:
    ensure_schema()
    rows = store.conn().execute(
        "SELECT * FROM users WHERE disabled=0 AND role IN ('admin','manager')").fetchall()
    out = []
    for r in rows:
        scope = projects_of(r)
        if r["role"] == "admin" or scope is None or project in scope:
            out.append(r)
    managers = [r for r in out if r["role"] == "manager"]
    return managers or out          # the project's managers; admins only when it has none


def request_access(principal, projects: list[str], role_wanted: str = "", note: str = "") -> list[dict]:
    ensure_schema()
    username = (principal or {}).get("username")
    row = _user(username)
    if row is None:
        raise AccountError("Sign in with your own account to ask for access.")
    have = projects_of(row)
    known = set(all_projects())
    projects = [p for p in dict.fromkeys(projects or []) if p]
    if not projects:
        raise AccountError("Pick at least one project.")
    if role_wanted and role_wanted not in auth.MANAGER_GRANTS:
        role_wanted = ""
    c = store.conn()
    made = []
    for p in projects:
        if p not in known:
            raise AccountError(f"Unknown project: {p}")
        if have is None or p in have:
            continue
        if c.execute("SELECT 1 FROM access_requests WHERE username=? AND project=? AND status='pending'",
                     (username, p)).fetchone():
            continue
        cur = c.execute("INSERT INTO access_requests(username, project, role_wanted, note, created_at) "
                        "VALUES (?,?,?,?,?)", (username, p, role_wanted, (note or "").strip()[:500],
                                               _now().strftime(_FMT)))
        made.append({"id": cur.lastrowid, "project": p})
        who = row["full_name"] or username
        for a in _approvers(p):
            send_mail(_mail_of(a), f"Access request: {who} for {p}", _page(
                f"{who} asks for access to {p}",
                f"<p><b>{_html.escape(who)}</b> ({_html.escape(_mail_of(row) or username)}) asks for access to "
                f"the project <b>{_html.escape(p)}</b>"
                f"{(' as ' + _html.escape(auth.ROLE_LABELS[role_wanted])) if role_wanted else ''}.</p>"
                f"{('<p>Note: ' + _html.escape(note.strip()[:500]) + '</p>') if (note or '').strip() else ''}"
                f"<p>Approve or reject it on the Team &amp; access page.</p>",
                ("Open Team & access", f"{public_base()}/team")))
    log.info(f"accounts: {username} asked for access to {[m['project'] for m in made]}")
    return made


def list_requests(principal) -> dict:
    ensure_schema()
    me = (principal or {}).get("username")
    c = store.conn()
    mine = [dict(r) for r in c.execute(
        "SELECT * FROM access_requests WHERE username=? ORDER BY id DESC LIMIT 50", (me,)).fetchall()]
    to_decide = []
    if (principal or {}).get("role") in ("admin", "manager"):
        for r in c.execute("SELECT a.*, u.full_name, u.email, u.role AS current_role, u.customers "
                           "FROM access_requests a LEFT JOIN users u ON u.username=a.username "
                           "WHERE a.status='pending' ORDER BY a.id").fetchall():
            if manages_project(principal, r["project"]):
                d = dict(r)
                d["current_role_label"] = auth.ROLE_LABELS.get(d.get("current_role") or "", d.get("current_role") or "")
                d.pop("customers", None)
                to_decide.append(d)
    return {"mine": mine, "to_decide": to_decide}


def decide(principal, request_id: int, approve: bool, role: str = "", note: str = "") -> dict:
    ensure_schema()
    c = store.conn()
    req = c.execute("SELECT * FROM access_requests WHERE id=?", (int(request_id),)).fetchone()
    if req is None or req["status"] != "pending":
        raise AccountError("This request is no longer open.")
    if principal.get("role") not in ("admin", "manager") or not manages_project(principal, req["project"]):
        raise AccountError("Only a manager of this project (or an admin) can decide it.")
    row = _user(req["username"])
    if row is None:
        raise AccountError("That account no longer exists.")
    me = principal.get("username", "")
    role_note = ""
    final_role = row["role"]
    if approve:
        if req["username"] == me:
            raise AccountError("Someone else has to approve your own request.")
        if row["role"] in ("admin", "manager") and principal.get("role") != "admin":
            raise AccountError("A manager's access is given by an admin.")
        role = role or req["role_wanted"] or row["role"]
        projects = projects_of(row)
        if role != row["role"]:
            if role not in auth.grantable_roles(principal):
                raise AccountError(f"You may not give the role {auth.ROLE_LABELS.get(role, role)}.")
            if _within(principal, projects):
                final_role = role
            else:
                role_note = (f" Role stays {auth.ROLE_LABELS.get(row['role'], row['role'])}: the person also "
                             f"works on projects you do not manage.")
        if row["role"] in ("admin", "manager") and principal.get("role") != "admin":
            final_role = row["role"]
        if projects is not None:
            projects = projects | {req["project"]}
        c.execute("UPDATE users SET customers=?, role=? WHERE username=?",
                  ("*" if projects is None else _join(projects), final_role, row["username"]))
    status = "approved" if approve else "rejected"
    c.execute("UPDATE access_requests SET status=?, decided_by=?, decided_at=?, decided_role=?, decision_note=? "
              "WHERE id=?", (status, me, _now().strftime(_FMT), final_role if approve else "",
                             ((note or "").strip()[:500] + role_note).strip(), req["id"]))
    who = principal.get("username", "")
    send_mail(_mail_of(row), f"Access to {req['project']} {status}", _page(
        f"Access to {req['project']} {status}",
        (f"<p>{_html.escape(who)} gave you access to <b>{_html.escape(req['project'])}</b> as "
         f"<b>{_html.escape(auth.ROLE_LABELS.get(final_role, final_role))}</b>.{_html.escape(role_note)}</p>"
         if approve else
         f"<p>{_html.escape(who)} did not approve your access to <b>{_html.escape(req['project'])}</b>.</p>")
        + (f"<p>Note: {_html.escape(note.strip()[:500])}</p>" if (note or "").strip() else ""),
        ("Open InfraBeatOps", f"{public_base()}/") if approve else None))
    log.info(f"accounts: {me} {status} access of {row['username']} to {req['project']} ({final_role})")
    return {"status": status, "role": final_role, "note": role_note.strip()}


def withdraw(principal, request_id: int) -> None:
    ensure_schema()
    cur = store.conn().execute(
        "UPDATE access_requests SET status='withdrawn' WHERE id=? AND username=? AND status='pending'",
        (int(request_id), (principal or {}).get("username")))
    if not cur.rowcount:
        raise AccountError("This request is no longer open.")
