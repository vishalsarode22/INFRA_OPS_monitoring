"""
Passwords and tokens on the Settings page -- WRITE-ONLY.

The page shows, per secret, only whether it is set and where it lives
(.env or the Windows credential store). The value is never sent back to a
browser, never logged, never put on the Timeline. An admin can type a new
value (twice); it is stored and used at once.

Which secrets
  * the platform's own: SMTP password, WhatsApp token, MSG91 key, Entra
    client secret, AI keys;
  * every ${VAR} that config/systems.yaml uses for a password / token /
    secret (the SAP, RFC, SSH and SMTP passwords of each system).
  IBO_API_TOKEN is NOT here on purpose: it is the key between the Windows
  and the Ubuntu dashboard, and changing it on one side alone cuts the link.

Where a new value goes
  * already in .env            -> .env (an entry there wins over the store)
  * in the credential store    -> the credential store
  * nowhere yet                -> the credential store when the `keyring`
                                  package works here, else .env
  .env values are quoted so that #, spaces and $ inside a password are kept
  exactly (python-dotenv: single quotes = literal).

Every change: Timeline line (which secret, who -- never the value) and a
mail to Basis, so a change nobody expected is noticed.
"""
from __future__ import annotations

import os
import re
import threading

from utils.logger import get_logger

log = get_logger(__name__)

PLATFORM: dict[str, str] = {
    "SMTP_PASSWORD": "SMTP password (alert and report mails)",
    "WHATSAPP_ACCESS_TOKEN": "WhatsApp access token",
    "IBO_MSG91_AUTHKEY": "MSG91 auth key (SMS / calls)",
    "IBO_ENTRA_CLIENT_SECRET": "Microsoft sign-in client secret",
    "GEMINI_API_KEY": "Gemini API key (AI analysis)",
    "GROK_API_KEY": "Grok API key (AI analysis)",
}
NEVER = {"IBO_API_TOKEN"}
_SECRET_WORD = re.compile(r"(PASSWORD|PASSWD|PWD|SECRET|TOKEN|AUTHKEY|API_KEY)", re.I)
_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,80}$")
_lock = threading.Lock()


def _keyring():
    try:
        import keyring
        keyring.get_password("infrabeatops", "__probe__")      # raises when no usable backend
        return keyring
    except Exception:
        return None


def _env_file_values() -> dict:
    try:
        from dotenv import dotenv_values
        from core.delivery_settings import ENV_PATH
        return dict(dotenv_values(ENV_PATH)) if os.path.isfile(ENV_PATH) else {}
    except Exception:
        return {}


