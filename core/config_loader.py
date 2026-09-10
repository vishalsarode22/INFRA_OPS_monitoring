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
        return os.getenv(full.group(1))

    return _ENV_PATTERN.sub(
        lambda match: os.getenv(match.group(1), match.group(0)),
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
    resolved = {}
    for key, value in system.items():
        if isinstance(value, dict):
            resolved[key] = {k: _resolve_env_value(v) for k, v in value.items()}
        else:
            resolved[key] = _resolve_env_value(value)
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


def get_smtp_config(system_cfg: dict = None) -> dict:
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

    def pick(system_key: str, env_key: str, default=None):
        value = cfg.get(system_key)
        if value not in (None, "", []):
            return value
        return os.getenv(env_key, default)

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
        raise ValueError(
            f"Missing SMTP configuration{where}: {', '.join(missing)}. "
            f"Set them in .env, or per system in config/systems.yaml."
        )

    if not to_emails:
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
    }


def get_monitoring_tasks() -> list[dict]:
    """Load configured SAP monitoring T-codes."""
    path = os.path.join(CONFIG_DIR, "monitoring_tasks.yaml")

    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    return data.get("tasks", [])


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
    global _systems_cache
    path = os.path.join(CONFIG_DIR, "systems.yaml")

    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime, stat.st_size)
    except OSError:
        stamp = None

    if stamp is not None:
        with _systems_cache_lock:
            hit = _systems_cache
        if hit is not None and (hit[0], hit[1]) == stamp:
            # Copy on the way out. Callers mutate the dicts they get back
            # (read_live writes into cfg), and a shared cache handing out the
            # same objects would let one system's poll corrupt another's.
            return [_copy.deepcopy(s) for s in hit[2]]

    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    systems = [_resolve_system(system) for system in data.get("systems", [])]

    if stamp is not None:
        with _systems_cache_lock:
            _systems_cache = (stamp[0], stamp[1], [_copy.deepcopy(s) for s in systems])

    return systems


def invalidate_systems_cache() -> None:
    """Drop the parsed systems.yaml. Call after any write to that file."""
    global _systems_cache
    with _systems_cache_lock:
        _systems_cache = None


def _env_path() -> str:
    return os.path.join(os.path.dirname(CONFIG_DIR), ".env")


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

    path = _env_path()
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()

    replaced = False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"# added by the Add System form")
        lines.append(f"{key}={value}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


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

    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {"systems": []}

    prefix = (name or "SYS").strip().upper().replace("-", "_").replace(" ", "_")

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
            "username": username,
            "password": (existing.get("password") if not password
                         else "${%s_SAP_PASSWORD}" % prefix)
                        or ("${%s_SAP_PASSWORD}" % prefix),
        })

    if has_os_access:
        if ssh_password:
            _write_env_secret(f"{prefix}_SSH_PASSWORD", ssh_password)
        new_system.update({
            "ssh_host": ssh_host,
            "ssh_port": int(ssh_port or 22),
            "ssh_username": ssh_username or "root",
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
            "ashost": rfc_ashost,
            "sysnr": str(rfc_sysnr or sap_instance_nr or "00").zfill(2),
            "client": rfc_client or client,
            "username": rfc_username,
            "password": keep_rfc_password or ("${%s_RFC_PASSWORD}" % prefix),
            "language": language,
        }
        if rfc_saprouter:
            # Stored verbatim: a complete route ends in '/H/' so the RFC
            # library can append the target host, and it may carry a router
            # password in a /W/ segment. Reformatting it breaks the route.
            rfc["saprouter"] = rfc_saprouter
        new_system["rfc"] = rfc

    # Per-system alerting. Only written when something was supplied, so a
    # system with no overrides simply inherits the global .env settings.
    if smtp_password:
        _write_env_secret(f"{prefix}_SMTP_PASSWORD", smtp_password)
    alerting = {
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_username": smtp_username,
        "smtp_password": ("${%s_SMTP_PASSWORD}" % prefix if smtp_password
                          else existing.get("smtp_password", "")),
        "sender_email": sender_email,
        "alert_receivers": alert_receivers,
        "alert_receivers_cc": alert_receivers_cc,
    }
    new_system.update({k: v for k, v in alerting.items() if v})

    # Replace an existing entry of the same name rather than duplicating it.
    systems = data.setdefault("systems", [])
    for i, existing in enumerate(systems):
        if str(existing.get("name", "")).strip().lower() == str(name).strip().lower():
            systems[i] = new_system
            break
    else:
        systems.append(new_system)

    with open(path, "w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, default_flow_style=False, sort_keys=False)

    invalidate_systems_cache()


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

    with open(path, "w", encoding="utf-8") as file:
        yaml.safe_dump(
            data,
            file,
            default_flow_style=False,
            sort_keys=False,
        )

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

    with open(path, "w", encoding="utf-8") as file:
        yaml.safe_dump(
            data,
            file,
            default_flow_style=False,
        )


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
    masked = []

    for system in get_systems():
        item = dict(system)

        if "password" in item:
            item["password"] = "\u2022" * 8
        if item.get("ssh_password"):
            item["ssh_password"] = "\u2022" * 8

        if isinstance(item.get("rfc"), dict):
            rfc = dict(item["rfc"])
            if rfc.get("password"):
                rfc["password"] = "\u2022" * 8
            # A SAProuter route can carry a router password as /W/<secret>/.
            if rfc.get("saprouter"):
                rfc["saprouter"] = re.sub(
                    r"(/W/)[^/]+", r"\1" + "\u2022" * 8, str(rfc["saprouter"])
                )
            item["rfc"] = rfc

        masked.append(item)

    return masked

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