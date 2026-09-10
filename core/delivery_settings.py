"""
Delivery settings -- who is written to, and the credentials used to write.

TWO KINDS OF SETTING, STORED DIFFERENTLY ON PURPOSE

Addresses and phone numbers are configuration. They belong in the profile,
they are edited freely, and they are shown in full: knowing who gets the
alert is the point.

Passwords, tokens and app passwords are secrets. They are written to .env and
NEVER read back to the browser. The UI shows "configured" or "not configured"
and nothing else.

This follows the rule this project already made for systems.yaml: secrets by
${VAR} reference only, values in .env, and a blank field on edit means keep
rather than clear. It exists because a password that round-trips through a
browser ends up in the DOM, in a screenshot, in a bug report, and eventually
in a commit -- which is exactly how ten credentials for four systems came to
be sitting in this project's git history.

WRITING TO .env
save_secrets() rewrites only the keys it is given, preserving every other
line, comment and blank line in the file. A settings save must not quietly
drop an unrelated variable.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from datetime import datetime

from utils.paths import BASE_DIR
from utils.logger import get_logger

log = get_logger(__name__, "delivery_settings")

ENV_PATH = os.path.join(BASE_DIR, ".env")

_lock = threading.Lock()

# key -> (label, group). Only these may be written from the UI: an endpoint
# that can set any environment variable can set IBO_API_TOKEN and lock
# everyone out, or point a system at a different host.
WRITABLE_SECRETS = {
    "SMTP_PASSWORD": ("SMTP password / app password", "email"),
    "WHATSAPP_ACCESS_TOKEN": ("WhatsApp access token", "whatsapp"),
}

# Non-secret settings that also live in .env. Shown in full.
WRITABLE_PLAIN = {
    "SMTP_HOST": ("SMTP host", "email"),
    "SMTP_PORT": ("SMTP port", "email"),
    "SMTP_USERNAME": ("SMTP username", "email"),
    "SMTP_FROM_EMAIL": ("Sender address", "email"),
    "WHATSAPP_ENABLED": ("WhatsApp enabled", "whatsapp"),
    "WHATSAPP_MODE": ("WhatsApp mode", "whatsapp"),
    "WHATSAPP_PHONE_NUMBER_ID": ("WhatsApp phone number id", "whatsapp"),
    "WHATSAPP_WEBHOOK_URL": ("Webhook URL", "whatsapp"),
}


def read_settings() -> dict:
    """
    Current delivery settings for the UI.

    Secrets are reported as configured or not -- never their value, not even
    partially. A masked password is still a length hint and still ends up in
    a screenshot.
    """
    plain = {key: os.getenv(key, "") for key in WRITABLE_PLAIN}
    secrets = {key: bool(os.getenv(key, "").strip()) for key in WRITABLE_SECRETS}

    return {
        "plain": plain,
        "secrets_configured": secrets,
        "labels": {**{k: v[0] for k, v in WRITABLE_PLAIN.items()},
                   **{k: v[0] for k, v in WRITABLE_SECRETS.items()}},
        "groups": {**{k: v[1] for k, v in WRITABLE_PLAIN.items()},
                   **{k: v[1] for k, v in WRITABLE_SECRETS.items()}},
        "env_path": ENV_PATH,
        "env_exists": os.path.isfile(ENV_PATH),
    }


def _rewrite_env(updates: dict[str, str]) -> None:
    """
    Update the given keys in .env, leaving everything else exactly as it was.

    Rewriting the file wholesale would drop the comments and the dozens of
    system credentials already in it, so existing lines are edited in place
    and only genuinely new keys are appended.
    """
    lines: list[str] = []
    if os.path.isfile(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        # One backup per day: enough to recover a bad edit, without a new
        # copy of every credential in the project on every save.
        backup = f"{ENV_PATH}.bak-{datetime.now():%Y%m%d}"
        if not os.path.isfile(backup):
            shutil.copy2(ENV_PATH, backup)

    remaining = dict(updates)
    out: list[str] = []

    for line in lines:
        match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$", line)
        if match and match.group(2) in remaining:
            key = match.group(2)
            out.append(f"{match.group(1)}{key}{match.group(3)}{remaining.pop(key)}")
        else:
            out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Added from the Delivery settings page")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    with open(ENV_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out) + "\n")


def save_settings(plain: dict | None = None,
                  secrets: dict | None = None) -> dict:
    """
    Write delivery settings.

    A blank or absent secret means KEEP the existing value, never clear it.
    The UI cannot show the current value, so it also cannot resubmit it, and
    treating blank as "clear" would wipe a working password the first time
    someone saved an unrelated change on the same page.

    To deliberately remove a secret, pass the literal string "__CLEAR__".
    """
    updates: dict[str, str] = {}
    rejected: list[str] = []

    for key, value in (plain or {}).items():
        if key not in WRITABLE_PLAIN:
            rejected.append(key)
            continue
        updates[key] = str(value).strip()

    for key, value in (secrets or {}).items():
        if key not in WRITABLE_SECRETS:
            rejected.append(key)
            continue
        text = str(value or "").strip()
        if not text:
            continue                      # blank = keep
        updates[key] = "" if text == "__CLEAR__" else text

    if rejected:
        # Named rather than ignored. An endpoint that silently drops unknown
        # keys makes a typo look like a successful save.
        raise ValueError(f"Not settable from this page: {sorted(set(rejected))}. "
                         f"Allowed: {sorted(set(WRITABLE_PLAIN) | set(WRITABLE_SECRETS))}")

    if not updates:
        return read_settings()

    with _lock:
        _rewrite_env(updates)

    # Reflect immediately in this process; .env is otherwise read at startup
    # only, and a saved setting that needs a restart to take effect looks
    # like a setting that did not save.
    for key, value in updates.items():
        os.environ[key] = value

    log.info(f"Delivery settings updated: {sorted(updates)}")
    return read_settings()


def normalise_recipients(raw) -> list[str]:
    """Split a comma, semicolon or newline separated list into clean entries."""
    if isinstance(raw, (list, tuple)):
        items = [str(r) for r in raw]
    else:
        items = re.split(r"[,;\n]", str(raw or ""))
    return [i.strip() for i in items if i.strip()]


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_recipients(emails=None, phones=None) -> list[str]:
    """
    Returns a list of problems, empty when everything is usable.

    Checked on save rather than at send time: an address typed wrongly today
    fails silently at 3am, when the alert it was meant to carry is the reason
    anyone is awake.
    """
    problems = []

    for address in normalise_recipients(emails):
        if not _EMAIL.match(address):
            problems.append(f"{address} is not a valid email address")

    for number in normalise_recipients(phones):
        digits = re.sub(r"[^\d]", "", number)
        if number.startswith("+") or not digits:
            # WhatsApp Cloud API wants country code and digits only, no plus,
            # no spaces. A number in the wrong shape is accepted by the API
            # and then simply never delivers.
            problems.append(f"{number}: use country code and digits only, "
                            f"e.g. 919812345678")
        elif len(digits) < 10 or len(digits) > 15:
            problems.append(f"{number}: expected 10-15 digits including "
                            f"country code")

    return problems