def system_secrets() -> dict[str, str]:
    """{VAR: 'PS4 -- rfc password'} for every secret-looking ${VAR} in systems.yaml."""
    out: dict[str, str] = {}
    try:
        import yaml
        from core.config_loader import CONFIG_DIR
        with open(os.path.join(CONFIG_DIR, "systems.yaml"), "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return out

    def walk(node, system, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, system, path + [str(k)])
        elif isinstance(node, list):
            for v in node:
                walk(v, system, path)
        elif isinstance(node, str):
            for var in _REF.findall(node):
                if _SECRET_WORD.search(var) or (path and _SECRET_WORD.search(path[-1])):
                    out.setdefault(var, f"{system} -- {' '.join(path).replace('_', ' ')}")
    for entry in (data.get("systems") or []) if isinstance(data, dict) else []:
        if isinstance(entry, dict):
            walk(entry, str(entry.get("name") or "?"), [])
    return out


def catalogue() -> dict[str, str]:
    cat = dict(PLATFORM)
    for k, v in system_secrets().items():
        cat.setdefault(k, v)
    for k in NEVER:
        cat.pop(k, None)
    return cat


def status() -> list[dict]:
    """[{key, label, set, where}] -- never a value."""
    envfile = _env_file_values()
    kr = _keyring()
    out = []
    for key, label in catalogue().items():
        where = ""
        if (envfile.get(key) or "").strip():
            where = ".env"
        elif kr is not None:
            try:
                if kr.get_password("infrabeatops", key):
                    where = "credential store"
            except Exception:
                pass
        if not where and (os.getenv(key) or "").strip():
            where = "environment"
        out.append({"key": key, "label": label, "set": bool(where), "where": where,
                    "group": "Platform" if key in PLATFORM else label.split(" -- ")[0]})
    return out


def _quote(value: str) -> str:
    """A .env value python-dotenv reads back exactly."""
    if "'" not in value:
        return "'" + value + "'"                    # single quotes: literal, no ${} expansion
    if "$" in value:
        raise ValueError("A password with both ' and $ cannot be stored in .env safely. Store it in the "
                         "credential store instead: python scripts/set_secret.py <NAME>")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(key: str, value: str, by: str) -> dict:
    """Store one secret. Raises ValueError with a sentence; the value is never echoed."""
    from core import delivery_settings, timeline
    key = str(key or "").strip()
    if key in NEVER or not _NAME.match(key) or key not in catalogue():
        raise ValueError(f"{key} cannot be set here.")
    value = str(value or "")
    if not value.strip():
        raise ValueError("Type the new value (blank is not allowed -- nothing was changed).")
    if len(value) > 512 or any(c in value for c in "\r\n\x00"):
        raise ValueError("The value must be one line, at most 512 characters.")
    with _lock:
        in_env = bool((_env_file_values().get(key) or "").strip())
        kr = _keyring()
        in_store = False
        if kr is not None and not in_env:
            try:
                in_store = bool(kr.get_password("infrabeatops", key))
            except Exception:
                in_store = False
        if kr is not None and not in_env:
            kr.set_password("infrabeatops", key, value)
            where = "credential store"
        else:
            with delivery_settings._lock:
                try:
                    delivery_settings._rewrite_env({key: _quote(value)})
                except OSError as exc:
                    raise ValueError(f"Could not write .env ({exc.strerror or exc}); nothing was changed.")
            where = ".env"
        os.environ[key] = value                     # this process uses it at once
    try:
        from core.config_loader import invalidate_systems_cache, secrets_marker
        invalidate_systems_cache()
        # Tells the other processes (RFC workers, jobs) to re-read passwords
        # and to stop waiting out a cooldown caused by the old one.
        with open(secrets_marker(), "w", encoding="utf-8") as fh:
            fh.write(key + "\n")
    except Exception:
        pass
    try:
        from collectors.rfc_collector import clear_cooldown
        clear_cooldown()
    except Exception:
        pass
    label = catalogue().get(key, key)
    try:
        timeline.add("IBOPS", "config", f"Secret changed: {key}", f"{label}; stored in {where}"
                     + (" (was there already)" if in_env or in_store else " (new)"), severity="warning",
                     actor=by or timeline.actor_name())
    except Exception:
        pass
    log.warning(f"secret {key} changed from the Settings page by {by or '?'} (stored in {where})")
    _notify(key, label, by, where)
    return {"key": key, "where": where}


def _notify(key: str, label: str, by: str, where: str) -> None:
    try:
        import html
        from core import recipients
        from core.config_loader import get_smtp_config
        from notifications.mailer import send_html
        to = recipients.basis(None)
        if not to:
            return
        send_html(subject=f"[IBOPS] Password / token changed: {key}",
                  html=(f"<p><b>{html.escape(key)}</b> ({html.escape(label)}) was changed on the Settings page "
                        f"by <b>{html.escape(by or '?')}</b>; stored in {html.escape(where)}.</p>"
                        f"<p>If nobody in the team expected this, check the Timeline (IBOPS) and the users.</p>"),
                  to=to, smtp_config=get_smtp_config(None, require_recipients=False),
                  system="IBOPS", kind="secret_changed", key=key, min_interval_s=0, severity="WARNING")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"secret change mail not sent: {exc}")
