"""
Named sender accounts.

One global sender does not survive contact with a real landscape: PRD alerts
should not arrive from the same mailbox as QAS noise, and a WhatsApp number
registered for one business unit is not the one another wants to send from.
So senders are named accounts, and a profile picks which one it uses.

WHERE EACH PART LIVES -- the same split as everywhere else in this project

Account settings (host, port, username, from address, phone number id) are
configuration. They live in config/delivery_accounts.yaml and are shown in
full.

Passwords and tokens are secrets. They live in .env under a key derived from
the account id -- SMTP_PASSWORD__PRD_MAIL -- and are never read back to the
browser. The UI shows "configured" or "not configured".

WHY THE KEY IS DERIVED, NOT STORED
The account file holds no reference to the secret's value or location beyond
the id. If someone screenshots or commits delivery_accounts.yaml, they leak
which mailboxes exist, not how to use them. That is a meaningfully smaller
problem than the one this project already has in its git history.
"""

from __future__ import annotations

import os
import re
import threading

import yaml

from core.config_loader import CONFIG_DIR
from core.delivery_settings import _rewrite_env, normalise_recipients, validate_recipients
from utils.logger import get_logger

log = get_logger(__name__, "delivery_accounts")

ACCOUNTS_PATH = os.path.join(CONFIG_DIR, "delivery_accounts.yaml")

_lock = threading.Lock()

EMAIL_FIELDS = ("host", "port", "username", "from_email", "use_tls")
WHATSAPP_FIELDS = ("mode", "phone_number_id", "webhook_url")


