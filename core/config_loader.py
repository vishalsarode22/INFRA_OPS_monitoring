"""
Central configuration and environment loader for SAP BASIS monitoring.

Supports:
- Legacy single-system .env configuration
- Multi-system config/systems.yaml configuration
- System-specific SAP GUI connection names
- Thresholds, SMTP, monitoring tasks and OCR configuration
- Safe system summaries
- Configuration validation
"""

from __future__ import annotations
from core import config_lock as _config_lock  # one lock for systems.yaml / .env writes

import copy as _copy
import os
import re
import threading as _threading

import yaml
from dotenv import load_dotenv

from utils.paths import BASE_DIR


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

CONFIG_DIR = os.path.join(BASE_DIR, "config")


# ---------------------------------------------------------------------------
# Environment / placeholder helpers
# ---------------------------------------------------------------------------

def _secret_from_keyring(name: str):
    """
    Optional second source for ${VAR}: Windows Credential Manager (or the
    OS keychain) through the `keyring` package, service name "infrabeatops".

        python scripts\\set_secret.py QAS_RFC_PASSWORD

    stores a value there, encrypted by the OS for the logged-on account, so
    .env need not hold 40 systems' passwords in plain text. An entry in .env
    still wins when both exist. Silently absent if keyring is not installed.
    """
    try:
        import keyring  # noqa: WPS433
    except ImportError:
        return None
    try:
        return keyring.get_password("infrabeatops", name)
    except Exception:
        return None


def env_or_keyring(name: str, default=None):
    value = os.getenv(name)
    if value not in (None, ""):
        return value
    secret = _secret_from_keyring(name)
    return secret if secret not in (None, "") else default


def _resolve_env_value(value):
    """Resolve ${ENV_VAR} references.

    A complete ${ENV_VAR} reference resolves to the environment value.
    Embedded references are replaced when available and left untouched
    when the environment variable does not exist.
    """
    if not isinstance(value, str):
        return value

    full = _ENV_PATTERN.fullmatch(value)

    if full:
        return env_or_keyring(full.group(1))

    return _ENV_PATTERN.sub(
        lambda match: env_or_keyring(match.group(1), match.group(0)),
        value,
    )


