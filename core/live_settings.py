"""
Settings page: switch features on and off and change the alert mail lists
without editing .env by hand or restarting the dashboard.

ONLY THE KEYS LISTED HERE can be changed from the page. An endpoint that can
set any variable could set IBO_API_TOKEN or point a system at another host.

How a change takes effect
  1. written to .env (core/delivery_settings._rewrite_env: only that line is
     changed, every other line and comment kept, a daily backup .env.bak-*,
     written to a temp file and swapped in);
  2. set in this process at once (os.environ) -- every listed key is read at
     the moment it is used, so no restart is needed;
  3. other processes of this dashboard (RFC worker processes, the jobs and
     collector services) pick it up within a minute: they call sync(),
     which re-reads .env when the file changed;
  4. recorded on the Timeline (IBOPS, category "config") with who changed
     what, old value -> new value.

Each dashboard has its own .env: the Windows and the Ubuntu dashboard are
set separately.
"""
from __future__ import annotations

import os
import re
import threading

from utils.logger import get_logger

log = get_logger(__name__)

# key: (label, what it does, values for on/off, default when not set)
FEATURES: dict[str, tuple[str, str, tuple[str, str], bool]] = {
    "IBO_ENABLE_EMAIL": ("E-mail reports and sweep mails",
                         "Sweep reports and sweep-failure mails. Off: sweeps still run, nothing is mailed from them.",
                         ("1", "0"), False),
    "IBO_REACH_ALERTS": ("SYSTEM DOWN / VPN / credentials alerts",
                         "Mails when a system cannot be reached, a VPN drops or a logon is refused.",
                         ("1", "0"), True),
    "IBO_EXTRA_CHECKS": ("Extra SAP checks",
                         "Certificates, licence, expected and long-running jobs, users, RFC destinations, "
                         "spool, transports, HANA -- every 30 minutes.", ("1", "0"), True),
    "IBO_CHECK_ALERTS": ("Mails for extra-check problems",
                         "Mail Basis when an extra check is WARNING or CRITICAL (once per problem per 12 h).",
                         ("1", "0"), True),
    "IBO_ERROR_MAIL": ("Mail on new IBOPS errors",
                       "Mail Basis the first time a new kind of internal error appears (Errors page).",
                       ("1", "0"), True),
    "IBO_DIGEST": ("Weekly digest", "Monday morning summary mail.", ("1", "0"), True),
    "IBO_SYNTHETIC": ("Synthetic logon test",
                      "A timed test logon every 10 minutes per system. Each run is a logon in the SAP "
                      "security audit log.", ("1", "0"), False),
    "WHATSAPP_ENABLED": ("WhatsApp", "Reports and alerts to WhatsApp (needs the WhatsApp settings).",
                         ("true", "false"), False),
    "NOTIFY_JOB_OWNERS": ("Mail job owners", "Tell whoever scheduled a failed or long job.",
                          ("true", "false"), False),
    "NOTIFY_DUMP_OWNERS": ("Mail dump owners", "Tell the user whose program dumped repeatedly.",
                           ("true", "false"), False),
    "IBO_ENABLE_AI": ("AI analysis", "AI explanation of problems in reports and Ask.", ("1", "0"), False),
    "IBO_ENABLE_RCA": ("AI root-cause sweeps", "Diagnostic sweep when response time is too high.",
                       ("1", "0"), False),
    "IBO_ACTIONS_ENABLED": ("One-click actions",
                            "Lets people restart jobs, release locks, unlock users, cancel jobs from the "
                            "dashboard. These CHANGE the SAP system.", ("1", "0"), False),
    "IBO_ACTIONS_TWO_PERSON": ("Second person must approve actions",
                               "Every one-click action needs another person's approval.", ("1", "0"), False),
}

MAIL_LISTS: dict[str, tuple[str, str]] = {
    "IBO_BASIS_EMAILS": ("Basis team",
                         "First alerts, VPN and credential problems, check mails, errors. Whoever is on call "
                         "(On call page) and per-system basis_emails come first."),
    "ALERT_MANAGER_EMAILS": ("Basis manager",
                             "SYSTEM DOWN, repeated breaches, escalation, weekly digest."),
    "ALERT_TO_EMAILS": ("Report recipients",
                        "Sweep reports. Also used for Basis alerts when the Basis team list is empty. "
                        "Per-system alert_receivers in systems.yaml come first."),
    "ALERT_CC_EMAILS": ("Copy (CC) on reports", "Copied on sweep reports."),
}

# No quotes, spaces or "=": the value is written unquoted into .env.
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_lock = threading.Lock()
_seen = {"mtime": None}


# How each key is read where it is used, so the switch shows what the code does.
_STRICT_TRUE = {"WHATSAPP_ENABLED", "NOTIFY_JOB_OWNERS", "NOTIFY_DUMP_OWNERS"}     # only "true" is on
_OFF_WORDS = ("0", "false", "no", "off")


def _on(key: str) -> bool:
    default = FEATURES[key][3]
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    if key in _STRICT_TRUE:
        return raw == "true"
    if key == "IBO_ENABLE_RCA":
        return raw in ("1", "true", "yes")
    if default:                       # default-on keys: anything but an "off" word is on
        return raw not in _OFF_WORDS
    return raw in ("1", "true", "yes", "on")