def secret_key(kind: str, account_id: str) -> str:
    """
    Environment variable holding this account's secret.

    Derived, never stored: the account file then contains no pointer to a
    credential, only the name of a mailbox.
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "_", str(account_id)).strip("_").upper()
    prefix = "SMTP_PASSWORD" if kind == "email" else "WHATSAPP_TOKEN"
    return f"{prefix}__{safe}"


def _load_raw() -> dict:
    if not os.path.isfile(ACCOUNTS_PATH):
        return {}
    try:
        with open(ACCOUNTS_PATH, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception as exc:
        # A broken accounts file must not stop monitoring; it degrades to the
        # single global sender already configured in .env.
        log.error(f"Could not read {ACCOUNTS_PATH}: {exc}")
        return {}


def load_accounts() -> dict:
    """{'email': [...], 'whatsapp': [...]} with secret status, never secrets."""
    raw = _load_raw()
    out = {"email": [], "whatsapp": []}

    for kind, fields in (("email", EMAIL_FIELDS), ("whatsapp", WHATSAPP_FIELDS)):
        for entry in raw.get(kind, []) or []:
            aid = str(entry.get("id") or "").strip()
            if not aid:
                continue
            account = {
                "id": aid,
                "label": entry.get("label") or aid,
                "systems": [str(s).strip() for s in (entry.get("systems") or [])
                            if str(s).strip()],
            }
            for field in fields:
                account[field] = entry.get(field, "")
            key = secret_key(kind, aid)
            account["secret_key"] = key
            account["secret_configured"] = bool(os.getenv(key, "").strip())
            out[kind].append(account)

    return out


def save_accounts(accounts: dict, secrets: dict | None = None) -> dict:
    """
    Write account settings, and any secrets actually supplied.

    A blank or absent secret means KEEP. The UI cannot display the current
    value so it cannot resubmit it, and treating blank as "clear" would wipe
    a working password the first time someone saved an unrelated change.
    Pass "__CLEAR__" to remove one deliberately.
    """
    cleaned = {"email": [], "whatsapp": []}
    seen: set[str] = set()

    for kind, fields in (("email", EMAIL_FIELDS), ("whatsapp", WHATSAPP_FIELDS)):
        for entry in (accounts or {}).get(kind, []) or []:
            aid = str(entry.get("id") or "").strip()
            if not aid:
                raise ValueError(f"Every {kind} account needs an id.")
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", aid):
                # The id becomes part of an environment variable name, so a
                # space or a slash in it would produce a key that cannot be
                # set and a secret that silently never loads.
                raise ValueError(f"{aid}: use letters, digits, dot, dash or "
                                 f"underscore only -- the id becomes part of "
                                 f"an environment variable name.")
            if aid in seen:
                raise ValueError(f"Duplicate account id: {aid}")
            seen.add(aid)

            if kind == "email":
                sender = str(entry.get("from_email") or "").strip()
                if sender:
                    problems = validate_recipients(emails=sender)
                    if problems:
                        raise ValueError(f"{aid}: {problems[0]}")

            record = {"id": aid,
                      "label": entry.get("label") or aid,
                      "systems": [str(s).strip()
                                  for s in (entry.get("systems") or [])
                                  if str(s).strip()]}
            for field in fields:
                record[field] = entry.get(field, "")
            cleaned[kind].append(record)

    updates: dict[str, str] = {}
    for full_key, value in (secrets or {}).items():
        text = str(value or "").strip()
        if not text:
            continue
        valid = {secret_key(k, a["id"])
                 for k in ("email", "whatsapp") for a in cleaned[k]}
        if full_key not in valid:
            # Only keys belonging to an account being saved may be written.
            # Without this the endpoint could set any environment variable.
            raise ValueError(f"{full_key} does not belong to any account here.")
        updates[full_key] = "" if text == "__CLEAR__" else text

    with _lock:
        os.makedirs(os.path.dirname(ACCOUNTS_PATH), exist_ok=True)
        header = (
            "# Sender accounts. Edited from the Profiles page.\n"
            "#\n"
            "# Settings live here; passwords and tokens live in .env under\n"
            "# SMTP_PASSWORD__<ID> and WHATSAPP_TOKEN__<ID>. This file holds no\n"
            "# credential and no pointer to one beyond the account name, so\n"
            "# leaking it exposes which mailboxes exist, not how to use them.\n\n")
        with open(ACCOUNTS_PATH, "w", encoding="utf-8") as handle:
            handle.write(header)
            yaml.safe_dump(cleaned, handle, sort_keys=False,
                           default_flow_style=False, allow_unicode=True)

        if updates:
            _rewrite_env(updates)
            for key, value in updates.items():
                # Apply now: .env is read at startup, and a saved setting that
                # needs a restart looks like a setting that did not save.
                os.environ[key] = value

    log.info(f"Saved {len(cleaned['email'])} email and "
             f"{len(cleaned['whatsapp'])} WhatsApp account(s)"
             + (f"; {len(updates)} secret(s) updated" if updates else ""))
    return load_accounts()


def resolve_email_account(account_id: str | None, system: str | None = None) -> dict:
    """
    The SMTP config to send with, as the mailer expects it.

    Resolution order: the account named by the profile, then an account
    claiming this system, then the global settings in .env. The fallback
    matters -- a profile with no account set must still send rather than
    silently deliver nothing.
    """
    accounts = load_accounts()["email"]

    chosen = None
    if account_id:
        chosen = next((a for a in accounts if a["id"] == account_id), None)
        if chosen is None:
            log.warning(f"Email account {account_id!r} not found; "
                        f"falling back to the global sender.")
    if chosen is None and system:
        chosen = next((a for a in accounts if system in (a.get("systems") or [])), None)

    if chosen is None:
        return {
            "host": os.getenv("SMTP_HOST", ""),
            "port": int(os.getenv("SMTP_PORT", "587") or 587),
            "username": os.getenv("SMTP_USERNAME", ""),
            "password": os.getenv("SMTP_PASSWORD", ""),
            "from_email": os.getenv("SMTP_FROM_EMAIL", "")
                          or os.getenv("SMTP_USERNAME", ""),
            "account": "(global .env)",
        }

    try:
        port = int(str(chosen.get("port") or 587))
    except ValueError:
        port = 587

    return {
        "host": chosen.get("host") or os.getenv("SMTP_HOST", ""),
        "port": port,
        "username": chosen.get("username") or "",
        "password": os.getenv(chosen["secret_key"], ""),
        "from_email": chosen.get("from_email") or chosen.get("username") or "",
        "account": chosen["id"],
    }


def resolve_whatsapp_account(account_id: str | None,
                             system: str | None = None) -> dict:
    accounts = load_accounts()["whatsapp"]

    chosen = None
    if account_id:
        chosen = next((a for a in accounts if a["id"] == account_id), None)
        if chosen is None:
            log.warning(f"WhatsApp account {account_id!r} not found; "
                        f"falling back to the global sender.")
    if chosen is None and system:
        chosen = next((a for a in accounts if system in (a.get("systems") or [])), None)

    if chosen is None:
        return {
            "mode": os.getenv("WHATSAPP_MODE", "cloud_api"),
            "phone_number_id": os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
            "token": os.getenv("WHATSAPP_ACCESS_TOKEN", ""),
            "webhook_url": os.getenv("WHATSAPP_WEBHOOK_URL", ""),
            "account": "(global .env)",
        }

    return {
        "mode": chosen.get("mode") or "cloud_api",
        "phone_number_id": chosen.get("phone_number_id") or "",
        "token": os.getenv(chosen["secret_key"], ""),
        "webhook_url": chosen.get("webhook_url") or "",
        "account": chosen["id"],
    }