def _resolve_system(system):
    """
    Resolve environment placeholders in one system configuration.

    Resolves one level of nesting so the "rfc:" block used by the RFC
    collector can hold ${VAR} placeholders exactly like the top-level keys.
    Without this its password would be passed to pyrfc as the literal string
    "${TST_RFC_PASSWORD}" and every logon would fail.
    """
    unresolved: list[str] = []

    def walk(value, key):
        if isinstance(value, dict):
            return {k: walk(v, f"{key}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            # ${VAR} inside a list (alert_receivers: ["${CEAT_TO}"]) used to stay literal.
            return [walk(v, key) for v in value]
        out = _resolve_env_value(value)
        if isinstance(value, str) and _ENV_PATTERN.search(value) and (
                out in (None, "") or (isinstance(out, str) and _ENV_PATTERN.search(out))):
            unresolved.append(key)
        return out

    resolved = {key: walk(value, key) for key, value in system.items()}
    if unresolved:
        resolved["_unresolved"] = sorted(set(unresolved))
    return resolved


# Load .env from project root.
load_dotenv()


# ---------------------------------------------------------------------------
# Legacy single-system SAP configuration
# ---------------------------------------------------------------------------

def get_sap_credentials() -> dict:
    """Return legacy single-system SAP credentials from .env."""
    client = os.getenv("SAP_CLIENT")
    username = os.getenv("SAP_USERNAME")
    password = os.getenv("SAP_PASSWORD")
    language = os.getenv("SAP_LANGUAGE", "EN")

    missing = [
        name
        for name, value in [
            ("SAP_CLIENT", client),
            ("SAP_USERNAME", username),
            ("SAP_PASSWORD", password),
        ]
        if not value
    ]

    if missing:
        raise ValueError(
            f"Missing required .env values: {', '.join(missing)}"
        )

    return {
        "client": client,
        "username": username,
        "password": password,
        "language": language,
    }


def get_linux_ssh_credentials() -> dict:
    """Return legacy single-system Linux SSH credentials from .env."""
    host = os.getenv("SSH_HOST")
    port = int(os.getenv("SSH_PORT", "22"))
    username = os.getenv("SSH_USERNAME")
    password = os.getenv("SSH_PASSWORD")

    missing = [
        name
        for name, value in [
            ("SSH_HOST", host),
            ("SSH_USERNAME", username),
            ("SSH_PASSWORD", password),
        ]
        if not value
    ]

    if missing:
        raise ValueError(
            f"Missing required .env values: {', '.join(missing)}"
        )

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }


def get_sap_instance_nr() -> str:
    """Return legacy SAP instance number from .env."""
    nr = os.getenv("SAP_INSTANCE_NR")

    if not nr:
        raise ValueError(
            "Missing required .env value: SAP_INSTANCE_NR"
        )

    return nr


# ---------------------------------------------------------------------------
# YAML configuration
# ---------------------------------------------------------------------------

def get_thresholds() -> dict:
    """Load monitoring thresholds."""
    path = os.path.join(CONFIG_DIR, "thresholds.yaml")

    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def get_smtp_config(system_cfg: dict = None, require_recipients: bool = True) -> dict:
    """
    SMTP configuration, with optional PER-SYSTEM overrides.

    The global .env values are the default. A system may override any of them
    in its systems.yaml entry, which matters when different systems belong to
    different customers or support teams -- a PRD alert should not land in the
    same inbox as a sandbox alert just because they share a mail server.

    Anything the system does not specify falls back to .env, so existing
    single-tenant setups keep working unchanged.
    """
    cfg = system_cfg or {}
    from notifications import graph_mail
    graph = graph_mail.enabled()

    unresolved = set(cfg.get("_unresolved") or [])

    def pick(system_key: str, env_key: str, default=None):
        value = cfg.get(system_key)
        if system_key in unresolved and system_key in ("alert_receivers", "alert_receivers_cc",
                                                        "manager_receivers"):
            # The system names its own list through a ${VAR} that is not set:
            # NEVER fall back to the global list (another customer's inbox).
            from utils.logger import get_logger as _gl
            _gl(__name__, "application").error(f"{cfg.get('name')}: {system_key} uses a ${{VAR}} that is not set in .env -- "
                      f"no mail goes to that list until it is set")
            return []
        if value not in (None, "", []):
            return value
        return env_or_keyring(env_key, default)

    host = pick("smtp_host", "SMTP_HOST")
    port = int(pick("smtp_port", "SMTP_PORT", "587") or 587)
    username = pick("smtp_username", "SMTP_USERNAME")
    password = pick("smtp_password", "SMTP_PASSWORD")
    from_email = pick("sender_email", "ALERT_FROM_EMAIL") or username

    def as_list(value):
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [e.strip() for e in str(value or "").split(",") if e.strip()]

    to_emails = as_list(pick("alert_receivers", "ALERT_TO_EMAILS", ""))
    # A second address list, so an alert can reach both the Basis team and a
    # customer contact without editing the primary list.
    cc_emails = as_list(pick("alert_receivers_cc", "ALERT_CC_EMAILS", ""))
    # Tier-2 list used only when the same metric breaches on consecutive
    # sweeps (core/escalation.py).
    manager_emails = as_list(pick("manager_receivers", "ALERT_MANAGER_EMAILS", ""))

    if graph:
        # Microsoft Graph sends (notifications/graph_mail.py): no SMTP server
        # or password; the sender is the one mailbox the app may send as.
        g = graph_mail.smtp_like_config()
        host, port, username, password, from_email = g["host"], g["port"], g["username"], "", g["from_email"]
        missing = graph_mail.missing_settings()
    else:
        missing = [
            name
            for name, value in [
                ("SMTP_HOST", host),
                ("SMTP_USERNAME", username),
                ("SMTP_PASSWORD", password),
                ("ALERT_FROM_EMAIL", from_email),
            ]
            if not value
        ]

    if missing:
        where = f" for system '{cfg.get('name')}'" if cfg.get("name") else ""
        if graph:
            raise ValueError(f"Microsoft Graph mail is on but .env lacks {', '.join(missing)}{where}.")
        raise ValueError(
            f"Mail is not set up{where}: IBOPS is not using Microsoft Graph (IBO_MAIL_TRANSPORT=graph is "
            f"not active -- add it on its own line in .env and restart the dashboard), and the SMTP "
            f"settings {', '.join(missing)} are not set."
        )

    # Alerts that choose their own recipients (VPN / DOWN / monitor-down go
    # to the Basis and manager lists, core/recipients.py) only need the SMTP
    # server, not the report list -- they pass require_recipients=False.
    if not to_emails and require_recipients:
        raise ValueError(
            f"No alert recipients configured"
            f"{f' for system ' + repr(cfg.get('name')) if cfg.get('name') else ''}. "
            f"Set ALERT_TO_EMAILS in .env or alert_receivers on the system."
        )

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "from_email": from_email,
        "to_emails": to_emails,
        "cc_emails": cc_emails,
        "manager_emails": manager_emails,
    }


