"""
Sign in with Microsoft (Entra ID), OpenID Connect authorization-code flow
with PKCE. Local accounts keep working beside it (fallback, and for the
API token).

ONE-TIME SETUP (needs someone with rights to register apps in the tenant)
  Entra admin center -> App registrations -> New registration
    Name: InfraBeatOps dashboard
    Supported account types: this organisation only
    Redirect URI (Web): <IBO_PUBLIC_URL>/auth/callback
  Certificates & secrets -> New client secret -> copy the VALUE
  Token configuration -> Add optional claim (ID) -> email   [and "groups" if
    you map Entra groups to roles below]
.env on each dashboard that should offer it:
    IBO_ENTRA_TENANT_ID=<Directory (tenant) ID>
    IBO_ENTRA_CLIENT_ID=<Application (client) ID>
    IBO_ENTRA_CLIENT_SECRET=<secret value>
    IBO_PUBLIC_URL=https://...          (the redirect URI is built from it)

WHO GETS IN, WITH WHICH ROLE -- config/sso.yaml (copy the .example):
    allowed_domains: [infrabeat.com]
    roles:
      admin: [staff.a@infrabeat.com]
      operator: ["*@infrabeat.com"]
      viewer: []
    groups: {"<entra group object id>": operator}
    customers: {"*@metro.example": Metro}      # customer scope, default all
    default_role: none                          # none = refuse anyone not matched
The role is re-evaluated at every sign-in; removing someone from sso.yaml
(or from the Entra group) takes effect at their next sign-in, and their
current sessions can be ended with scripts/ibops_user.py disable <email>.

SECURITY NOTES
* state, nonce and the PKCE verifier travel in a short-lived (10 min),
  HMAC-signed, HttpOnly cookie, checked on return.
* The ID token comes straight from Microsoft's token endpoint over TLS
  (certificate verified), which OpenID Connect Core 1.0 section 3.1.3.7
  allows in place of checking the token signature; issuer, audience, tenant,
  expiry and nonce are all checked.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

from utils.logger import get_logger

log = get_logger(__name__)

AUTHORITY = "https://login.microsoftonline.com"
COOKIE = "ibo_sso_flow"


_MULTI = ("common", "organizations", "consumers")


def configured() -> bool:
    return all(os.getenv(k, "").strip() for k in ("IBO_ENTRA_TENANT_ID", "IBO_ENTRA_CLIENT_ID",
                                                  "IBO_ENTRA_CLIENT_SECRET")) and bool(_redirect_uri()) \
        and _tenant().lower() not in _MULTI


def problem() -> str:
    """Why sign-in with Microsoft is not offered (for the Health page), or ''."""
    if _tenant().lower() in _MULTI:
        return (f"IBO_ENTRA_TENANT_ID={_tenant()} is not allowed: set the Directory (tenant) ID of your own "
                f"organisation, so accounts from other organisations cannot sign in")
    if not configured():
        return "not configured"
    return ""


def _tenant() -> str:
    return os.getenv("IBO_ENTRA_TENANT_ID", "").strip()


def _client() -> str:
    return os.getenv("IBO_ENTRA_CLIENT_ID", "").strip()


def _redirect_uri() -> str:
    from core.action_links import public_url
    explicit = os.getenv("IBO_ENTRA_REDIRECT_URI", "").strip()
    return explicit or (f"{public_url()}/auth/callback" if public_url() else "")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: dict) -> str:
    from core.action_links import _secret
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return f"{_b64(raw)}.{_b64(hmac.new(_secret(), b'sso|' + raw, hashlib.sha256).digest())}"


def _verify(token: str) -> dict:
    from core.action_links import _secret
    try:
        a, b = token.split(".", 1)
        raw = _unb64(a)
        if not hmac.compare_digest(_unb64(b), hmac.new(_secret(), b"sso|" + raw, hashlib.sha256).digest()):
            raise ValueError
        data = json.loads(raw)
    except Exception:
        raise ValueError("Sign-in state is invalid -- start again.")
    if data.get("exp", 0) < time.time():
        raise ValueError("Sign-in took longer than 10 minutes -- start again.")
    return data


def start(next_path: str = "/") -> tuple[str, str]:
    """(redirect URL to Microsoft, signed cookie value)."""
    if not configured():
        raise ValueError("Microsoft sign-in is not configured.")
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    nxt = next_path if str(next_path or "").startswith("/") and not str(next_path).startswith("//") else "/"
    cookie = _sign({"s": state, "n": nonce, "v": verifier, "next": nxt, "exp": int(time.time()) + 600})
    q = urlencode({"client_id": _client(), "response_type": "code", "redirect_uri": _redirect_uri(),
                   "response_mode": "query", "scope": "openid profile email", "state": state, "nonce": nonce,
                   "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"})
    return f"{AUTHORITY}/{_tenant()}/oauth2/v2.0/authorize?{q}", cookie


def _claims(id_token: str) -> dict:
    try:
        return json.loads(_unb64(id_token.split(".")[1]))
    except Exception:
        raise ValueError("Microsoft returned an unreadable ID token.")


def _validate(claims: dict, nonce: str) -> None:
    tid = str(claims.get("tid") or "")
    if claims.get("aud") != _client():
        raise ValueError("ID token is for another application.")
    if claims.get("iss") != f"{AUTHORITY}/{tid}/v2.0":
        raise ValueError("ID token has an unexpected issuer.")
    if not tid or tid.lower() != _tenant().lower():
        raise ValueError("Account belongs to another Microsoft tenant.")
    now = time.time()
    if float(claims.get("exp", 0)) < now - 120 or float(claims.get("nbf", 0)) > now + 120:
        raise ValueError("ID token has expired -- sign in again.")
    if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise ValueError("Sign-in nonce does not match -- start again.")


def _mapping() -> dict:
    try:
        import yaml
        from core.config_loader import CONFIG_DIR
        path = os.path.join(CONFIG_DIR, "sso.yaml")
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"config/sso.yaml unreadable: {exc}")
        return {}


def role_for(email: str, groups: list[str]) -> tuple[str | None, str]:
    """(role or None when refused, customers scope)."""
    m = _mapping()
    email = email.lower()
    domains = [d.lower().lstrip("@") for d in (m.get("allowed_domains") or [])]
    if domains and email.rsplit("@", 1)[-1] not in domains:
        return None, "*"
    role = None
    from core.auth import ROLE_LEVEL
    order = sorted(ROLE_LEVEL, key=lambda r: -ROLE_LEVEL[r])
    for r in order:
        if any(fnmatch.fnmatchcase(email, str(p).lower()) for p in ((m.get("roles") or {}).get(r) or [])):
            role = r
            break
    if role is None:
        rank = ROLE_LEVEL
        best = None
        for gid, r in (m.get("groups") or {}).items():
            if str(gid) in groups and r in rank and (best is None or rank[r] > rank[best]):
                best = r
        role = best
    if role is None:
        d = str(m.get("default_role") or "none").lower()
        role = d if d in ROLE_LEVEL else None
    customers = "*"
    for pattern, scope in (m.get("customers") or {}).items():
        if fnmatch.fnmatchcase(email, str(pattern).lower()):
            customers = ",".join(scope) if isinstance(scope, list) else str(scope)
            break
    return role, customers


def _sso_table():
    from core import ops_db
    ops_db.db().execute("CREATE TABLE IF NOT EXISTS sso_users (username TEXT PRIMARY KEY, first_at TEXT NOT NULL)")
    return ops_db


def _upsert_user(email: str, role: str, customers: str) -> str:
    """Create or update the dashboard account; returns the role it ends up with.

    * a new person gets an account managed by sso.yaml (random password:
      they cannot sign in with a password);
    * an account first made by Microsoft sign-in follows sso.yaml at every
      sign-in (role and customers), its password is never touched;
    * an account that existed as a LOCAL account (made with
      scripts/ibops_user.py) keeps its password, role and customers -- the
      local admin does not lose the password fallback or get downgraded.
    """
    from core import auth
    ops_db = _sso_table()
    from core import accounts
    accounts.ensure_schema()
    row = auth.store.conn().execute("SELECT role, disabled, email_verified FROM users WHERE username=?",
                                    (email,)).fetchone()
    if row is not None and row["disabled"]:
        raise ValueError(f"{email} has been disabled on this dashboard.")
    if row is not None and not row["email_verified"]:
        # A self sign-up nobody confirmed: whoever typed this address may not
        # be its owner. Microsoft just proved the owner, so the account becomes
        # hers: random password, managed by sso.yaml from now on.
        auth.store.conn().execute(
            "UPDATE users SET pw_hash=?, role=?, customers=?, email_verified=1 WHERE username=?",
            (auth.hash_password(secrets.token_urlsafe(32)), role, customers or "*", email))
        auth.store.conn().execute("DELETE FROM sessions WHERE username=?", (email,))
        ops_db.execute("INSERT OR IGNORE INTO sso_users(username, first_at) VALUES (?,?)", (email, ops_db.now()))
        return role
    managed = ops_db.one("SELECT username FROM sso_users WHERE username=?", (email,)) is not None
    if row is None:
        auth.add_user(email, secrets.token_urlsafe(32), role, customers)
        ops_db.execute("INSERT OR IGNORE INTO sso_users(username, first_at) VALUES (?,?)", (email, ops_db.now()))
        return role
    if managed:
        auth.store.conn().execute("UPDATE users SET role=?, customers=? WHERE username=?",
                                  (role, customers or "*", email))
        return role
    return row["role"]


def finish(code: str, state: str, cookie: str, remote: str = "") -> tuple[str, str, str]:
    """Exchange the code; returns (session token, username, next path). Raises ValueError with a sentence."""
    import requests
    from core import auth, timeline
    flow = _verify(cookie or "")
    if not hmac.compare_digest(str(state or ""), str(flow.get("s"))):
        raise ValueError("Sign-in state does not match -- start again.")
    r = requests.post(f"{AUTHORITY}/{_tenant()}/oauth2/v2.0/token", timeout=20, data={
        "client_id": _client(), "client_secret": os.getenv("IBO_ENTRA_CLIENT_SECRET", ""),
        "grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(),
        "code_verifier": flow["v"], "scope": "openid profile email"})
    if r.status_code >= 300:
        try:
            err = r.json().get("error_description", "")[:200]
        except ValueError:
            err = r.text[:200]
        raise ValueError(f"Microsoft refused the sign-in: {err}")
    id_token = r.json().get("id_token")
    if not id_token:
        raise ValueError("Microsoft did not return an ID token.")
    claims = _claims(id_token)
    _validate(claims, flow["n"])
    # preferred_username (the UPN) is controlled by the tenant's verified
    # domains; the optional "email" claim is only a fallback.
    email = str(claims.get("preferred_username") or claims.get("email") or "").strip().lower()
    if "@" not in email:
        raise ValueError("Your Microsoft account has no e-mail address the dashboard can use.")
    role, customers = role_for(email, [str(g) for g in (claims.get("groups") or [])])
    if role is None:
        log.warning(f"sso: {email} signed in with Microsoft but is not allowed (config/sso.yaml)")
        raise ValueError(f"{email} is not allowed to use this dashboard. Ask an admin to add you.")
    role = _upsert_user(email, role, customers)
    token = auth.create_session(email, remote, how="Microsoft")
    timeline.add("IBOPS", "config", f"{email} signed in with Microsoft ({role})", actor=email)
    return token, email, flow.get("next") or "/"
