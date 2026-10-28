"""
Escalation by time: an alert nobody acknowledges climbs a ladder.

    config/escalation.yaml (copy the .example):
        enabled: true
        states: [SYSTEM DOWN]          # reachability states that escalate
        peers: true                    # also "[WINDOWS VM SILENT]"
        steps:
          - {after_minutes: 15, action: manager_mail}
          - {after_minutes: 30, action: sms,   to: [oncall, manager]}
          - {after_minutes: 45, action: voice, to: [oncall]}
        manager_phones: ["+91 98xxxxxxxx"]

The clock starts when the problem starts (the reachability "since"). Any
of these stops the ladder for that problem: Acknowledge, Ignore until
fixed, Snooze (for that alert or "all"), a maintenance window, recovery.
Each step happens once per problem; a new outage starts a new ladder. A
send that fails (MSG91 refused, SMTP down) is tried again after 5, 15 and
60 minutes, then given up and noted on the timeline. If the dashboard was
not running when a step fell due, all steps that are due go out together
when it starts again (nobody acknowledged in that time either).
Checked every minute by core/ops_tick.py. Every step taken (or skipped
because SMS/voice is not configured) is on the system's timeline.
"""
from __future__ import annotations

import os
from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__)

STATE_KIND = {"SYSTEM DOWN": "system_down", "VPN NOT CONNECTED": "vpn_down", "CREDENTIALS": "credentials",
              "NOT REACHABLE": "not_reachable", "COLLECTOR UNREACHABLE": "collector_down",
              "TUNNEL DOWN": "collector_down", "COLLECTOR NOT ANSWERING": "collector_down"}
ONCE_S = 366 * 86400        # each step once per problem (the key holds the problem's start)
DEFAULT_STEPS = [{"after_minutes": 15, "action": "manager_mail"},
                 {"after_minutes": 30, "action": "sms", "to": ["oncall", "manager"]},
                 {"after_minutes": 45, "action": "voice", "to": ["oncall"]}]


def config() -> dict:
    try:
        import yaml
        from core.config_loader import CONFIG_DIR
        path = os.path.join(CONFIG_DIR, "escalation.yaml")
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"config/escalation.yaml unreadable: {exc}")
        return {}


def _held(system: str, kind: str) -> str | None:
    from core import alert_control
    if alert_control.in_maintenance(system):
        return "maintenance"
    for s in alert_control.active_silences(system):
        if s["kind"] in ("*", kind):
            return f"{s['mode']} by {s['created_by']}"
    return None