# Screen checks added in Oct 2026 (sap_gui/tcode_actions_extra.py). They are
# part of every installation without editing config/monitoring_tasks.yaml (a
# file that may carry local WhatsApp choices); listing one there with another
# action overrides it. Per system: Profile page -> "T-codes for each system".
BUILT_IN_TASKS = [
    {"tcode": "ST03N", "action": "st03n_workload"},
    {"tcode": "SMICM", "action": "smicm_services"},
    {"tcode": "ST02", "action": "st02_tune"},
    {"tcode": "ST04", "action": "st04_db"},
    {"tcode": "ST05", "action": "st05_status"},
    {"tcode": "STMS", "action": "stms_systems"},
    {"tcode": "STRUST", "action": "strust_certs"},
]


def get_monitoring_tasks() -> list[dict]:
    """Load configured SAP monitoring T-codes, then the built-in screen checks not listed there."""
    path = os.path.join(CONFIG_DIR, "monitoring_tasks.yaml")

    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    tasks = list(data.get("tasks", []) or [])
    have = {str(t.get("tcode") or "").strip().upper() for t in tasks}
    return tasks + [dict(t) for t in BUILT_IN_TASKS if t["tcode"] not in have]


# ---------------------------------------------------------------------------
# SAP GUI launch configuration
# ---------------------------------------------------------------------------

def get_launch_config(connection_name: str | None = None) -> dict:
    """Return SAP GUI launch configuration.

    Legacy behavior:
        get_launch_config()

    uses:
        SAPLOGON_EXE_PATH
        SAP_CONNECTION_NAME

    Multi-system behavior:
        get_launch_config("Fuji QAS")

    uses:
        SAPLOGON_EXE_PATH
        supplied system-specific connection name.

    This allows multiple SAP systems to coexist without requiring a
    single global SAP_CONNECTION_NAME in .env.
    """
    exe_path = os.getenv("SAPLOGON_EXE_PATH")

    if not exe_path:
        raise ValueError(
            "Missing SAPLOGON_EXE_PATH in .env"
        )

    # If the multi-system pipeline supplied a connection name,
    # it takes precedence over the legacy global .env value.
    resolved_connection_name = (
        connection_name
        if connection_name
        else os.getenv("SAP_CONNECTION_NAME")
    )

    if not resolved_connection_name:
        raise ValueError(
            "Missing SAP_CONNECTION_NAME in .env "
            "and no system-specific connection_name was supplied"
        )

    return {
        "exe_path": exe_path,
        "connection_name": resolved_connection_name,
    }


def get_ocr_patterns() -> dict:
    """Load OCR extraction patterns."""
    path = os.path.join(CONFIG_DIR, "ocr_patterns.yaml")

    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


# ---------------------------------------------------------------------------
# Multi-system configuration
# ---------------------------------------------------------------------------

_systems_cache: tuple[float, int, list[dict]] | None = None
_systems_cache_lock = _threading.Lock()


def get_systems() -> list[dict]:
    """
    Load and resolve all configured SAP systems.

    Cached against the file's (mtime, size). This is called on essentially
    every request -- /api/live, /api/live/{name}, /api/overview and
    /api/status each call it, and the wall polls several of those -- so it
    was re-opening and re-parsing YAML, then re-resolving every ${VAR}
    reference, dozens of times a minute. On Windows with Defender watching
    the config directory that is not free.

    Keyed on mtime AND size because a same-second edit that happens to keep
    the mtime can still change the length; the pair is what add_system() and
    a hand edit in Notepad both move. Anything that writes the file calls
    invalidate_systems_cache() as well, so this is a backstop, not the only
    correctness mechanism.
    """
    global _systems_cache, _systems_good
    path = os.path.join(CONFIG_DIR, "systems.yaml")

    try:
        stat = os.stat(path)
        # A password changed on the Settings page lives in .env or in the
        # Windows credential store, not in systems.yaml; .env's time and the
        # marker secrets_store touches make every process (RFC workers
        # included) resolve the ${VAR}s again instead of keeping the old,
        # or missing, password until a restart.
        stamp = (stat.st_mtime, stat.st_size, _mtime(_env_path()), _mtime(secrets_marker()))
    except OSError:
        stamp = None

    if stamp is not None:
        with _systems_cache_lock:
            hit = _systems_cache
        if hit is not None and hit[0] == stamp:
            # Copy on the way out. Callers mutate the dicts they get back
            # (read_live writes into cfg), and a shared cache handing out the
            # same objects would let one system's poll corrupt another's.
            return _scoped([_copy.deepcopy(s) for s in hit[2]])

    # A malformed systems.yaml (a stray tab, an unclosed quote) used to raise
    # a ScannerError out of here: every API answered 500 and the scheduler
    # thread died. Keep serving the LAST GOOD parse instead, say so loudly
    # in the log, and expose the problem through config_error().
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}
        if not isinstance(data, dict) or not isinstance(data.get("systems", []) or [], list):
            raise ValueError("systems.yaml must be a mapping with a 'systems:' list")
    except FileNotFoundError:
        raise
    except Exception as exc:  # noqa: BLE001 -- yaml.YAMLError, ValueError, OSError
        _note_config_error(path, exc)
        with _systems_cache_lock:
            good = _systems_good
        return _scoped([_copy.deepcopy(s) for s in (good or [])])

    # .env changed since the last parse: reload the ${VAR}s systems.yaml uses.
    _reload_env_if_changed(data)
    try:
        # Pick up values another process wrote to .env (Settings page) first.
        from core import live_settings
        live_settings.sync()
    except Exception:  # noqa: BLE001
        pass
    systems = [_resolve_system(system) for system in data.get("systems", []) or []]

    _clear_config_error(path)
    with _systems_cache_lock:
        _systems_good = [_copy.deepcopy(s) for s in systems]
        if stamp is not None:
            _systems_cache = (stamp, None, [_copy.deepcopy(s) for s in systems])

    return _scoped(systems)


