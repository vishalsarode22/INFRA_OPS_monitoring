"""
Synthetic user transaction: what a user feels, measured every few minutes.

A fresh RFC logon (not the pooled monitoring connection), then
RFC_SYSTEM_INFO and a one-row table read, timed:

    synthetic.logon_ms   time to log on (message server / dispatcher / auth)
    synthetic.total_ms   logon + two round trips

The numbers go into the metric history, so the learned baselines learn
what is normal for each hour, and the wall shows them on the Operations
page. Two runs in a row over IBO_SYNTHETIC_WARN_MS (default 3000) mail Basis
once (kind "synthetic", repeats every 2 h while it lasts).

Off by default -- every run is a logon in the security audit log:
    IBO_SYNTHETIC=1              all systems with RFC
    synthetic: true              per system in systems.yaml (overrides off)
    synthetic: false             per system (overrides on)
    IBO_SYNTHETIC_MINUTES=10
A refused logon (wrong password, locked user) stops the synthetic check for
that system until the dashboard restarts -- repeated failed logons would
lock the monitoring user.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_last = {"at": 0.0}
_stopped: dict[str, str] = {}
_slow_runs: dict[str, int] = {}
results: dict[str, dict] = {}


def interval_s() -> float:
    try:
        return max(2.0, float(os.getenv("IBO_SYNTHETIC_MINUTES", "10") or 10)) * 60
    except ValueError:
        return 600.0


def wanted(cfg: dict) -> bool:
    flag = cfg.get("synthetic")
    if flag is not None:
        return bool(flag)
    return os.getenv("IBO_SYNTHETIC", "0").strip().lower() in ("1", "true", "yes", "on", "all")


def measure(system: str, cfg: dict) -> dict:
    """One timed run. Never raises."""
    from collectors.rfc_collector import Connection, build_rfc_params, is_credential_error, guarded_connect
    out = {"system": system, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": False}
    if Connection is None:
        out["error"] = "pyrfc not installed"
        return out
    params = build_rfc_params(cfg)
    if not params:
        out["error"] = "no RFC configuration"
        return out
    conn = None
    t0 = time.perf_counter()
    try:
        conn = guarded_connect(system, params, cfg)     # via the cross-process logon guard
        t1 = time.perf_counter()
        conn.call("RFC_SYSTEM_INFO")
        conn.call("RFC_READ_TABLE", QUERY_TABLE="T000", DELIMITER="|", FIELDS=[{"FIELDNAME": "MANDT"}],
                  ROWCOUNT=1)
        t2 = time.perf_counter()
        out.update(ok=True, logon_ms=round((t1 - t0) * 1000), total_ms=round((t2 - t0) * 1000))
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        out["error"] = text[:300]
        if is_credential_error(text):
            out["credentials"] = True
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _record(system: str, r: dict, cfg: dict) -> None:
    from core.metric_history import append_metrics
    from core.models import MetricResult, Status
    results[system] = r
    if not r.get("ok"):
        return
    ms = [MetricResult(name="synthetic.logon_ms", value=r["logon_ms"], display_value=f"{r['logon_ms']} ms",
                       status=Status.NORMAL, source="synthetic"),
          MetricResult(name="synthetic.total_ms", value=r["total_ms"], display_value=f"{r['total_ms']} ms",
                       status=Status.NORMAL, source="synthetic")]
    append_metrics(system, ms, force=True)
    try:
        warn = float(os.getenv("IBO_SYNTHETIC_WARN_MS", "3000") or 3000)
    except ValueError:
        warn = 3000.0
    if r["total_ms"] > warn:
        _slow_runs[system] = _slow_runs.get(system, 0) + 1
    else:
        _slow_runs[system] = 0
        try:
            from core.alert_control import clear_until_fixed
            clear_until_fixed(system, {"synthetic"})
        except Exception:
            pass
    if _slow_runs.get(system, 0) >= 2:
        try:
            from core import recipients, timeline
            from core.config_loader import get_smtp_config
            from notifications.mailer import send_html
            sent = send_html(subject=f"[SLOW] InfraBeatOps -- {system}: a test logon + read takes {r['total_ms']} ms",
                             html=(f"<p><b>{system}</b> answers slowly: a fresh logon took {r['logon_ms']} ms and "
                                   f"logon plus two reads {r['total_ms']} ms (limit {int(warn)} ms), two runs in a "
                                   f"row. Users will feel this. Check SM50/SM66 for busy work processes, the message "
                                   f"server and the network.</p>"),
                             to=recipients.basis(cfg), smtp_config=get_smtp_config(cfg, require_recipients=False),
                             system=system, kind="synthetic", key="slow", min_interval_s=7200, severity="WARNING")
            if sent:
                timeline.add(system, "synthetic", f"Slow test transaction: {r['total_ms']} ms", severity="warning")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"{system}: synthetic alert failed: {exc}")


def run_all() -> int:
    from core import alert_control, reachability
    from core.config_loader import get_all_systems
    n = 0
    for cfg in get_all_systems():
        name = cfg.get("name")
        if not name or not wanted(cfg) or name in _stopped or alert_control.sweep_block_reason(name):
            continue
        if name in reachability.current_failures() or reachability.precheck(name, cfg):
            continue
        from collectors.checks_job import synthetic_job
        from core import rfc_proc
        try:
            # Not queued behind a long check run: the wait would count as slowness.
            r = rfc_proc.call(synthetic_job, name, cfg, timeout=180, skip_if_busy=True)
        except rfc_proc.WorkerBusy:
            continue
        except Exception as exc:  # noqa: BLE001
            r = {"system": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                 "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        n += 1
        if r.get("credentials"):
            _stopped[name] = r.get("error", "")
            log.warning(f"{name}: synthetic logon refused -- stopped until restart: {r.get('error')}")
            try:
                from core import timeline
                timeline.add(name, "synthetic", "Synthetic check stopped: logon refused", r.get("error", ""),
                             severity="warning")
            except Exception:
                pass
            continue
        _record(name, r, cfg)
    return n


def maybe_start() -> bool:
    if time.monotonic() - _last["at"] < interval_s() or not _lock.acquire(blocking=False):
        return False
    _last["at"] = time.monotonic()

    def _go():
        try:
            run_all()
        except Exception as exc:  # noqa: BLE001
            try:
                from core import errors
                errors.capture(exc, "loop", context="synthetic transaction")
            except Exception:
                pass
        finally:
            _lock.release()
    threading.Thread(target=_go, daemon=True, name="synthetic").start()
    return True


def status() -> dict:
    return {"results": results, "stopped": _stopped}
