"""
Add and edit systems and thresholds from the web page, safely.

Today we fixed systems.yaml by hand: a missing password variable and one
wrong indentation stop a system silently. Every save from the Systems and
Thresholds pages now goes through here:

  1. validate     every field, with a sentence per problem (missing RFC
                  password, client not 3 digits, VPN check spelled wrong,
                  ${VAR} that .env does not define ...). Errors block the
                  save; warnings are shown and the save goes ahead.
  2. back up      config/backups/<file>-<timestamp>.yaml (last 30 kept)
  3. write safely to a temporary file, read it back, check it says what
                  was meant, then replace the real file in one step -- a
                  half-written systems.yaml cannot happen
  4. verify       the system resolves (passwords present in .env) and say
                  what is still missing

Secrets still go to .env, never into systems.yaml (config_loader.add_system).
Keys the form does not manage (network, actions, customer, notes, anything
added by hand) are kept on an edit.
"""
from __future__ import annotations
from core import config_lock as _config_lock  # one lock for systems.yaml / .env writes

import copy
import ipaddress
import os
import re
import shutil
import tempfile
from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__)

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-\.]{0,251}[A-Za-z0-9])?$")
_CHECK = re.compile(r"^(forticlient(:\S+)?|openfortivpn:\S+|remote:[^/\s]+/\S+)$", re.I)
MANAGED_EXTRA = ("customer", "network", "basis_emails", "manager_receivers", "live_refresh_seconds", "actions",
                 "notes", "recovery_emails", "down_emails", "access_emails", "alert_timing")


def config_dir() -> str:
    from core.config_loader import CONFIG_DIR
    return CONFIG_DIR


# ---------------------------------------------------------------- backups

def backup(filename: str, keep: int = 30) -> str | None:
    src = os.path.join(config_dir(), filename)
    if not os.path.exists(src):
        return None
    bdir = os.path.join(config_dir(), "backups")
    os.makedirs(bdir, exist_ok=True)
    stem, ext = os.path.splitext(filename)
    # Microseconds: two saves in the same second overwrote each other's backup.
    dest = os.path.join(bdir, f"{stem}-{datetime.now():%Y%m%d-%H%M%S-%f}{ext}")
    shutil.copy2(src, dest)
    olds = sorted(f for f in os.listdir(bdir) if f.startswith(stem + "-") and f.endswith(ext))
    for old in olds[:-keep]:
        try:
            os.remove(os.path.join(bdir, old))
        except OSError:
            pass
    return dest