def _emails(raw) -> list[str]:
    return [x for x in re.split(r"[,;\s]+", str(raw or "")) if x]


def read() -> dict:
    sync()
    return {
        "features": [{"key": k, "label": v[0], "help": v[1], "on": _on(k),
                      "set": bool((os.getenv(k) or "").strip()), "default": v[3]} for k, v in FEATURES.items()],
        "mail_lists": [{"key": k, "label": v[0], "help": v[1], "emails": _emails(os.getenv(k))}
                       for k, v in MAIL_LISTS.items()],
    }


def _validate(features: dict, mail_lists: dict) -> tuple[dict[str, str], list[str]]:
    updates, errors = {}, []
    for key, value in (features or {}).items():
        if key not in FEATURES:
            errors.append(f"{key} cannot be changed here")
            continue
        if not isinstance(value, bool):
            errors.append(f"{key}: expected on or off")
            continue
        updates[key] = FEATURES[key][2][0] if value else FEATURES[key][2][1]
    for key, value in (mail_lists or {}).items():
        if key not in MAIL_LISTS:
            errors.append(f"{key} cannot be changed here")
            continue
        items = value if isinstance(value, list) else _emails(value)
        items = [str(x).strip() for x in items if str(x).strip()]
        bad = [x for x in items if not _EMAIL.match(x) or len(x) > 120]
        if bad:
            errors.append(f"{MAIL_LISTS[key][0]}: not an e-mail address: {', '.join(bad[:3])}")
            continue
        if len(items) > 30:
            errors.append(f"{MAIL_LISTS[key][0]}: at most 30 addresses")
            continue
        seen, uniq = set(), []
        for x in items:
            if x.lower() not in seen:
                seen.add(x.lower())
                uniq.append(x)
        updates[key] = ",".join(uniq)
    return updates, errors


def save(features: dict | None = None, mail_lists: dict | None = None, by: str = "") -> dict:
    """Validate, write .env, apply here, log. Raises ValueError (nothing written) on any invalid value."""
    from core import delivery_settings, timeline
    updates, errors = _validate(features or {}, mail_lists or {})
    if errors:
        raise ValueError("; ".join(errors))
    changed = {k: v for k, v in updates.items() if (os.getenv(k) or "") != v}
    if not changed:
        return {"changed": []}
    final = lambda k: changed[k] if k in changed else (os.getenv(k) or "")  # noqa: E731
    if ({"IBO_BASIS_EMAILS", "ALERT_TO_EMAILS"} & set(changed)) and \
            not _emails(final("IBO_BASIS_EMAILS")) and not _emails(final("ALERT_TO_EMAILS")):
        raise ValueError("The Basis team list and the report list cannot both be empty -- Basis alerts "
                         "would reach nobody.")
    with _lock, delivery_settings._lock:
        before = {k: os.getenv(k) or "" for k in changed}
        try:
            delivery_settings._rewrite_env(changed)
        except OSError as exc:
            raise ValueError(f"Could not write .env ({exc.strerror or exc}). Nothing was changed -- try again; "
                             f"if it repeats, check that .env is not open in another program.")
        for k, v in changed.items():
            os.environ[k] = v
        try:
            _seen["mtime"] = os.path.getmtime(delivery_settings.ENV_PATH)
        except OSError:
            pass
    lines = []
    for k, v in changed.items():
        if k in FEATURES:
            lines.append(f"{FEATURES[k][0]}: {'on' if v == FEATURES[k][2][0] else 'off'}")
        else:
            lines.append(f"{MAIL_LISTS[k][0]}: {before[k] or '(empty)'} -> {v or '(empty)'}")
    try:
        timeline.add("IBOPS", "config", "Settings changed: " + "; ".join(lines)[:180], "\n".join(lines),
                     actor=by or timeline.actor_name())
    except Exception:
        pass
    log.info(f"settings changed by {by or '?'}: {'; '.join(lines)}")
    return {"changed": list(changed), "summary": lines}


def sync() -> None:
    """Pick up a change another process wrote to .env (only the keys this page manages). Cheap."""
    if os.getenv("IBO_SETTINGS_SYNC", "1").strip().lower() in ("0", "false", "no", "off"):
        return              # tests: never pull the machine's real .env into a test run
    try:
        from core.delivery_settings import ENV_PATH
        mtime = os.path.getmtime(ENV_PATH)
    except OSError:
        return
    if _seen["mtime"] == mtime:
        return
    with _lock:             # never interleave with save() in this process
        try:
            mtime = os.path.getmtime(ENV_PATH)
            from dotenv import dotenv_values
            values = dotenv_values(ENV_PATH)
        except Exception:
            return          # try again next time
        keys = list(FEATURES) + list(MAIL_LISTS)
        try:
            from core import secrets_store
            keys += list(secrets_store.catalogue())     # a password changed on the Settings page
        except Exception:
            pass
        for key in keys:
            if key in values and values[key] is not None and os.environ.get(key) != values[key]:
                os.environ[key] = values[key]
        _seen["mtime"] = mtime
