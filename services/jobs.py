"""
Job runner: everything slow or external that a sweep should not wait on.

    mail          send one HTML mail (queued by notifications/mailer.py when
                  IBO_MAIL_ASYNC=1, default)
    ai_analysis   AI RCA for a system's last snapshot (queued by the sweep
                  when IBO_AI_ASYNC=1) -- result written back to the snapshot
    maintenance   retention, ledger prune, job prune, nightly DB backup
    watchdog      "monitor is down" -- any service whose heartbeat is older
                  than IBO_HEARTBEAT_STALE_SECONDS (default 300) is mailed
                  to the manager and Basis, repeated every 30 min while
                  stale, with a recovery mail when it comes back.

Jobs live in the `jobs` table (core/store.py). A failed job is retried
with a delay up to max_attempts and then marked failed with its error --
visible at GET /api/jobs. A runner that dies mid-job leaves the row
'running'; the next runner requeues rows older than 30 min.

WHO WATCHES THE WATCHDOG
If this process stops, its own heartbeat goes stale, and the web service
checks that on every /healthz (dashboard/app.py readiness) and sends the
same alert itself -- the two services watch each other, so a single
failure is always reported by the survivor. If BOTH stop, only an
external check can tell you: point an uptime monitor at /healthz.
"""
from __future__ import annotations

import os
import socket
import time
from datetime import datetime

from services import common  # noqa: E402  (sets cwd, loads .env)

log = common.get_logger("services.jobs")

WORKER = f"{socket.gethostname()}:{os.getpid()}"


# ------------------------------------------------------------------ handlers

def _job_mail(payload: dict) -> None:
    from core.config_loader import get_smtp_config, get_systems
    from notifications.mailer import send_html_now, last_reason
    cfg = next((s for s in get_systems() if s.get("name") == payload.get("system")), None)
    ok = send_html_now(subject=payload["subject"], html=payload["html"], to=payload["to"],
                       smtp_config=get_smtp_config(cfg, require_recipients=False), cc=payload.get("cc") or [],
                       text_fallback=payload.get("text"), system=payload.get("system", ""),
                       kind=payload.get("kind", ""), key=payload.get("key"),
                       severity=payload.get("severity", ""))
    if not ok:
        why = last_reason()
        if why.startswith(("permanent", "suppressed")):
            # A 5xx answer or a suspended mailbox: retrying the job in five
            # minutes gets the same answer and spends quota. Recorded already.
            log.warning(f"mail job not retried: {why}")
            return
        raise RuntimeError(f"SMTP send failed: {why or 'see log'}")


