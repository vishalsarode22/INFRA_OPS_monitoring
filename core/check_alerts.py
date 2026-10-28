"""
Mail Basis about the extra checks (collectors/extra_checks.py) without
waiting for a sweep: every IBO_CHECK_ALERT_MINUTES (default 30) the
checks are run (from their 30-minute cache) for each system this machine
monitors over RFC, and a check that is WARNING or CRITICAL is mailed --
once per check and state per 12 hours (the ledger), a change of detail
(another job missed) goes out at once.

Skipped for a system that is unreachable (the reachability alerts cover
that), in maintenance, parked, or on standby (core/standby.py).
Runs on its own thread so slow RFC reads never hold up the ops tick, and
the RFC part runs in a separate worker process (core/rfc_proc.py) so it
never freezes the web server. IBO_CHECK_ALERTS=0 turns the mails off; the
checks still run so the wall shows them.
"""
from __future__ import annotations

import hashlib
import html
import os
import threading
import time

from utils.logger import get_logger

log = get_logger(__name__)

_running = threading.Lock()
_last = {"at": 0.0}


def interval_s() -> float:
    try:
        return float(os.getenv("IBO_CHECK_ALERT_MINUTES", "30") or 30) * 60
    except ValueError:
        return 1800.0


def maybe_start() -> bool:
    """Every IBO_CHECK_ALERT_MINUTES: run the checks (stored for the wall
    and the sweep) and, unless IBO_CHECK_ALERTS=0, mail problems."""
    if time.monotonic() - _last["at"] < interval_s() or not _running.acquire(blocking=False):
        return False
    _last["at"] = time.monotonic()

    def _go():
        try:
            run_all()
        except Exception as exc:  # noqa: BLE001
            try:
                from core import errors
                errors.capture(exc, "loop", context="check alerts")
            except Exception:
                pass
        finally:
            _running.release()
    threading.Thread(target=_go, daemon=True, name="check-alerts").start()
    return True


def _eligible(cfg: dict) -> bool:
    from core import alert_control
    name = cfg.get("name")
    if not ((cfg.get("rfc") or {}).get("ashost") or cfg.get("hana")):
        return False
    if alert_control.sweep_block_reason(name):
        return False
    try:
        from core import standby
        if not standby.active(cfg):
            return False
    except Exception:
        pass
    try:
        from core.reachability import current_failures
        if name in current_failures():
            return False
    except Exception:
        pass
    return True


def run_all() -> int:
    """Run the checks (in the RFC worker process, core/rfc_proc.py) and mail
    what is WARNING / CRITICAL. Mails go out from here, not from the worker."""
    from collectors import extra_checks
    from collectors.checks_job import collect_checks_job
    from core import rfc_proc
    from core.config_loader import get_all_systems
    if not extra_checks.enabled():
        return 0
    mail_on = os.getenv("IBO_CHECK_ALERTS", "1").strip().lower() not in ("0", "false", "no", "off")
    # Check values alert through core/alert_engine.py, as chosen per value on
    # the Thresholds page; the old per-check mails only with IBO_LEGACY_VALUE_MAILS=1.
    from core.alert_engine import legacy_value_mails
    mail_on = mail_on and legacy_value_mails()
    mailed = 0
    for cfg in get_all_systems():
        name = cfg.get("name")
        if not name or not _eligible(cfg):
            continue
        try:
            from core import reachability
            if reachability.precheck(name, cfg):
                continue
            res = rfc_proc.call(collect_checks_job, name, cfg)
            metrics = [extra_checks._from_json(d) for d in res.get("metrics") or []]
            if res.get("error"):
                log.info(f"{name}: extra checks: {res['error']}")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"{name}: extra checks failed: {type(exc).__name__}: {exc}")
            continue
        if not metrics:
            continue
        if mail_on:
            for m in metrics:
                if m.status.value in ("WARNING", "CRITICAL") and _mail(cfg, m):
                    mailed += 1
        try:
            from core.alert_control import clear_until_fixed
            ok_names = {m.name for m in metrics if m.status.value == "NORMAL"}
            if ok_names:
                clear_until_fixed(name, {f"check:{n}" for n in ok_names})
        except Exception:
            pass
    return mailed


def _mail(cfg: dict, m) -> bool:
    from core import recipients, timeline
    from core.config_loader import get_smtp_config
    from notifications.mailer import send_html
    name = cfg.get("name")
    to, cc = (recipients.basis_and_manager(cfg) if m.status.value == "CRITICAL"
              else (recipients.basis(cfg), []))
    if not to:
        return False
    # Same problem = same key, so the repeat waits 12 h. Numbers that move on
    # their own ("running 45 min", "12 days", "3 failed") are left out of the
    # key; names that contain digits (K900123, BWPCLNT100, Z_JOB_01) stay in,
    # so another transport / destination / job is a new mail at once.
    import re as _re
    stable = _re.sub(r"(?<![\w])\d+(?:[.,:]\d+)*(?![\w])", "#", str(m.detail or ""))
    digest = hashlib.sha1(f"{m.status.value}|{stable}".encode()).hexdigest()[:10]
    label = {"sap.strust.cert_days_left": "certificate expiry", "sap.slicense.days_left": "licence expiry",
             "sap.sm37.missed_jobs": "expected job missed", "sap.sm36.housekeeping_missing": "housekeeping jobs",
             "sap.sm37.overdue_jobs": "job running long", "sap.su01.user_issues": "user locked / expiring",
             "sap.sm59.failing_destinations": "RFC destination failing", "sap.sp01.spool_requests": "spool filling",
             "sap.stms.failed_imports": "transport import failed"}.get(m.name, m.name)
    subject = f"[{m.status.value}] InfraBeatOps -- {name}: {label} ({m.display_value})"
    body = (f"<p><b>{html.escape(name)}: {html.escape(label)}</b> -- {html.escape(m.display_value)}</p>"
            f"<p>{html.escape(m.detail)}</p><p>Transaction: {html.escape(m.tcode or '-')}</p>")
    sent = send_html(subject=subject, html=body, to=to, cc=cc,
                     smtp_config=get_smtp_config(cfg, require_recipients=False), system=name,
                     kind=f"check:{m.name}", key=digest, min_interval_s=12 * 3600,
                     severity=m.status.value)
    if sent:
        timeline.add(name, "check", f"{label}: {m.display_value}", m.detail,
                     severity="critical" if m.status.value == "CRITICAL" else "warning")
    return bool(sent)