@_config_lock.locked
def write_text_safely(filename: str, text: str, check=None) -> None:
    """Temp file, optional check(parsed_yaml), then atomic replace (after a backup)."""
    import yaml
    path = os.path.join(config_dir(), filename)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".yaml", dir=config_dir())
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        with open(tmp, "r", encoding="utf-8") as fh:
            parsed = yaml.safe_load(fh)
        if check is not None:
            check(parsed)
        backup(filename)
        from utils.atomic import replace_or_overwrite   # Windows: file in use by readers
        replace_or_overwrite(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_yaml_safely(filename: str, data, check=None) -> None:
    import yaml
    text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    write_text_safely(filename, text, check)


# ------------------------------------------------------------- validation

def _env_missing(value) -> list[str]:
    out = []
    if isinstance(value, dict):
        for v in value.values():
            out += _env_missing(v)
    elif isinstance(value, list):
        for v in value:
            out += _env_missing(v)
    elif isinstance(value, str):
        for name in _ENV_REF.findall(value):
            if not os.getenv(name):
                out.append(name)
    return out


def _host_ok(text: str) -> bool:
    text = str(text or "").strip()
    if not text:
        return False
    if _ENV_REF.fullmatch(text):
        return True
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return bool(_HOST.match(text))


def _emails(text) -> tuple[list[str], list[str]]:
    items = text if isinstance(text, list) else re.split(r"[,;\s]+", str(text or ""))
    good, bad = [], []
    for e in items:
        e = str(e).strip()
        if not e:
            continue
        if _ENV_REF.fullmatch(e):
            good.append(e)
        elif _EMAIL.match(e):
            good.append(e)
        else:
            bad.append(e)
    return good, bad


def existing_entry(name: str) -> dict | None:
    import yaml
    path = os.path.join(config_dir(), "systems.yaml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        return None
    for e in data.get("systems") or []:
        if str(e.get("name", "")).strip().lower() == str(name).strip().lower():
            return e
    return None


def validate_system(form: dict, editing: bool = False) -> tuple[list[str], list[str]]:
    """(errors, warnings) for one Add/Edit System form."""
    errors: list[str] = []
    warnings: list[str] = []
    # Field types first: a list or object where text is expected crashed the
    # checks below with a 500 (black-box test 28.10.2026).
    shaped = {"alert_timing": dict, "actions": list}
    for k, v in (form or {}).items():
        want = shaped.get(k)
        if want is not None:
            if v is not None and not isinstance(v, want):
                errors.append(f"{k}: wrong type.")
            elif k == "actions" and isinstance(v, list) and not all(isinstance(x, str) for x in v):
                errors.append("actions: must be a list of names.")
        elif v is not None and not isinstance(v, (str, bool, int, float)):
            errors.append(f"{k}: must be text.")
    if errors:
        return errors, warnings
    f = {k: (v.strip() if isinstance(v, str) else v) for k, v in (form or {}).items()}
    name = f.get("name") or ""
    if not name:
        errors.append("Name is required.")
    elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _\-]{0,39}", name):
        errors.append("Name: letters, digits, space, _ or -, at most 40 characters.")
    current = existing_entry(name) if name else None
    if current and not editing:
        errors.append(f"A system called '{current.get('name')}' already exists -- use Edit.")
    if editing and not current:
        errors.append(f"There is no system '{name}' to edit.")
    current = current or {}
    cur_rfc = current.get("rfc") or {}

    client = str(f.get("client") or "")
    if not re.fullmatch(r"\d{3}", client):
        errors.append("Client must be 3 digits (e.g. 100).")
    lang = str(f.get("language") or "EN")
    if not re.fullmatch(r"[A-Za-z]{2}", lang):
        errors.append("Language must be 2 letters (EN, DE ...).")

    if f.get("has_gui_access"):
        if not f.get("connection_name"):
            errors.append("SAP GUI access needs the SAP Logon connection name (exactly as in SAP Logon on the VM).")
        if not f.get("username"):
            errors.append("SAP GUI access needs the monitoring user name.")
        if not f.get("password") and not current.get("password"):
            errors.append("SAP GUI access needs a password (it is stored in .env, not in systems.yaml).")

    rfc_any = any(f.get(k) for k in ("rfc_ashost", "rfc_username", "rfc_password", "rfc_sysnr"))
    if rfc_any:
        if not _host_ok(f.get("rfc_ashost")):
            errors.append("RFC host: give an IP address or host name.")
        if not f.get("rfc_username"):
            errors.append("RFC user is required when an RFC host is given.")
        if not f.get("rfc_password") and not cur_rfc.get("password"):
            errors.append("RFC password is required (stored in .env).")
        sysnr = str(f.get("rfc_sysnr") or "00")
        if not re.fullmatch(r"\d{1,2}", sysnr):
            errors.append("Instance number (sysnr) must be 2 digits, e.g. 00.")
        rc = str(f.get("rfc_client") or client)
        if rc and not re.fullmatch(r"\d{3}", rc):
            errors.append("RFC client must be 3 digits.")
    else:
        warnings.append("No RFC details: this system will have no live data on the wall and no counters.")

    if f.get("has_os_access"):
        if not _host_ok(f.get("ssh_host")):
            errors.append("OS access needs an SSH host.")
        try:
            port = int(f.get("ssh_port") or 22)
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("SSH port must be a number between 1 and 65535.")

    for field, label in (("alert_receivers", "Recipients"), ("alert_receivers_cc", "Second list"),
                         ("basis_emails", "Basis e-mails"), ("manager_receivers", "Manager e-mails"),
                         ("recovery_emails", "Back to normal"), ("down_emails", "System down"),
                         ("access_emails", "VPN / logon problems")):
        _, bad = _emails(f.get(field))
        if bad:
            errors.append(f"{label}: not an e-mail address: {', '.join(bad[:5])}")

    net_check = str(f.get("network_check") or "")
    if net_check and not _CHECK.match(net_check):
        errors.append("VPN check must be forticlient[:profile], openfortivpn:<name> or remote:<source>/<vpn>.")
    for probe in re.split(r"[,\s]+", str(f.get("network_probe") or "")):
        if probe and not re.fullmatch(r"[A-Za-z0-9\.\-]+:\d{1,5}", probe):
            errors.append(f"Network probe '{probe}' must be host:port.")
    if net_check.lower().startswith("remote:"):
        try:
            from dashboard.remote_sources import load_sources
            src = net_check.split(":", 1)[1].split("/", 1)[0]
            if not any(s["name"] == src for s in load_sources()):
                warnings.append(f"VPN check names remote source '{src}', which is not in config/remote_sources.yaml.")
        except Exception:
            pass
    if f.get("network_vpn") and not f.get("network_name"):
        warnings.append("Tick 'behind a VPN' together with a network name, so systems on the same VPN share one alert.")

    lrs = f.get("live_refresh_seconds")
    if lrs not in (None, "", 0, "0"):
        try:
            v = int(lrs)
            if not 30 <= v <= 86400:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("Live refresh must be between 30 and 86400 seconds (or empty).")

    acts = f.get("actions") or []
    if isinstance(acts, str):
        acts = [a.strip() for a in acts.split(",") if a.strip()]
    from core.sap_actions import CATALOG
    unknown = [a for a in acts if a not in CATALOG]
    if unknown:
        errors.append(f"Unknown action(s): {', '.join(unknown)}.")

    missing = sorted(set(_env_missing(current))) if current else []
    if missing:
        warnings.append(f"These variables are referenced in systems.yaml but not set in .env: "
                        f"{', '.join(missing)}. Fill the matching fields to set them.")
    return errors, warnings


# --------------------------------------------------------------- saving

@_config_lock.locked
def save_system(form: dict, editing: bool = False) -> dict:
    """Validate, back up, write (via config_loader.add_system), apply extras, verify."""
    from core import config_loader
    from core import timeline
    errors, warnings = validate_system(form, editing)
    if errors:
        raise ValueError(" ".join(errors))
    f = dict(form)
    name = str(f["name"]).strip()
    before = copy.deepcopy(existing_entry(name) or {})
    backup("systems.yaml")
    config_loader.add_system(
        name=name, client=str(f.get("client")), connection_name=f.get("connection_name") or "",
        username=f.get("username") or "", password=f.get("password") or "",
        language=f.get("language") or "EN",
        has_gui_access=bool(f.get("has_gui_access")), has_os_access=bool(f.get("has_os_access")),
        ssh_host=f.get("ssh_host") or "", ssh_port=int(f.get("ssh_port") or 22),
        ssh_username=f.get("ssh_username") or "", ssh_password=f.get("ssh_password") or "",
        sap_instance_nr=f.get("sap_instance_nr") or "", sap_system_id=f.get("sap_system_id") or "",
        rfc_ashost=f.get("rfc_ashost") or "", rfc_sysnr=f.get("rfc_sysnr") or "",
        rfc_client=f.get("rfc_client") or "", rfc_username=f.get("rfc_username") or "",
        rfc_password=f.get("rfc_password") or "", rfc_saprouter=f.get("rfc_saprouter") or "",
        smtp_host=f.get("smtp_host") or "", smtp_port=f.get("smtp_port") or "",
        smtp_username=f.get("smtp_username") or "", smtp_password=f.get("smtp_password") or "",
        sender_email=f.get("sender_email") or "", alert_receivers=f.get("alert_receivers") or "",
        alert_receivers_cc=f.get("alert_receivers_cc") or "",
        manager_receivers=f.get("manager_receivers") or "",
    )
    _apply_extras(name, f, before)
    config_loader.invalidate_systems_cache()
    # Verify the saved entry resolves.
    resolved = next((s for s in config_loader.get_all_systems() if s.get("name") == name), None)
    if resolved is None:
        raise RuntimeError("Saved, but the system could not be read back -- check config/systems.yaml "
                           "(a backup is in config/backups).")
    rfc = resolved.get("rfc") or {}
    if rfc and not rfc.get("password"):
        warnings.append("The RFC password variable is empty in .env -- the system cannot log on until it is set.")
    if resolved.get("has_gui_access") and not resolved.get("username"):
        warnings.append("The SAP GUI user resolves to empty -- sweeps will stop at the logon screen.")
    timeline.add(name, "config", "System settings changed" if editing else "System added",
                 actor=timeline.actor_name())
    return {"saved": True, "warnings": warnings}


def _apply_extras(name: str, f: dict, before: dict) -> None:
    import yaml
    path = os.path.join(config_dir(), "systems.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {"systems": []}
    entry = next((e for e in data.get("systems") or []
                  if str(e.get("name", "")).strip().lower() == name.lower()), None)
    if entry is None:
        return
    # Keys nobody manages from the form survive the edit untouched.
    for k, v in before.items():
        if k not in entry and k not in MANAGED_EXTRA and k not in _FORM_KEYS:
            entry[k] = v

    def setkey(key, value):
        if value in (None, "", [], {}):
            entry.pop(key, None)
        else:
            entry[key] = value

    if "customer" in f:
        setkey("customer", str(f.get("customer") or "").strip())
    else:
        setkey("customer", before.get("customer"))
    if any(k in f for k in ("network_name", "network_vpn", "network_check", "network_probe")):
        net = dict(before.get("network") or {})
        for key, fk in (("name", "network_name"), ("check", "network_check"), ("probe", "network_probe")):
            v = str(f.get(fk) or "").strip()
            if v:
                net[key] = v
            else:
                net.pop(key, None)
        if f.get("network_vpn"):
            net["vpn"] = True
        else:
            net.pop("vpn", None)
        setkey("network", net)
    else:
        setkey("network", before.get("network"))
    if "alert_timing" in f:
        from core import alert_timing
        setkey("alert_timing", alert_timing.clean(f.get("alert_timing")))
    else:
        setkey("alert_timing", before.get("alert_timing"))
    for key in ("basis_emails", "recovery_emails", "down_emails", "access_emails"):
        if key in f:
            good, _ = _emails(f.get(key))
            setkey(key, ",".join(good))
        else:
            setkey(key, before.get(key))
    if not f.get("manager_receivers") and before.get("manager_receivers") and "manager_receivers" not in f:
        entry["manager_receivers"] = before["manager_receivers"]
    if "live_refresh_seconds" in f:
        v = f.get("live_refresh_seconds")
        setkey("live_refresh_seconds", int(v) if v not in (None, "", 0, "0") else None)
    else:
        setkey("live_refresh_seconds", before.get("live_refresh_seconds"))
    if "actions" in f:
        acts = f.get("actions") or []
        if isinstance(acts, str):
            acts = [a.strip() for a in acts.split(",") if a.strip()]
        setkey("actions", list(acts))
    else:
        setkey("actions", before.get("actions"))
    if "notes" in f:
        setkey("notes", str(f.get("notes") or "").strip())
    else:
        setkey("notes", before.get("notes"))

    def _check(parsed):
        names = [str(e.get("name")) for e in (parsed or {}).get("systems") or []]
        if name not in names:
            raise RuntimeError("write check failed: the system is missing from the new file")
    write_yaml_safely("systems.yaml", data, _check)


_FORM_KEYS = ("name", "client", "language", "has_gui_access", "has_os_access", "sap_system_id", "connection_name",
              "username", "password", "ssh_host", "ssh_port", "ssh_username", "ssh_password", "sap_instance_nr",
              "rfc", "smtp_host", "smtp_port", "smtp_username", "smtp_password", "sender_email",
              "alert_receivers", "alert_receivers_cc")


def system_extras(name: str) -> dict:
    """The extra fields for the edit form."""
    e = existing_entry(name) or {}
    net = e.get("network") or {}
    acts = e.get("actions") or []
    if isinstance(acts, str):
        acts = [a.strip() for a in acts.split(",") if a.strip()]
    return {"customer": e.get("customer", ""), "network_name": net.get("name", ""),
            "network_vpn": bool(net.get("vpn")), "network_check": net.get("check", ""),
            "network_probe": net.get("probe", "") if isinstance(net.get("probe", ""), str)
            else ",".join(net.get("probe") or []),
            "basis_emails": e.get("basis_emails", ""), "manager_receivers": e.get("manager_receivers", ""),
            "recovery_emails": e.get("recovery_emails", ""), "down_emails": e.get("down_emails", ""),
            "access_emails": e.get("access_emails", ""),
            "alert_timing": e.get("alert_timing") or {},
            "live_refresh_seconds": e.get("live_refresh_seconds", ""), "actions": acts,
            "notes": e.get("notes", ""), "missing_env": sorted(set(_env_missing(e)))}


# -------------------------------------------------------------- testing

def test_connection(name: str) -> dict:
    """Ports, tunnel far end and an RFC logon for a saved system. Never raises."""
    from core import reachability
    from core.config_loader import get_all_systems
    cfg = next((s for s in get_all_systems() if s.get("name") == name), None)
    if cfg is None:
        return {"ok": False, "steps": [{"step": "config", "ok": False, "text": "system not found -- save it first"}]}
    steps = []
    host, gw, disp = reachability._sap_ports(cfg)
    if not host:
        return {"ok": False, "steps": [{"step": "config", "ok": False, "text": "no RFC host configured"}]}
    tunnelled = reachability._is_loopback(host)
    gw_ok = reachability.tcp_open(host, gw)
    steps.append({"step": f"gateway port {host}:{gw}", "ok": gw_ok,
                  "text": "answers" if gw_ok else "does not answer"})
    if tunnelled and gw_ok:
        far = reachability.tcp_open_far_end(host, gw)
        steps.append({"step": "tunnel far end", "ok": far,
                      "text": "SAP system reached through the tunnel" if far else
                      "the tunnel accepts but the far end does not answer (VPN down or system down)"})
        gw_ok = far
    disp_ok = reachability.tcp_open(host, disp)
    steps.append({"step": f"dispatcher port {host}:{disp}", "ok": disp_ok,
                  "text": "answers" if disp_ok else "does not answer"})
    if gw_ok:
        try:
            from collectors.rfc_collector import SapSession
            with SapSession(name, cfg) as s:
                if s.ok:
                    info = s.call("RFC_SYSTEM_INFO") or {}
                    si = info.get("RFCSI_EXPORT") or {}
                    steps.append({"step": "RFC logon", "ok": True,
                                  "text": f"logged on -- {si.get('RFCSYSID', '')} on {si.get('RFCHOST', '')}"})
                else:
                    steps.append({"step": "RFC logon", "ok": False, "text": s.error or "failed"})
        except Exception as exc:  # noqa: BLE001
            steps.append({"step": "RFC logon", "ok": False, "text": f"{type(exc).__name__}: {exc}"})
    try:
        from core import timeline
        timeline.add(name, "config", "Connection test", "; ".join(f"{s['step']}: {s['text']}" for s in steps),
                     actor=timeline.actor_name(), severity="ok" if all(s["ok"] for s in steps) else "warning")
    except Exception:
        pass
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


# ------------------------------------------------------------- thresholds

_THR_FIELDS = ("warning", "critical", "inclusive", "severity", "enabled", "alert", "channels")


_YAML_WORDS = {"on", "off", "yes", "no", "true", "false", "null", "y", "n", "~"}


def _yaml_scalar(v) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(str(x) for x in v) + "]"
    text = str(v)
    # "off" unquoted is read back by YAML as the boolean False: quote it.
    return f'"{text}"' if text.strip().lower() in _YAML_WORDS else text


def _change_text(vals: dict) -> str:
    extra = ""
    if "alert" in vals or "channels" in vals:
        extra = f"alert {vals.get('alert', '(unchanged)')}"
        if vals.get("channels") is not None:
            extra += f" via {', '.join(vals['channels']) or 'no channel'}"
        if not any(k in vals for k in ("warning", "critical", "severity", "enabled")):
            return extra
        extra = "; " + extra
    return _change_text_limits(vals) + extra


def _change_text_limits(vals: dict) -> str:
    if "severity" in vals or ("warning" not in vals and "critical" not in vals):
        parts = []
        if "severity" in vals:
            parts.append(f"not running = {vals['severity']}")
        if "enabled" in vals:
            parts.append("switched on" if vals["enabled"] else "switched OFF")
        return ", ".join(parts)
    text = f"warning {vals.get('warning')}, critical {vals.get('critical')}"
    if vals.get("enabled") is False:
        text += ", switched OFF"
    return text


def read_thresholds() -> dict:
    import yaml
    path = os.path.join(config_dir(), "thresholds.yaml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        data = {}
    out = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[str(k)] = {kk: v.get(kk) for kk in ("warning", "critical", "inclusive", "emergency",
                                                    "severity", "enabled", "alert", "channels")
                           if kk in v}
    return out


ALERT_LEVELS = ("off", "critical", "warning")        # warning = warning AND critical
ALERT_CHANNELS = ("email", "whatsapp", "sms", "call")


def validate_threshold(key: str, warning, critical, inclusive=None, severity=None,
                       enabled=None, alert=None, channels=None) -> tuple[list[str], dict]:
    """
    Check one metric's limits before they are written.

    Direction comes from core/metric_catalogue.py:
      high  -- higher is worse: warning must not be above critical
      low   -- lower is worse (days to expiry, free work processes, uptime,
               cache hit %): warning must not be BELOW critical
      state -- a service that should be running: only `severity`
               (critical / warning / off) and `enabled` apply
    `enabled` false switches the metric off (no colour, no alert).
    """
    from core import metric_catalogue as cat
    errors = []
    key = str(key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.\-/]{0,79}", key):
        errors.append("Metric name: letters, digits, '.', '_' or '-' (e.g. sap.st22.dumps).")
    direction = cat.direction(key)
    vals: dict = {}
    # Alert choice for this value: when (off / critical only / warning and
    # critical) and which channels.
    if alert not in (None, ""):
        a = str(alert).strip().lower()
        if a not in ALERT_LEVELS:
            errors.append("Alert must be off, critical or warning.")
        else:
            vals["alert"] = a
    if channels is not None:
        if isinstance(channels, str):
            channels = [c for c in re.split(r"[,\s]+", channels) if c]
        chans = [str(c).strip().lower() for c in (channels or [])]
        bad = [c for c in chans if c not in ALERT_CHANNELS]
        if bad:
            errors.append(f"Unknown channel(s): {', '.join(bad)}.")
        else:
            vals["channels"] = [c for c in ALERT_CHANNELS if c in chans]
    alert_only = "alert" in vals or "channels" in vals
    if enabled is not None:
        vals["enabled"] = bool(enabled) if not isinstance(enabled, str) else \
            enabled.strip().lower() in ("1", "true", "yes", "on")
    if direction == cat.STATE:
        if severity not in (None, ""):
            sev = str(severity).strip().lower()
            if sev not in cat.STATE_SEVERITIES:
                errors.append("Severity must be critical, warning or off.")
            else:
                vals["severity"] = sev
        if "severity" not in vals and "enabled" not in vals and not alert_only:
            errors.append("Choose what 'not running' means: critical, warning or off.")
        return errors, vals
    for label, v in (("warning", warning), ("critical", critical)):
        if v is None:
            continue              # not sent: this field stays as it is
        if v == "":
            vals[label] = None    # sent empty: back to the standard / built-in
            continue
        try:
            n = float(v)
            vals[label] = int(n) if n.is_integer() else n
        except (TypeError, ValueError):
            errors.append(f"{label.capitalize()} must be a number.")
    if vals.get("warning") is None and vals.get("critical") is None and "enabled" not in vals \
            and not alert_only:
        errors.append("Give a warning or a critical limit (or both).")
    w, c = vals.get("warning"), vals.get("critical")
    if w is not None and c is not None:
        if direction == cat.LOW and w < c:
            errors.append("For this metric LOWER values are worse: warning must not be below critical "
                          "(e.g. warning 30 days, critical 7 days).")
        elif direction != cat.LOW and w > c:
            errors.append("Warning must not be above critical (higher values are worse).")
    if inclusive is not None:
        vals["inclusive"] = bool(inclusive)
    return errors, vals


def save_threshold(key: str, warning, critical, inclusive=None, severity=None, enabled=None,
                   alert=None, channels=None) -> dict:
    """
    Change one metric's limits in config/thresholds.yaml, KEEPING the
    comments and the order of the file (they document the company
    standard). Only the warning/critical/inclusive lines of that one block
    are rewritten; a new metric is appended at the end.
    """
    from core import timeline
    errors, vals = validate_threshold(key, warning, critical, inclusive, severity, enabled,
                                      alert, channels)
    if errors:
        raise ValueError(" ".join(errors))
    path = os.path.join(config_dir(), "thresholds.yaml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    key = key.strip()
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*:\s*(#.*)?$", ln)), None)
    if start is None:
        block = ["", f"{key}:"]
        for k in _THR_FIELDS:
            if vals.get(k) is not None:
                block.append(f"  {k}: {_yaml_scalar(vals[k])}")
        lines += block
    else:
        end = start + 1
        while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
            if not lines[end].strip() and end + 1 < len(lines) and not lines[end + 1].startswith((" ", "\t")):
                break
            end += 1
        body = lines[start + 1:end]
        # New fields use the block's own indentation (2 or 4 spaces, tabs).
        ind = next((re.match(r"^(\s+)", ln).group(1) for ln in body
                    if ln.strip() and not ln.lstrip().startswith("#") and re.match(r"^\s+", ln)), "  ")
        for k in _THR_FIELDS:
            if k not in vals:
                continue
            v = vals[k]
            text = None if v is None else _yaml_scalar(v)
            idx = next((i for i, ln in enumerate(body) if re.match(rf"^\s+{k}\s*:", ln)), None)
            if idx is not None:
                if text is None:
                    body.pop(idx)
                else:
                    indent = re.match(r"^(\s+)", body[idx]).group(1)
                    comment = ""
                    m = re.search(r"\s(#.*)$", body[idx])
                    if m:
                        comment = "  " + m.group(1)
                    body[idx] = f"{indent}{k}: {text}{comment}"
            elif text is not None:
                # after the last field line, before trailing blank lines
                pos = len(body)
                while pos and not body[pos - 1].strip():
                    pos -= 1
                body.insert(pos, f"{ind}{k}: {text}")
        lines = lines[:start + 1] + body + lines[end:]
    text = "\n".join(lines) + "\n"

    def _check(parsed):
        got = (parsed or {}).get(key) or {}
        for k in ("warning", "critical"):
            if vals.get(k) is not None and float(got.get(k)) != float(vals[k]):
                raise RuntimeError("write check failed")
        for k in ("severity", "enabled", "inclusive", "alert", "channels"):
            if k in vals and vals[k] is not None and got.get(k) != vals[k]:
                raise RuntimeError("write check failed")
    write_text_safely("thresholds.yaml", text, _check)
    try:
        from core import live_metrics
        live_metrics._EXCEL_CACHE = None
    except Exception:
        pass
    timeline.add("IBOPS", "config", f"Threshold {key} changed",
                 _change_text(vals), actor=timeline.actor_name())
    return read_thresholds().get(key, {})