# Last successfully parsed systems.yaml, served while the file is broken.
_systems_good: list[dict] | None = None
_config_errors: dict[str, dict] = {}
_env_seen = {"mtime": None}


def _note_config_error(path: str, exc: Exception) -> None:
    from datetime import datetime as _dt
    text = f"{type(exc).__name__}: {exc}"
    # One line of the parser's message is enough; never the file content.
    text = text.splitlines()[0][:300] if text else type(exc).__name__
    first = path not in _config_errors or _config_errors[path].get("error") != text
    _config_errors[path] = {"file": os.path.basename(path), "error": text,
                            "since": (_config_errors.get(path) or {}).get("since")
                            or _dt.now().isoformat(timespec="seconds"),
                            "serving": "last good copy" if _systems_good is not None else "no systems"}
    if first:
        from utils.logger import get_logger
        get_logger(__name__, "application").error(
            f"CONFIG ERROR: {os.path.basename(path)} cannot be parsed ({text}). Serving the "
            f"{'last good copy' if _systems_good is not None else 'empty system list'} until it "
            f"is fixed.")


def _clear_config_error(path: str) -> None:
    if _config_errors.pop(path, None) is not None:
        from utils.logger import get_logger
        get_logger(__name__, "application").warning(f"{os.path.basename(path)} parses again -- config error cleared.")


def config_error() -> dict | None:
    """The current configuration parse error (systems.yaml), or None."""
    path = os.path.join(CONFIG_DIR, "systems.yaml")
    err = _config_errors.get(path)
    return dict(err) if err else None