def _phones(targets: list[str], cfg: dict, system_cfg: dict | None) -> list[str]:
    from core import oncall
    out: list[str] = []
    for t in targets or []:
        if t == "oncall":
            cur = oncall.current()
            out += oncall.phones_for(cur.get("emails") or [])
        elif t == "manager":
            out += [str(p) for p in (cfg.get("manager_phones") or [])]
        else:
            out.append(str(t))
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _do_step(i: int, step: dict, system: str, kind: str, since: str, minutes: int, message: str,
             cfg: dict, system_cfg: dict | None) -> None:
    from core import alert_ledger, recipients
    action = str(step.get("action") or "")
    ledger_kind = f"escalation_{i + 1}"
    # A step that cannot work (SMS not configured, no phone numbers) is noted
    # once per problem, not retried every minute.
    if action in ("sms", "voice"):
        from notifications import sms
        missing = ("MSG91 SMS is not configured" if action == "sms" and not sms.sms_configured() else
                   "MSG91 voice is not configured" if action == "voice" and not sms.voice_configured() else
                   "no phone numbers" if not _phones(step.get("to") or ["oncall"], cfg, system_cfg) else "")
        if missing:
            ok, _ = alert_ledger.should_send(system, f"{ledger_kind}_skipped", f"{kind}:{since}",
                                             min_interval_s=ONCE_S, daily_cap=20)
            if ok:
                alert_ledger.record(system, f"{ledger_kind}_skipped", f"{kind}:{since}", "WARNING",
                                    f"Escalation step {i + 1} ({action}) skipped: {missing}", [], delivered=True)
            return
    # Failed sends are retried with back-off (now, +5, +15, +60 min), then
    # given up and noted once -- never every minute (core/attempts.py).
    from core import attempts
    scope = f"esc:{system}:{ledger_kind}:{kind}:{since}"
    if not attempts.allowed(scope):
        if attempts.gave_up(scope):
            ok, _ = alert_ledger.should_send(system, f"{ledger_kind}_failed", f"{kind}:{since}",
                                             min_interval_s=ONCE_S, daily_cap=20)
            if ok:
                row = attempts.last(scope) or {}
                alert_ledger.record(system, f"{ledger_kind}_failed", f"{kind}:{since}", "WARNING",
                                    f"Escalation step {i + 1} ({action}) given up after {row.get('tries')} "
                                    f"tries: {row.get('last_error', '')}", [], delivered=True)
        return
    ok, why = alert_ledger.should_send(system, ledger_kind, f"{kind}:{since}", min_interval_s=ONCE_S,
                                       daily_cap=20)
    if not ok:
        return
    fields = {"system": system, "state": kind.replace("_", " ").upper(), "minutes": minutes,
              "message": message[:300]}
    if action == "manager_mail":
        from core.config_loader import get_smtp_config
        from notifications.mailer import send_html_now
        from html import escape as _e
        to, cc = recipients.basis_and_manager(system_cfg)
        delivered = send_html_now(
            subject=f"[ESCALATION] InfraBeatOps -- {system} {fields['state']} for {minutes} min, not acknowledged",
            html=(f"<p><b>{_e(str(system))} has been {_e(fields['state'])} for {minutes} minutes and nobody has "
                  f"acknowledged it.</b></p><p>{_e(str(message))}</p><p>Acknowledge it (button below, or on the "
                  f"wall) to stop the "
                  f"escalation; next steps: SMS, then a phone call.</p>"),
            to=to, cc=cc, smtp_config=get_smtp_config(system_cfg, require_recipients=False))
        detail = f"mail to {', '.join(to + cc)}"
        rcpt = to + cc
    elif action in ("sms", "voice"):
        from notifications import sms
        phones = _phones(step.get("to") or ["oncall"], cfg, system_cfg)
        rcpt = [f"tel:...{p[-4:]}" for p in phones]
        if not phones:
            delivered, detail = False, "no phone numbers (phone: in oncall.yaml, manager_phones in escalation.yaml)"
        elif action == "sms":
            delivered, detail = sms.send_sms(phones, fields)
        else:
            delivered, detail = sms.call(phones, fields)
        detail = f"{action} to {len(phones)} number(s): {detail}"
    else:
        delivered, detail, rcpt = False, f"unknown action {action!r}", []
    attempts.done(scope, delivered, "" if delivered else detail)
    # The ledger's own timeline line ("alert") records this step.
    alert_ledger.record(system, ledger_kind, f"{kind}:{since}", "CRITICAL",
                        f"Escalation step {i + 1}: {action} after {minutes} min ({kind})", rcpt,
                        delivered=delivered, reason="" if delivered else detail[:200])
    log.info(f"{system}: escalation step {i + 1} ({action}) {'sent' if delivered else 'NOT sent'}: {detail}")


def run() -> int:
    """One pass. Returns the number of problems currently climbing the ladder."""
    cfg = config()
    if not cfg or cfg.get("enabled") is False:
        return 0
    steps = cfg.get("steps") or DEFAULT_STEPS
    states = set(cfg.get("states") or ["SYSTEM DOWN"])
    from core.config_loader import get_all_systems
    from core.ops_db import parse
    from core.reachability import current_failures
    systems = {c.get("name"): c for c in get_all_systems()}
    climbing = 0
    problems = []
    from core import standby
    for system, rec in current_failures().items():
        if system in systems and not standby.active(systems[system]):
            continue            # the peer dashboard monitors it (core/standby.py)
        if rec.get("state") in states:
            problems.append((system, STATE_KIND.get(rec["state"], "system_down"), rec.get("since"),
                             rec.get("message", ""), systems.get(system)))
    if cfg.get("peers", True):
        try:
            from core import peer_watch
            for p in peer_watch.peers():
                if p["silent"] and p["age_seconds"] is not None:
                    since = p["beat_at"]
                    problems.append(("IBOPS", "peer_silent", since, f"{p['name']} silent since {since}", None))
        except Exception:
            pass
    for system, kind, since, message, system_cfg in problems:
        start = parse(since)
        if start is None or _held(system, kind):
            continue
        minutes = int((datetime.now() - start).total_seconds() // 60)
        climbing += 1
        for i, step in enumerate(steps):
            try:
                after = float(step.get("after_minutes"))
            except (TypeError, ValueError):
                continue
            if minutes >= after:
                try:
                    _do_step(i, step, system, kind, str(since), minutes, message, cfg, system_cfg)
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"{system}: escalation step {i + 1} failed: {exc}")
    return climbing
