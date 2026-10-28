"""
One gate for everything the dashboard writes into .env or uses to name a system.

WHY THIS EXISTS
.env is line-oriented. A value typed into a UI field that contains a newline
("secret\\nSAPLOGON_EXE_PATH=C:\\evil.exe") used to be written verbatim, and
the second line became a real variable -- one the GUI launcher later
executes. Every writer (config_loader._write_env_secret,
delivery_settings._rewrite_env and through it delivery_accounts,
secrets_store and live_settings) now goes through validate_env_pair(), so a
new writer cannot forget the check.

RULES
    key    ^[A-Z][A-Z0-9_]{0,63}$
    value  no CR, LF or NUL; written single-quoted (python-dotenv reads a
           single-quoted value literally: no escapes, no ${VAR} expansion)
    system name  [A-Za-z0-9 _.-]{1,64}
"""
from __future__ import annotations

import os
import re

ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SYSTEM_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,64}$")
_FORBIDDEN = ("\r", "\n", "\x00")
MAX_VALUE_LEN = 4096
# Values made only of these are written as-is; anything else is quoted.
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_./:@,+\-]+")


def validate_env_key(key) -> str:
    text = str(key if key is not None else "")
    if any(c in text for c in _FORBIDDEN) or not ENV_KEY_RE.fullmatch(text):
        raise ValueError(
            "Invalid setting name: use upper-case letters, digits and underscore "
            "only, starting with a letter, at most 64 characters.")
    return text


def validate_env_value(value) -> str:
    text = "" if value is None else str(value)
    if any(c in text for c in _FORBIDDEN):
        raise ValueError("The value must be a single line (no line breaks or NUL characters).")
    if len(text) > MAX_VALUE_LEN:
        raise ValueError(f"The value is too long (at most {MAX_VALUE_LEN} characters).")
    return text


def validate_env_pair(key, value) -> tuple[str, str]:
    return validate_env_key(key), validate_env_value(value)


def quote_env_value(value) -> str:
    """
    A .env value python-dotenv reads back exactly.

    Already-quoted values (what secrets_store._quote produces) are passed
    through unchanged so they are not double-quoted. Empty stays empty.
    """
    text = validate_env_value(value)
    if text == "" or _PLAIN_VALUE.fullmatch(text):
        # Nothing dotenv (or a plain KEY=VALUE reader) could misread: keep
        # the file format the other tools and scripts already expect.
        return text
    if "${" in text:
        # python-dotenv 1.x expands ${VAR} even inside single quotes, so such
        # a value would silently read back as something else.
        raise ValueError("A value containing '${' cannot be stored in .env safely. "
                         "Store it in the credential store instead: python scripts/set_secret.py <NAME>")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        inner = text[1:-1]
        if text[0] == "'" and "'" not in inner:
            return text
        if text[0] == '"' and not re.search(r'(?<!\\)"', inner):
            return text
    if "'" not in text:
        return "'" + text + "'"
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def validate_system_name(name) -> str:
    text = str(name if name is not None else "").strip()
    if not SYSTEM_NAME_RE.fullmatch(text):
        raise ValueError("System name must be 1-64 characters: letters, digits, space, "
                         "dot, dash or underscore.")
    return text


def env_prefix_for_system(name) -> str:
    """The .env variable prefix derived from a (validated) system name."""
    text = validate_system_name(name)
    prefix = re.sub(r"[^A-Z0-9]", "_", text.upper())
    if not prefix[:1].isalpha():
        prefix = "SYS_" + prefix
    return prefix


def backup_env(env_path: str, keep: int | None = None) -> str | None:
    """
    One backup of .env per day, kept OUT of the project folder in
    runtime/env_backups (IBO_RUNTIME_DIR), and only the last `keep`
    (IBO_ENV_BACKUPS_KEEP, default 7). Before, a full plaintext copy of every
    credential was left beside .env every day (.env.bak-YYYYMMDD) and never
    removed. Returns the backup path, or None when there was nothing to copy.
    """
    import shutil
    from datetime import datetime
    if not os.path.isfile(env_path):
        return None
    if keep is None:
        try:
            keep = max(1, int(os.getenv("IBO_ENV_BACKUPS_KEEP", "7") or 7))
        except ValueError:
            keep = 7
    base = os.environ.get("IBO_RUNTIME_DIR") or os.path.join(os.path.dirname(os.path.abspath(env_path)), "runtime")
    bdir = os.path.join(base, "env_backups")
    os.makedirs(bdir, exist_ok=True)
    dest = os.path.join(bdir, f"env-{datetime.now():%Y%m%d}.bak")
    if not os.path.isfile(dest):
        shutil.copy2(env_path, dest)
    olds = sorted(f for f in os.listdir(bdir) if f.startswith("env-") and f.endswith(".bak"))
    for old in olds[:-keep]:
        try:
            os.remove(os.path.join(bdir, old))
        except OSError:
            pass
    return dest