def _placeholder_names(data) -> set[str]:
    names: set[str] = set()

    def walk(value):
        if isinstance(value, str):
            names.update(_ENV_PATTERN.findall(value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(data)
    return names


def _reload_env_if_changed(data) -> None:
    """
    .env was loaded once at import, so a host or password changed there
    needed a restart. When .env's mtime moves, re-read it and OVERRIDE the
    process environment for the ${VAR}s systems.yaml references (only
    those: a process-level setting such as IBO_ROLE must not be flipped by
    a file edit). IBO_SETTINGS_SYNC=0 (tests) switches this off.
    """
    if os.getenv("IBO_SETTINGS_SYNC", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    env_path = _env_path()
    mtime = _mtime(env_path)
    if not mtime or _env_seen["mtime"] == mtime:
        return
    if _env_seen["mtime"] is None:
        # First look: load_dotenv() at import already read this version.
        _env_seen["mtime"] = mtime
        return
    try:
        from dotenv import dotenv_values
        values = dotenv_values(env_path)
    except Exception:  # noqa: BLE001 -- try again on the next parse
        return
    for key in _placeholder_names(data):
        value = values.get(key)
        if value is not None and os.environ.get(key) != value:
            os.environ[key] = value
    _env_seen["mtime"] = mtime


def _scoped(systems: list[dict]) -> list[dict]:
    """
    Customer scope, enforced here so EVERY caller is covered.

    A logged-in dashboard user with a customer scope (core/auth.py) sees
    only the systems whose `customer:` is in that scope. Outside a request
    (the collector, main.py, the jobs runner) there is no principal and
    nothing is filtered.
    """
    try:
        from core.auth import filter_systems
        return filter_systems(systems)
    except Exception:
        return systems


def get_all_systems() -> list[dict]:
    """Every system regardless of the request's customer scope (admin/system use)."""
    from core.auth import current_user
    token = current_user.set(None)
    try:
        return get_systems()
    finally:
        current_user.reset(token)


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def secrets_marker() -> str:
    """Touched whenever a password / token is changed (core/secrets_store.py)."""
    return os.path.join(CONFIG_DIR, ".secrets_changed")


def secrets_changed_at() -> float:
    return _mtime(secrets_marker())


def invalidate_systems_cache() -> None:
    """Drop the parsed systems.yaml. Call after any write to that file."""
    global _systems_cache
    with _systems_cache_lock:
        _systems_cache = None


def _env_path() -> str:
    return os.path.join(os.path.dirname(CONFIG_DIR), ".env")


@_config_lock.locked
def _write_env_secret(key: str, value: str) -> None:
    """
    Stores a secret in .env, replacing any existing entry for the key.

    Secrets must never be written into systems.yaml. That file is committed,
    screen-shared and pasted into tickets; every existing entry uses ${VAR}
    references precisely so it holds nothing sensitive. The previous version
    of add_system() wrote the password inline, so one use of the "Add System"
    button silently turned a shareable config into a secrets file.
    """
    if not value:
        return

    # CR/LF/NUL in a key or value would add an attacker-chosen line to .env
    # (e.g. SAPLOGON_EXE_PATH, which the launcher executes). Rejected here,
    # and the value is written quoted so dotenv reads it back literally.
    from core.env_safety import validate_env_key, validate_env_value, quote_env_value
    key = validate_env_key(key)
    value = validate_env_value(value)
    stored = quote_env_value(value)

    path = _env_path()
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()

    replaced = False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={stored}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"# added by the Add System form")
        lines.append(f"{key}={stored}")

    from utils.atomic import atomic_write_text
    atomic_write_text(path, "\n".join(lines) + "\n")     # never a half-written .env

    # The running process loaded .env once at import. Without this, a system
    # added from the UI would resolve its ${VAR}s to None until a restart --
    # and the immediate first sweep after "Add System" would log on with
    # an empty password.
    os.environ[key] = value


def _env_ref(prefix: str, suffix: str, value):
    """
    Put a connection detail in .env and return its ${VAR} reference.

    Passwords were already handled this way. Hosts, SAP usernames, saprouter
    strings (which can carry a /W/ router password), SMTP accounts and alert
    addresses were written into systems.yaml as plain text, so the committed
    file still named client IPs, logon users and personal mailboxes.

    A value that is already a ${VAR} reference is kept as it is, so an edit
    that re-submits an unchanged reference does not create a new variable.
    """
    if value is None or str(value).strip() == "":
        return value
    text = str(value).strip()
    if _ENV_PATTERN.fullmatch(text):
        return text
    key = f"{prefix}_{suffix}"
    _write_env_secret(key, text)
    return "${%s}" % key


@_config_lock.locked
def add_system(
    name: str,
    client: str,
    connection_name: str = "",
    username: str = "",
    password: str = "",
    language: str = "EN",
    has_gui_access: bool = True,
    has_os_access: bool = False,
    ssh_host: str = "",
    ssh_port: int = 22,
    ssh_username: str = "",
    ssh_password: str = "",
    sap_instance_nr: str = "",
    sap_system_id: str = "",
    rfc_ashost: str = "",
    rfc_sysnr: str = "",
    rfc_client: str = "",
    rfc_username: str = "",
    rfc_password: str = "",
    rfc_saprouter: str = "",
    smtp_host: str = "",
    smtp_port: str = "",
    smtp_username: str = "",
    smtp_password: str = "",
    sender_email: str = "",
    alert_receivers: str = "",
    alert_receivers_cc: str = "",
    manager_receivers: str = "",
):
    """
    Add a system to config/systems.yaml.

    Writes ${VAR} references and puts the actual secrets in .env.

    The rfc_* parameters matter: without an "rfc" block the RFC collector
    never runs for the system, so it produces no T-code counters, nothing on
    the live wall, and no data at all on hosts with no SAP GUI or OS access.
    A system added without them looks configured but reports nothing.
    """
    path = os.path.join(CONFIG_DIR, "systems.yaml")

    # Validate every input BEFORE anything is written (.env or YAML).
    from core.env_safety import (validate_system_name, validate_env_value,
                                 env_prefix_for_system)
    name = validate_system_name(name)
    for _field, _val in list(locals().items()):
        if isinstance(_val, str):
            try:
                validate_env_value(_val)
            except ValueError as exc:
                raise ValueError(f"{_field}: {exc}") from None

    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {"systems": []}

    prefix = env_prefix_for_system(name)

    # An EDIT never shows the operator the existing passwords -- the API
    # masks them. So a blank password field means "leave it alone", not
    # "erase it". Without this, editing a router string would silently wipe
    # the RFC password and the system would stop collecting.
    existing = next(
        (e for e in data.get("systems", [])
         if str(e.get("name", "")).strip().lower() == str(name).strip().lower()),
        {},
    )
    existing_rfc = existing.get("rfc") or {}

    # Repointing a connection while silently keeping its stored password is
    # a credential-capture primitive: change the RFC/SSH/SMTP host to your
    # own machine, leave the password blank, and the next connection (the
    # immediate monitor_now run) sends the real password there. Any change
    # to where or as whom a credential is used requires that credential to
    # be typed again.
    if existing:
        _require_password_reentry(existing, {
            "client": client, "connection_name": connection_name, "username": username,
            "password": password, "has_gui_access": has_gui_access,
            "has_os_access": has_os_access, "ssh_host": ssh_host, "ssh_port": ssh_port,
            "ssh_username": ssh_username, "ssh_password": ssh_password,
            "rfc_ashost": rfc_ashost,
            "rfc_sysnr": str(rfc_sysnr or sap_instance_nr or "00").zfill(2),
            "rfc_client": rfc_client or client, "rfc_username": rfc_username,
            "rfc_password": rfc_password, "rfc_saprouter": rfc_saprouter,
            "smtp_host": smtp_host, "smtp_port": smtp_port,
            "smtp_username": smtp_username, "smtp_password": smtp_password,
        })

    new_system = {
        "name": name,
        "client": client,
        "language": language,
        "has_gui_access": bool(has_gui_access),
        "has_os_access": bool(has_os_access),
    }
    if sap_system_id:
        new_system["sap_system_id"] = sap_system_id

    if has_gui_access:
        if password:
            _write_env_secret(f"{prefix}_SAP_PASSWORD", password)
        new_system.update({
            "connection_name": connection_name,
            "username": _env_ref(prefix, "SAP_USERNAME", username),
            "password": (existing.get("password") if not password
                         else "${%s_SAP_PASSWORD}" % prefix)
                        or ("${%s_SAP_PASSWORD}" % prefix),
        })

    if has_os_access:
        if ssh_password:
            _write_env_secret(f"{prefix}_SSH_PASSWORD", ssh_password)
        new_system.update({
            "ssh_host": _env_ref(prefix, "SSH_HOST", ssh_host),
            "ssh_port": int(ssh_port or 22),
            "ssh_username": _env_ref(prefix, "SSH_USERNAME", ssh_username or "root"),
            "ssh_password": (existing.get("ssh_password") if not ssh_password
                             else "${%s_SSH_PASSWORD}" % prefix)
                            or ("${%s_SSH_PASSWORD}" % prefix),
            "sap_instance_nr": sap_instance_nr or "00",
        })

    # The RFC block is kept when host and user are present. The password may
    # be blank on an edit, in which case the existing ${VAR} reference is
    # reused -- requiring it would mean re-typing every password to change a
    # router string.
    keep_rfc_password = existing_rfc.get("password") if not rfc_password else None
    if rfc_ashost and rfc_username and (rfc_password or keep_rfc_password):
        if rfc_password:
            _write_env_secret(f"{prefix}_RFC_PASSWORD", rfc_password)
        rfc = {
            "ashost": _env_ref(prefix, "RFC_HOST", rfc_ashost),
            "sysnr": str(rfc_sysnr or sap_instance_nr or "00").zfill(2),
            "client": rfc_client or client,
            "username": _env_ref(prefix, "RFC_USER", rfc_username),
            "password": keep_rfc_password or ("${%s_RFC_PASSWORD}" % prefix),
            "language": language,
        }
        if rfc_saprouter:
            # Stored verbatim: a complete route ends in '/H/' so the RFC
            # library can append the target host, and it may carry a router
            # password in a /W/ segment. Reformatting it breaks the route.
            rfc["saprouter"] = _env_ref(prefix, "SAPROUTER", rfc_saprouter)
        new_system["rfc"] = rfc

    # Per-system alerting. Only written when something was supplied, so a
    # system with no overrides simply inherits the global .env settings.
    if smtp_password:
        _write_env_secret(f"{prefix}_SMTP_PASSWORD", smtp_password)
    alerting = {
        "smtp_host": _env_ref(prefix, "SMTP_HOST", smtp_host),
        "smtp_port": smtp_port,
        "smtp_username": _env_ref(prefix, "SMTP_USERNAME", smtp_username),
        "smtp_password": ("${%s_SMTP_PASSWORD}" % prefix if smtp_password
                          else existing.get("smtp_password", "")),
        "sender_email": _env_ref(prefix, "SENDER_EMAIL", sender_email),
        "alert_receivers": _env_ref(prefix, "ALERT_RECEIVERS", alert_receivers),
        "alert_receivers_cc": _env_ref(prefix, "ALERT_RECEIVERS_CC", alert_receivers_cc),
        "manager_receivers": _env_ref(prefix, "MANAGER_RECEIVERS", manager_receivers),
    }
    new_system.update({k: v for k, v in alerting.items() if v})

    # Replace an existing entry of the same name rather than duplicating it.
    systems = data.setdefault("systems", [])
    for i, existing in enumerate(systems):
        if str(existing.get("name", "")).strip().lower() == str(name).strip().lower():
            # Keys the edit form does not manage survive an edit from the UI.
            # An allowlist, not "everything": a field the form DOES manage and
            # the operator cleared must stay cleared.
            for key in ("customer", "basis_emails", "vpn", "notes"):
                if key in existing and key not in new_system:
                    new_system[key] = existing[key]
            systems[i] = new_system
            break
    else:
        systems.append(new_system)

    from utils.atomic import atomic_write_yaml       # a reader never sees half a file
    atomic_write_yaml(path, data, default_flow_style=False, sort_keys=False, allow_unicode=False)

    invalidate_systems_cache()


class PasswordReentryRequired(ValueError):
    """An edit changed a connection target but did not re-supply its password."""


def _norm_target(value) -> str:
    resolved = _resolve_env_value(value) if isinstance(value, str) else value
    return "" if resolved is None else str(resolved).strip().lower()


def _require_password_reentry(existing: dict, new: dict) -> None:
    ex_rfc = existing.get("rfc") or {}
    groups = []
    # (label, new password field, stored password, enabled?, [(new value, stored value)])
    groups.append(("SAP GUI password", "password", existing.get("password"),
                   bool(new["has_gui_access"]),
                   [(new["connection_name"], existing.get("connection_name")),
                    (new["username"], existing.get("username")),
                    (new["client"], existing.get("client"))]))
    groups.append(("SSH password", "ssh_password", existing.get("ssh_password"),
                   bool(new["has_os_access"]),
                   [(new["ssh_host"], existing.get("ssh_host")),
                    (str(new["ssh_port"] or 22), str(existing.get("ssh_port") or 22)),
                    (new["ssh_username"] or "root", existing.get("ssh_username"))]))
    groups.append(("RFC password", "rfc_password", ex_rfc.get("password"),
                   bool(new["rfc_ashost"] and new["rfc_username"]),
                   [(new["rfc_ashost"], ex_rfc.get("ashost") or ex_rfc.get("mshost")),
                    (new["rfc_sysnr"], str(ex_rfc.get("sysnr") or "00").zfill(2)),
                    (new["rfc_client"], ex_rfc.get("client")),
                    (new["rfc_username"], ex_rfc.get("username") or ex_rfc.get("user")),
                    (new["rfc_saprouter"], ex_rfc.get("saprouter"))]))
    groups.append(("SMTP password", "smtp_password", existing.get("smtp_password"),
                   bool(new["smtp_host"] or new["smtp_username"]),
                   [(new["smtp_host"], existing.get("smtp_host")),
                    (str(new["smtp_port"] or ""), str(existing.get("smtp_port") or "")),
                    (new["smtp_username"], existing.get("smtp_username"))]))
    missing = []
    for label, field, stored, enabled, pairs in groups:
        if not enabled or not stored or new.get(field):
            continue
        if any(_norm_target(a) != _norm_target(b) for a, b in pairs):
            missing.append(label)
    if missing:
        raise PasswordReentryRequired(
            "The connection details changed (host, instance, client, user or router), so the "
            f"stored credential will not be reused. Re-enter: {', '.join(missing)}.")


@_config_lock.locked
def delete_system(name: str) -> bool:
    """Remove a system by name."""
    path = os.path.join(CONFIG_DIR, "systems.yaml")

    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {"systems": []}

    systems = data.get("systems", [])

    remaining = [
        system
        for system in systems
        if system.get("name") != name
    ]

    if len(remaining) == len(systems):
        return False

    data["systems"] = remaining

    from utils.atomic import atomic_write_yaml
    atomic_write_yaml(path, data, default_flow_style=False, sort_keys=False, allow_unicode=False)

    invalidate_systems_cache()
    return True


# ---------------------------------------------------------------------------
# Scheduler configuration
# ---------------------------------------------------------------------------

def get_scheduler_interval_minutes(default: int = 120) -> int:
    """Read scheduler interval from dashboard_settings.yaml."""
    path = os.path.join(
        CONFIG_DIR,
        "dashboard_settings.yaml",
    )

    if not os.path.isfile(path):
        return default

    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    return int(
        data.get(
            "schedule_interval_minutes",
            default,
        )
    )


def set_scheduler_interval_minutes(minutes: int):
    """Set scheduler interval in dashboard_settings.yaml."""
    path = os.path.join(
        CONFIG_DIR,
        "dashboard_settings.yaml",
    )

    data = {}

    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}

    data["schedule_interval_minutes"] = int(minutes)

    from utils.atomic import atomic_write_yaml
    atomic_write_yaml(path, data, default_flow_style=False, sort_keys=True, allow_unicode=False)


# ---------------------------------------------------------------------------
# Safe configuration views
# ---------------------------------------------------------------------------

def get_systems_safe() -> list[dict]:
    """
    Return systems with every secret masked, for API/dashboard responses.

    The nested "rfc" block was added after this function was written, so the
    two top-level checks below never reached it and GET /api/systems returned
    every RFC password in plaintext over an unauthenticated endpoint.
    """
    return [mask_secrets(dict(system)) for system in get_systems()]


# Any key whose name contains one of these (case-insensitive) is a secret,
# at any depth. Name-based on purpose: the old per-field list missed
# smtp_password the day it was added, and would miss the next one too.
_SECRET_KEY_PARTS = ("password", "passwd", "secret", "token", "api_key", "apikey",
                     "credential")
_MASK = "\u2022" * 8


def is_secret_key(key) -> bool:
    k = str(key).lower()
    return any(part in k for part in _SECRET_KEY_PARTS)


def mask_router_string(value):
    """A SAProuter route can carry a router password as /W/<secret>/."""
    if not isinstance(value, str) or "/W/" not in value.upper():
        return value
    return re.sub(r"(/[Ww]/)[^/]*", r"\1" + _MASK, value)


def mask_secrets(node):
    """
    Recursively mask secrets in a config structure (dicts, lists, scalars).

    Secret keys are masked whatever their value -- a ${VAR} reference too:
    it names the variable an attacker would go looking for. Router strings
    anywhere lose their /W/ password segment.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if is_secret_key(k):
                out[k] = _MASK if v not in (None, "") else v
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(node, list):
        return [mask_secrets(v) for v in node]
    if isinstance(node, tuple):
        return tuple(mask_secrets(v) for v in node)
    return mask_router_string(node)

# ---------------------------------------------------------------------------
# Configuration validation
#
# These three functions were accidentally deleted when get_systems_safe() was
# rewritten to mask the nested rfc block -- the edit replaced everything from
# that function to the end of file, and they lived below it. Restored verbatim.
# core/production_health.py imports configuration_summary at module load, so
# losing them broke `import dashboard.app` outright.
# ---------------------------------------------------------------------------

def validate_systems_configuration() -> dict:
    """Validate all configured systems without exposing secrets."""
    try:
        systems = get_systems()
    except Exception:
        return {
            "valid": False,
            "systems_configured": 0,
            "systems_valid": 0,
        }

    valid_count = 0

    for system in systems:
        required = (
            "name",
            "client",
            "username",
            "password",
            "connection_name",
        )

        if not all(system.get(key) for key in required):
            continue

        if system.get("has_os_access"):
            if not all(
                system.get(key)
                for key in (
                    "ssh_host",
                    "ssh_username",
                    "ssh_password",
                )
            ):
                continue

        valid_count += 1

    return {
        "valid": bool(systems) and valid_count == len(systems),
        "systems_configured": len(systems),
        "systems_valid": valid_count,
    }


def validate_configuration(
    include_optional: bool = False,
) -> dict:
    """Validate overall application configuration."""
    systems = validate_systems_configuration()

    provider = (
        os.getenv("AI_PROVIDER") or "mock"
    ).strip().lower()

    invalid = {}

    if provider not in {"mock", "gemini"}:
        invalid["AI_PROVIDER"] = "unsupported"

    for name in ("SMTP_PORT", "SSH_PORT"):
        value = os.getenv(name)

        if value:
            try:
                port = int(value)

                if not 1 <= port <= 65535:
                    invalid[name] = "out_of_range"

            except ValueError:
                invalid[name] = "not_integer"

    if systems["systems_configured"] == 0:
        invalid["systems_configuration"] = "no_valid_systems"

    return {
        "valid": systems["valid"] and not invalid,
        "systems_configured": systems["systems_configured"],
        "systems_valid": systems["systems_valid"],
        "invalid": invalid,
        "ai_provider": {
            "provider": provider,
            "configured": (
                provider != "gemini"
                or bool(os.getenv("GEMINI_API_KEY"))
            ),
        },
    }


def configuration_summary() -> dict:
    """Return a safe, non-secret configuration summary."""
    result = validate_configuration()

    return {
        "valid": result["valid"],
        "systems_configured": result["systems_configured"],
        "systems_valid": result["systems_valid"],
        "invalid": dict(result["invalid"]),
        "ai_provider": dict(result["ai_provider"]),
    }