def _job_ai_analysis(payload: dict) -> None:
    """Run the AI RCA on the saved snapshot and write the result back to it."""
    from core.status_snapshot import load_snapshot, _snapshot_path
    from core.atomic_json import write_json
    from core.models import MonitoringResult, MetricResult, Status
    from core.incidents import Incident
    from evaluation.ai_analyzer import analyze_incidents

    system = payload["system"]
    snap = load_snapshot(system)
    if not snap:
        raise RuntimeError(f"no snapshot for {system}")
    result = MonitoringResult(system=system, client=str(snap.get("client", "")))
    for m in snap.get("metrics", []) or []:
        try:
            result.metrics.append(MetricResult(
                name=m.get("name", ""), value=None, display_value=str(m.get("display_value", "")),
                status=Status(m.get("status", "UNKNOWN")), detail=m.get("detail", "")))
        except Exception:
            continue
    for inc in snap.get("incidents", []) or []:
        try:
            result.incidents.append(Incident.from_dict(inc))
        except Exception:
            continue
    try:
        result.overall_status = Status(snap.get("overall_status", "UNKNOWN"))
    except ValueError:
        pass
    analyses = analyze_incidents(result)
    ai = analyses[0] if analyses else None
    snap = load_snapshot(system) or snap          # re-read: a sweep may have run meanwhile
    snap["ai_analysis"] = None if ai is None else {
        "severity": ai.severity, "root_cause": ai.likely_root_cause, "confidence": ai.confidence,
        "recommended_actions": list(ai.recommended_actions or []),
        "analysed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "async": True}
    write_json(_snapshot_path(system), snap)
    log.info(f"{system}: AI analysis written to snapshot ({'found' if ai else 'nothing to analyse'})")


def _job_maintenance(_payload: dict) -> None:
    from core import store, alert_ledger, retention
    retention.run()
    alert_ledger.prune(90)
    n = store.prune_jobs(14)
    backup_dir = os.path.join(common.PROJECT_ROOT, "dashboard", "backups")
    dest = os.path.join(backup_dir, f"ibops-{datetime.now():%Y%m%d}.sqlite")
    store.backup(dest)
    # keep seven backups
    try:
        files = sorted(f for f in os.listdir(backup_dir) if f.startswith("ibops-"))
        for old in files[:-7]:
            os.remove(os.path.join(backup_dir, old))
    except OSError:
        pass
    log.info(f"maintenance: retention run, {n} old job(s) pruned, backup {os.path.basename(dest)}")


def _job_ticket_create(payload: dict) -> None:
    from core import tickets
    tickets.create_now(payload)


def _job_ticket_note(payload: dict) -> None:
    from core import tickets
    tickets.note_now(payload)


HANDLERS = {"mail": _job_mail, "ai_analysis": _job_ai_analysis, "maintenance": _job_maintenance,
            "ticket_create": _job_ticket_create, "ticket_note": _job_ticket_note}


# ------------------------------------------------------------------ watchdog

def check_heartbeats(sender_service: str = "jobs") -> list[dict]:
    """
    Mail manager + Basis about any OTHER service whose heartbeat is stale.
    Called by this runner every minute and by the web service's readiness
    probe, so each watches the other. Returns the stale entries.
    """
    from core import store, alert_ledger, recipients
    from notifications.mailer import send_html_now
    from core.config_loader import get_smtp_config

    try:
        stale_after = float(os.getenv("IBO_HEARTBEAT_STALE_SECONDS", "300") or 300)
    except ValueError:
        stale_after = 300.0
    # Single-process deployment (IBO_ROLE=all): there are no other services
    # to watch unless IBO_EXPECTED_SERVICES says so explicitly.
    default = "" if (os.getenv("IBO_ROLE", "all") or "all").strip().lower() == "all" else "web,collector,jobs"
    expected = [s.strip() for s in os.getenv("IBO_EXPECTED_SERVICES", default).split(",")
                if s.strip() and s.strip() != sender_service]
    seen = {h["service"]: h for h in store.heartbeats()}
    stale: list[dict] = []
    for svc in expected:
        h = seen.get(svc)
        age = None if h is None else h.get("age_seconds")
        if h is None or age is None or age > stale_after:
            stale.append({"service": svc, "age_seconds": age, "host": (h or {}).get("host", "?")})

    to, cc = recipients.basis_and_manager(None)
    for s in stale:
        age_text = "never reported" if s["age_seconds"] is None else f"last seen {s['age_seconds'] // 60} min ago"
        send_html_now(
            subject=f"[MONITOR DOWN] InfraBeatOps {s['service']} service is not running ({age_text})",
            html=(f"<p><b>The InfraBeatOps <code>{s['service']}</code> service has stopped reporting</b> "
                  f"({age_text}, host {s['host']}).</p>"
                  f"<p>While it is down: "
                  f"{'no sweeps run and no reports are sent' if s['service'] == 'collector' else ''}"
                  f"{'the dashboard is unavailable' if s['service'] == 'web' else ''}"
                  f"{'no mail or AI analysis goes out' if s['service'] == 'jobs' else ''}.</p>"
                  f"<p>Restart it (packaging/SERVICES.md). This mail repeats every 30 minutes until "
                  f"the heartbeat returns; a recovery mail follows.</p>"),
            to=to, cc=cc, smtp_config=get_smtp_config(None, require_recipients=False), system="IBOPS", kind="monitor_down",
            key=s["service"], min_interval_s=1800, severity="CRITICAL")
    # recovery: a service seen fresh now, for which a monitor_down went out and no recovery yet
    for svc in expected:
        if any(s["service"] == svc for s in stale):
            continue
        last = [a for a in alert_ledger.recent(50, "IBOPS") if a["key"] == svc and not a["suppressed"]]
        if last and last[0]["kind"] == "monitor_down":
            send_html_now(
                subject=f"[RECOVERED] InfraBeatOps {svc} service is reporting again",
                html=f"<p>The <code>{svc}</code> service heartbeat is back (host {seen[svc]['host']}).</p>",
                to=to, cc=cc, smtp_config=get_smtp_config(None, require_recipients=False), system="IBOPS",
                kind="monitor_recovered", key=svc, min_interval_s=600, severity="INFO")
    return stale


# ---------------------------------------------------------------------- main

def _schedule_maintenance() -> None:
    """One maintenance job per day, idempotent across restarts."""
    from core import store
    today = datetime.now().strftime("%Y-%m-%d")
    row = store.conn().execute(
        "SELECT 1 FROM jobs WHERE kind='maintenance' AND created_at LIKE ? LIMIT 1", (today + "%",)
    ).fetchone()
    if row is None:
        hour = os.getenv("IBO_MAINTENANCE_HOUR", "03")
        run_at = datetime.strptime(f"{today} {hour}:00:00", "%Y-%m-%d %H:%M:%S")
        delay = max(0.0, (run_at - datetime.now()).total_seconds())
        store.enqueue("maintenance", {}, delay_seconds=delay, max_attempts=1)


def main() -> None:
    from core import store
    common.install_signal_handlers(log)
    store.conn()
    requeued = store.requeue_stale_running()
    if requeued:
        log.warning(f"{requeued} job(s) left 'running' by a previous runner were requeued")
    common.start_heartbeat("jobs", lambda: str(store.jobs_summary()))
    log.info(f"jobs service started as {WORKER}")
    last_watchdog = 0.0
    last_sched = ""
    while not common.stopping():
        try:
            if datetime.now().strftime("%Y-%m-%d") != last_sched:
                _schedule_maintenance()
                last_sched = datetime.now().strftime("%Y-%m-%d")
            if time.monotonic() - last_watchdog > 60:
                last_watchdog = time.monotonic()
                try:
                    check_heartbeats("jobs")
                except Exception as exc:
                    log.warning(f"watchdog failed: {exc}")
                # Re-probe systems that are VPN-down / DOWN so the 5-minute
                # DOWN reminder and the [RECOVERED] mail happen without anyone
                # pressing Read live (core/reachability.py).
                try:
                    from core.reachability import recheck_known_failures
                    recheck_known_failures()
                except Exception as exc:
                    log.warning(f"reachability recheck failed: {exc}")
                try:
                    from core import ops_tick
                    ops_tick.run()
                except Exception as exc:
                    log.warning(f"ops tick failed: {exc}")
            job = store.claim_job(WORKER)
            if job is None:
                time.sleep(2)
                continue
            handler = HANDLERS.get(job["kind"])
            if handler is None:
                store.finish_job(job["id"], False, f"unknown job kind {job['kind']}")
                continue
            try:
                handler(job["payload"])
                store.finish_job(job["id"], True)
            except Exception as exc:
                try:
                    from core import errors as _errors
                    _errors.capture(exc, "job", context=f"job {job['id']} {job['kind']}")
                except Exception:
                    pass
                # warning, not error: capture() above already recorded it (an
                # ERROR line would be captured a second time as source "log").
                log.warning(f"job {job['id']} {job['kind']} failed (attempt {job['attempts']}): "
                          f"{type(exc).__name__}: {exc}")
                store.finish_job(job["id"], False, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            log.error(f"job loop error: {type(exc).__name__}: {exc}")
            time.sleep(5)
    log.info("jobs service stopped")


if __name__ == "__main__":
    main()
