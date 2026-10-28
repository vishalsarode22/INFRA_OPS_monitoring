"""
Master pipeline: sequences the entire SAP BASIS monitoring cycle end to end.
    1. Launch SAP Logon fresh, select connection
    2. Log in (keyboard automation, proven reliable)
    3. Acquire SAP GUI Scripting session
    4. Run monitoring cycle (Linux + SAP process + thresholds + AI + immediate alert)
       -- Linux/OS metrics only run if SSH credentials are configured for the system
    5. Collect T-code GUI evidence (screenshots + OCR-extracted data)
    6. Generate LaTeX PDF report (with embedded screenshots)
    7. Append to Excel history
    8. Fill MetroBrands client Excel template (with real OCR-extracted values)
    9. Send final report email

Two entry points:
    run_full_pipeline(system, client)   -- uses single-system credentials
                                            from .env (original, default flow)
    run_pipeline_for_system(system_config) -- uses a full per-system config
                                            dict (name/client/connection/creds,
                                            optionally ssh_host/ssh_username/
                                            ssh_password), used by the
                                            dashboard's multi-system scheduler
"""

import os
import sys
import time
import threading
from dotenv import load_dotenv

load_dotenv()

from core.config_loader import (
    get_sap_credentials, get_launch_config, get_monitoring_tasks,
    get_thresholds,
    get_smtp_config, get_systems,
)
# SAP GUI scripting is Windows-only (pywinauto, win32com). On the Ubuntu
# collector those packages are absent and importing them here used to stop
# main.py before it could do anything -- including the RFC-only work it is
# perfectly able to do on Linux. The GUI branch of the pipeline checks
# GUI_IMPORT_ERROR and says so plainly instead of failing at import time.
try:
    from sap_gui.launcher import kill_sap_processes, launch_saplogon, select_connection
    from sap_gui.connection import login
    from sap_gui.scripting_connection import get_scripting_session
    from collectors.sap_gui_collector import collect_tcode_evidence
    GUI_IMPORT_ERROR: str | None = None
except ImportError as _gui_exc:  # pragma: no cover - platform dependent
    GUI_IMPORT_ERROR = f"{type(_gui_exc).__name__}: {_gui_exc}"

    def kill_sap_processes():  # noqa: D103 - no SAP GUI processes on this host
        return None

    def _gui_unavailable(*_a, **_k):
        raise RuntimeError("SAP GUI scripting is not available on this host "
                           f"({GUI_IMPORT_ERROR}). Run GUI sweeps on the Windows VM.")

    launch_saplogon = select_connection = login = _gui_unavailable
    get_scripting_session = collect_tcode_evidence = _gui_unavailable
from sap_gui.reliability import com_thread_entry   # pure Python, importable everywhere
from core.orchestrator import run_monitoring_cycle
from core.tcode_metrics import normalize_tcode_results, merge_normalized_metrics
from core.event_engine import EventEngine
from core.correlation import CorrelationEngine
from evaluation.threshold_engine import evaluate_all
from evaluation.ai_analyzer import analyze_incidents as run_ai_analysis
from reporting.latex_report_builder import generate_latex_pdf_report
from reporting.excel_writer import append_result_to_excel
from reporting.excel_template_writer import fill_metrobrands_template
from notifications.email_report import send_final_report, send_failure_alert, send_tcode_failure_alert
from notifications.email_alert import send_critical_alert
from core.status_snapshot import save_snapshot
from core import heartbeat
from core.intelligence_runtime import get_intelligence_runtime, attach_operational_intelligence
from utils.logger import get_logger

log = get_logger(__name__, "application")

TEMPLATE_PATH = "config/templates/InfraBeatOps_Simple_Monitoring_Sheet.xlsx"


from reporting.system_paths import system_pdf_path, system_excel_path, system_template_path

from reporting.production_reports import generate_system_reports

def run_full_pipeline(system: str, client: str):
    """
    Default single-system pipeline. Uses SAP/launch credentials from .env
    (get_sap_credentials, get_launch_config) -- the original, manually-run
    flow via `python main.py`.
    """
    log.info("===== SAP BASIS Monitoring Pipeline: START =====")

    launch_cfg = get_launch_config()
    creds = get_sap_credentials()

    kill_sap_processes()
    launch_saplogon(launch_cfg["exe_path"])
    select_connection(launch_cfg["connection_name"])
    time.sleep(2)

    login_result = login(
        client=creds["client"], username=creds["username"],
        password=creds["password"], language=creds["language"],
        submit=True, verify=True,
    )
    if not login_result["success"]:
        log.error("Login could not be verified. Aborting pipeline.")
        sys.exit(1)
    log.info("Login verified.")

    session = None
    for attempt in range(5):
        try:
            session = get_scripting_session()
            break
        except Exception as e:
            log.warning(f"Scripting session not ready yet (attempt {attempt+1}/5): {e}")
            time.sleep(2)
    if session is None:
        log.error("Could not acquire SAP GUI Scripting session. Aborting pipeline.")
        sys.exit(1)

    result = run_monitoring_cycle(system=system, client=client, run_ai=False, send_alert=False)

    tasks = _tasks_this_run({"name": system})
    gui_results = collect_tcode_evidence(
        tasks,
        system=system,
        client=client,
    )

    # Make SAP GUI evidence available to the AI analysis layer.
    result.gui_results = gui_results

    # Finalize the complete result only after both OS and SAP GUI evidence are available.
    thresholds = get_thresholds()
    # One row per metric name. The GUI collector reuses the RFC collector's
    # metric names on purpose, so a bare extend() published every shared
    # metric twice -- a real report showed sap.sm12.lock_count as both
    # "2 count" and "3 count" in adjacent rows. See
    # core.tcode_metrics.merge_normalized_metrics for the reconciliation rule.
    result.metrics, _metric_conflicts = merge_normalized_metrics(
        result.metrics, normalize_tcode_results(gui_results, system=result.system)
    )
    for _note in _metric_conflicts:
        log.info(f"[{result.system}] metric reconciled -- {_note}")
    result.metrics = evaluate_all(result.metrics, thresholds, system=result.system)
    result.compute_overall_status()
    result.events = EventEngine(result.system, result.client).process_metrics(result.metrics)
    result.incidents = CorrelationEngine(result.system, result.client).correlate(
        result.metrics, result.events
    )

    # Operational intelligence is evaluated only after the complete metric and
    # incident picture exists. The runtime is per-system, so repeated scheduler
    # cycles build baseline history without mixing systems.
    if not _ai_enabled():
        log.info(f"{result.system}: AI analysis disabled (IBO_ENABLE_AI=0); skipping.")
        result.ai_analysis = None
    else:
        try:
            intelligence_runtime = get_intelligence_runtime(result.system)
            intelligence = attach_operational_intelligence(result, intelligence_runtime)
            analyses = run_ai_analysis(result, intelligence=intelligence)
            result.ai_analysis = analyses[0] if analyses else None
        except Exception as e:
            # AI/intelligence is optional; deterministic monitoring and incident
            # severity remain available when the provider fails.
            log.error(
                "Milestone 8.2: AI/intelligence analysis failed after final metric collection: %s",
                type(e).__name__,
            )
            result.errors.append(f"ai_analyzer: {type(e).__name__}")

    if result.overall_status.value == "CRITICAL":
        try:
            send_critical_alert(result, get_smtp_config())
        except Exception as e:
            log.error(f"Failed to send final critical alert: {e}")
            result.errors.append(f"email_alert: {e}")

    save_snapshot(result, gui_results=gui_results, system_name=system)

    report_root = f"reports/{time.strftime('%Y-%m-%d')}/{system}"
    pdf_path = generate_latex_pdf_report(
        result,
        gui_results=gui_results,
        output_path=f"{report_root}/{system}_Monitoring_Report.pdf",
    )
    excel_history_path = append_result_to_excel(
        result,
        path=f"{report_root}/{system}_Monitoring.xlsx",
    )
    metrobrands_path = fill_metrobrands_template(
        template_path=TEMPLATE_PATH,
        output_path=f"reports/{time.strftime('%Y-%m-%d')}/Monitoring Sheet.xlsx",
        gui_results=gui_results,
    )

    if _email_enabled():
        smtp_config = get_smtp_config()
        send_final_report(result, smtp_config, pdf_path, metrobrands_path)
    else:
        log.info("Final report email disabled (IBO_ENABLE_EMAIL=0); skipping.")

    log.info("===== SAP BASIS Monitoring Pipeline: COMPLETE =====")
    print(f"\nPDF (LaTeX): {pdf_path}")
    print(f"Excel history: {excel_history_path}")
    print(f"MetroBrands report: {metrobrands_path}")


SYSTEM_MAX_ATTEMPTS = 2  # 1 initial try + 1 full retry, per system, per cycle

# Absolute ceiling on one system's pipeline. This is a backstop for a run that
# somehow keeps beating while achieving nothing; the STALL check below is what
# normally catches a hang. A PRD sweep with 22 T-codes, a ~2min STAT read and an
# AI call runs 6-10 minutes legitimately, so this must be generous.
#
# HISTORY: this was 5*60 with a "TEMPORARY -- raise back to 20*60" comment, and
# the debug value was never raised. A normal PS4 run takes ~390s, so the
# watchdog force-killed healthy runs and re-ran the whole system: 14 times in
# the four days of logs reviewed, every one of them a run that was working.
PIPELINE_TIMEOUT_SECONDS = int(
    os.environ.get("IBO_PIPELINE_TIMEOUT", 20 * 60) or 20 * 60)

# How long the pipeline may go WITHOUT a heartbeat before it is declared hung.
# Every long stage (per T-code, per collector, per report step) calls
# core.heartbeat.beat(), so this is the real hang detector. The longest single
# legitimate gap observed in the logs is the RFC perf collector's STAT read at
# ~118s, so 180s leaves headroom without waiting five minutes on a dead COM call.
PIPELINE_STALL_SECONDS = int(
    os.environ.get("IBO_PIPELINE_STALL", 180) or 180)

# How long to wait for a killed thread to actually unwind before abandoning it.
# See _run_with_watchdog: abandoning it immediately is what let two threads
# drive the same SAP GUI at once.
PIPELINE_JOIN_GRACE_SECONDS = int(
    os.environ.get("IBO_PIPELINE_JOIN_GRACE", 90) or 90)

# How often the watchdog looks at the run's heartbeat.
_WATCH_POLL_SECONDS = 15

# Kill switches. Both default OFF right now: the Gemini keys are exhausted
# for the day and Grok's API key is rejected outright (HTTP 400,
# invalid-argument), so every AI call was guaranteed to fail after burning
# ~7s across all 6 providers -- and every cycle, failed or not, was still
# emailing a report. Set both to "1" in .env once the keys are fixed.
def _env_seconds(name: str, default: int) -> int:
    try:
        return max(10, int(os.environ.get(name, str(default)) or default))
    except ValueError:
        return default


def _wait_beating(fn, stage: str, limit: float):
    """
    fn() on a helper thread while THIS thread (the pipeline's) records a
    heartbeat every 20 s, so a slow but working step is not killed as hung.
    TimeoutError after `limit` seconds (the helper is left to finish on its
    own; its result is then discarded).
    """
    box: dict = {}

    def work():
        try:
            box["v"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- handed to the caller
            box["e"] = exc

    t = threading.Thread(target=work, daemon=True, name=f"wait-{stage}")
    t.start()
    deadline = time.monotonic() + limit
    while True:
        t.join(max(0.05, min(20.0, deadline - time.monotonic())))
        if not t.is_alive():
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{stage} did not finish within {limit:.0f}s")
        if heartbeat.cancelled():
            raise RuntimeError(f"run cancelled during {stage}")
        heartbeat.beat(stage)
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _live_payload_for_sweep(name: str, system_config: dict) -> dict:
    """
    Job/locked-user detail for the incident evidence.

    1. The dashboard's latest live reading, if under 10 minutes old (no RFC at all).
    2. Otherwise one live read in the system's own RFC worker PROCESS
       (core/rfc_workers.py), bounded and with heartbeats. Before, this was a
       full live read on the sweep's own thread with no heartbeat: slow on
       big systems (the watchdog then killed the finished sweep as hung) and,
       when the RFC library crashed (0xC0000005 on CEAT S4_QAS), it took the
       sweep down with it.
    """
    app = sys.modules.get("dashboard.app")
    if app is not None:
        try:
            with app._live_cache_lock:
                cached = app._live_cache.get(name)
                at = app._live_cache_at.get(name)
            if cached and cached.get("connected") and at and time.monotonic() - at < 600:
                return cached
        except Exception:  # noqa: BLE001
            pass
    limit = _env_seconds("IBO_SWEEP_LIVE_DETAIL_WAIT", 120)
    from core import rfc_workers
    if rfc_workers.enabled():
        from collectors.live_job import read_live_job
        try:
            return _wait_beating(lambda: rfc_workers.run(name, read_live_job, (name, system_config),
                                                         timeout=limit + 5),
                                 f"{name}:live-detail", limit + 10) or {}
        except TimeoutError:
            log.warning(f"{name}: job/user detail read took over {limit}s; incidents built without it.")
            return {}
    from collectors.rfc_live import read_live as _read_live
    return _wait_beating(lambda: _read_live(name, system_config, use_cache=True),
                         f"{name}:live-detail", limit) or {}


def _ai_enabled() -> bool:
    """Read at every use, so the Settings page can switch it without a restart."""
    return os.environ.get("IBO_ENABLE_AI", "0").strip().lower() in ("1", "true", "yes", "on")


def _email_enabled() -> bool:
    """Read at every use, so the Settings page can switch it without a restart."""
    return os.environ.get("IBO_ENABLE_EMAIL", "0").strip().lower() in ("1", "true", "yes", "on")


def _run_with_watchdog(system_config: dict) -> tuple:
    """
    Runs _run_pipeline_for_system_once() in a background thread, watched for
    LACK OF PROGRESS rather than for total elapsed time, because some failures
    don't raise a Python exception at all -- they just block forever (e.g. a
    modal SAP dialog left in a state that makes a COM call hang instead of
    erroring out, which can happen if someone manually interferes with the SAP
    GUI window mid-run). Retrying only helps for failures that actually raise;
    a genuine hang needs something external to break it, since Python cannot
    forcibly interrupt a blocked COM call from another thread.

    PROGRESS, NOT DURATION
    ----------------------
    The previous version used one wall-clock deadline, so a slow-but-healthy
    run and a hung one were indistinguishable. With the deadline set to 5
    minutes (a debug value left in the file), a normal ~390s PS4 sweep was
    declared hung and re-run from scratch. The pipeline now calls
    core.heartbeat.beat() at every stage and after every T-code, so a run that
    is making progress is never killed, however long it takes, and a real hang
    is caught in PIPELINE_STALL_SECONDS wherever in the run it happens.

    WAITING FOR THE THREAD TO DIE
    -----------------------------
    This used to return the moment it decided the run was hung, abandoning the
    old thread. The caller then immediately started attempt 2, so TWO threads
    drove the same single SAP GUI desktop. The log shows exactly that: the
    abandoned thread's snapshot write landing between the retry's login and its
    scripting call, which then failed with 'Invalid syntax' and cost the retry
    all of its GUI evidence. kill_sap_processes() is what unblocks the stuck
    COM call, so after killing we now WAIT for the thread to unwind before
    letting anyone else touch the GUI.

    ONE RUN, ONE HEARTBEAT
    ----------------------
    Each attempt gets its own core.heartbeat.Run, bound to the pipeline
    thread's context. When the watchdog gives up, the run is CANCELLED
    first: its late beats no longer count, and the pipeline checks the flag
    at every stage boundary and before any SAP kill/close or delivery, so a
    thread that unblocks later cannot close the NEXT run's SAP GUI or mail a
    stale report. A thread that does not unwind within the join grace is
    ABANDONED and remembered; no new SAP GUI run starts on this machine while
    it is still alive (see abandoned_gui_alive()).

    The hang itself is LOGGED (and recorded in the run history), not mailed.

    Returns (success: bool, result_or_error).
    """
    import contextvars

    name = system_config["name"]
    ctx = _ctx(system_config)
    result_box = {}
    run = heartbeat.start_run(f"{ctx.get('run_id') or 'run'}#{ctx.get('attempt') or 1}",
                              f"{name}:starting")

    @com_thread_entry      # this thread drives SAP GUI scripting: COM per thread
    def target():
        token = heartbeat.bind(run)
        try:
            result_box["value"] = _run_pipeline_for_system_once(system_config)
        except _RunCancelled as e:
            result_box["cancelled"] = str(e)
        except Exception as e:
            # com_thread_entry calls CoUninitialize when target() returns.
            # The stored exception's traceback still holds the frames -- and
            # their locals, i.e. SAP GUI COM objects -- which would then be
            # released after COM is gone. Clear the frames' locals now
            # (the traceback text stays printable).
            _release_frame_locals(e)
            result_box["error"] = e
        finally:
            heartbeat.unbind(token)

    started = time.monotonic()

    thread = threading.Thread(target=contextvars.copy_context().run, args=(target,),
                              daemon=True, name=f"pipeline-{name}")
    thread.start()

    while True:
        thread.join(timeout=_WATCH_POLL_SECONDS)
        if not thread.is_alive():
            break

        stalled_for = run.seconds_since_beat()
        elapsed = time.monotonic() - started

        if stalled_for < PIPELINE_STALL_SECONDS and elapsed < PIPELINE_TIMEOUT_SECONDS:
            continue

        stage = run.stage
        if stalled_for >= PIPELINE_STALL_SECONDS:
            reason = f"no progress for {stalled_for:.0f}s (last stage: {stage})"
        else:
            reason = (f"exceeded the {PIPELINE_TIMEOUT_SECONDS}s ceiling "
                      f"while still reporting progress")

        # Cancel BEFORE killing: from here on nothing this run does counts,
        # and it may not kill or close SAP or deliver anything itself.
        run.cancel(f"hung: {reason}")
        ctx["failed_stage"] = str(stage or "")
        ctx["failure_reason"] = f"hung -- {reason}"
        try:
            import traceback as _tb
            _frame = sys._current_frames().get(thread.ident)
            if _frame is not None:
                log.error(f"Pipeline for {name} hung here (stack of the stuck thread):\n"
                          + "".join(_tb.format_stack(_frame)[-12:]))
        except Exception:  # noqa: BLE001
            pass
        log.error(
            f"Pipeline for {name} appears hung -- {reason}. Force-killing SAP "
            f"processes to unblock it (this attempt counts as failed; logged, not alerted)."
        )
        try:
            kill_sap_processes()
        except Exception:
            pass

        # Give the unblocked thread a chance to unwind before the caller
        # retries. Without this the retry shares the GUI with a dying run.
        thread.join(timeout=PIPELINE_JOIN_GRACE_SECONDS)
        if thread.is_alive():
            run.abandon(f"abandoned after hang: {reason}")
            _register_abandoned(thread, system_config, run)
            ctx["result"] = "abandoned"
            log.error(
                f"Pipeline thread for {name} did not exit within "
                f"{PIPELINE_JOIN_GRACE_SECONDS}s of the SAP kill. Abandoned; it can no "
                f"longer act, and no SAP GUI run starts on this machine until it ends."
            )
        else:
            ctx["result"] = "hung-killed"
        return False, TimeoutError(f"Pipeline for {name} hung: {reason}.")

    if "cancelled" in result_box:
        return False, RuntimeError(f"Pipeline for {name} cancelled: {result_box['cancelled']}")
    if "error" in result_box:
        raise result_box["error"]
    return True, result_box.get("value", True)


def _release_frame_locals(exc: BaseException | None) -> None:
    """Drop the local variables of every finished frame in exc's traceback
    chain (cause / context too), keeping file/line info for logging."""
    import traceback as _tb
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if exc.__traceback__ is not None:
            try:
                _tb.clear_frames(exc.__traceback__)   # skips frames still executing
            except Exception:  # noqa: BLE001
                pass
        exc = exc.__cause__ or exc.__context__


class _RunCancelled(Exception):
    """Raised inside a pipeline thread whose run the watchdog cancelled."""


# Pipeline threads the watchdog abandoned (still blocked somewhere, typically
# in a COM call). While one that drove SAP GUI is alive, no new SAP GUI run
# may start: it could wake up at any moment in the middle of someone else's
# session.
_ABANDONED: list = []
_abandoned_lock = threading.Lock()


def _register_abandoned(thread, system_config: dict, run) -> None:
    with _abandoned_lock:
        _ABANDONED.append({"thread": thread, "system": system_config.get("name"),
                           "run_id": run.run_id,
                           "gui": bool(system_config.get("has_gui_access", True)),
                           "at": time.strftime("%Y-%m-%d %H:%M:%S")})


_GUI_BLOCK_REPORTED: set = set()


def abandoned_gui_alive() -> dict | None:
    """The abandoned SAP GUI pipeline thread that is still alive, or None."""
    with _abandoned_lock:
        _ABANDONED[:] = [a for a in _ABANDONED if a["thread"].is_alive()]
        hit = next((a for a in _ABANDONED if a["gui"]), None)
        return None if hit is None else {k: v for k, v in hit.items() if k != "thread"}


def _ctx(system_config: dict) -> dict:
    """
    The run's outcome record, kept ON the system config so it crosses every
    existing call without changing a signature (primitives only: the config
    is also handed to the RFC worker process). The dashboard reads it for the
    run history: attempt, failed stage, failure reason, screenshots, result.
    """
    ctx = system_config.get("_run_ctx")
    if not isinstance(ctx, dict):
        ctx = {}
        system_config["_run_ctx"] = ctx
    return ctx


def _checkpoint(name: str, stage: str) -> None:
    """Stage boundary: a sign of life, and the end of a cancelled run."""
    heartbeat.beat(f"{name}:{stage}")
    if heartbeat.cancelled():
        raise _RunCancelled(heartbeat.cancel_reason() or "cancelled by the watchdog")


def _kill_sap(name: str, why: str) -> bool:
    """kill_sap_processes(), refused for a run the watchdog cancelled or
    abandoned -- by then the SAP GUI on screen may belong to the next run."""
    if heartbeat.cancelled():
        log.warning(f"{name}: SAP GUI not closed ({why}) -- this run was cancelled by the "
                    f"watchdog ({heartbeat.cancel_reason()}).")
        return False
    try:
        kill_sap_processes()
        return True
    except Exception:  # noqa: BLE001
        return False


def _rca_from_sweep(name, system_config, gui_results) -> None:
    """Start a performance RCA when the sweep's worst SMLG instance is over the band."""
    from core.rca_trigger import TriggerConfig, mark_fired_on_disk, sweep_decision
    from evaluation.rca_pipeline import load_rca_config, rca_enabled, run_rca

    cfg = load_rca_config()
    if not rca_enabled(cfg):
        return
    worst, instance = None, ""
    for metric in gui_results or []:
        if str(getattr(metric, "tcode", "") or "").upper() != "SMLG":
            continue                       # only SMLG's per-instance response times
        for entry in (getattr(metric, "extra_data", {}) or {}).get("instances") or []:
            if not isinstance(entry, dict):
                continue
            try:
                value = float(entry.get("response_time_ms"))
            except (TypeError, ValueError):
                continue
            if worst is None or value > worst:
                worst, instance = value, str(entry.get("instance") or "")
    if worst is None:
        return

    trigger_cfg = TriggerConfig.from_yaml(cfg)
    fire, reason = sweep_decision(name, worst, instance, trigger_cfg)
    log.info(f"{name}: RCA sweep trigger -- SMLG worst {worst:.0f} ms: {reason}")
    if not fire:
        return
    try:
        _block = RCA_BLOCK(name) if RCA_BLOCK else None
    except Exception:  # noqa: BLE001
        _block = None
    if _block:
        log.info(f"{name}: performance RCA not started -- {_block} ({reason})")
        return
    mark_fired_on_disk(name, reason)
    log.warning(f"{name}: starting performance RCA ({reason})")
    run_rca(system_config, reason, cfg, should_stop=_stop_requested)


def run_pipeline_for_system(system_config: dict) -> bool:
    """
    Runs the full pipeline for one system, with automatic recovery from a
    failure BEFORE the data was collected (SAP Logon crashing, login never
    completing, an exception or a hang in the collectors -- see
    _run_with_watchdog above): SAP processes are reset and the system is
    tried once more. A failure AFTER collection (analysis, reports) is not
    retried -- the measurements are already saved, and sweeping SAP again
    would only repeat them. No mail is sent per attempt: failures are
    logged and recorded in the run history (system_config["_run_ctx"]).

    Returns True if the pipeline completed (with or without a retry),
    False otherwise.
    """
    system_config.pop("_tcodes_this_run", None)      # worked out again by this run (_tasks_this_run)
    # Per-system report locations (reporting/system_paths.py); the reports
    # themselves are written by generate_system_reports().
    _paths = (system_pdf_path(system_config.get("name", "UNKNOWN")),
              system_excel_path(system_config.get("name", "UNKNOWN")))
    ctx = _ctx(system_config)
    ctx.setdefault("run_id", f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
    name = system_config["name"]
    try:
        from core import live_settings
        live_settings.sync()          # a Settings page change made by the web process
    except Exception:
        pass

    # A pipeline thread the watchdog abandoned may still wake up inside SAP
    # GUI. Starting another SAP GUI run now would put two threads on one
    # desktop, so it is refused (and recorded) until that thread has ended.
    if system_config.get("has_gui_access", True) and not GUI_IMPORT_ERROR:
        zombie = abandoned_gui_alive()
        if zombie:
            ctx.update(result="skipped", failed_stage="start",
                       failure_reason=(f"previous run still alive: the {zombie['system']} run "
                                       f"{zombie['run_id']} was abandoned at {zombie['at']} and its "
                                       f"thread has not ended"))
            log.warning(f"{name}: sweep skipped -- {ctx['failure_reason']}.")
            # Visible on the Errors page (and its internal mail), once per
            # abandoned run: until that thread ends, NO SAP GUI sweep runs on
            # this machine, which used to be in the log only.
            try:
                key = f"gui-blocked:{zombie['run_id']}"
                if key not in _GUI_BLOCK_REPORTED:
                    _GUI_BLOCK_REPORTED.add(key)
                    from core import errors as _errors
                    _errors.capture(RuntimeError(
                        f"SAP GUI sweeps are blocked: the {zombie['system']} run {zombie['run_id']} was abandoned "
                        f"at {zombie['at']} and its thread is still inside SAP GUI. Sweeps resume when it ends; "
                        f"if it does not, restart the dashboard."), "loop",
                        context="SAP GUI blocked by an abandoned sweep thread")
            except Exception:  # noqa: BLE001
                pass
            return False

    # One sweep per machine, across processes. The dashboard scheduler and
    # a Task-Scheduler `python main.py` could otherwise run two sweeps on
    # one desktop and write the same snapshot. See core/run_lock.py.
    from core.run_lock import sweep_lock
    with sweep_lock(name) as acquired:
        if not acquired:
            ctx.update(result="skipped", failed_stage="start",
                       failure_reason="another process is running a sweep (sweep lock held)")
            return False
        return _run_pipeline_with_retries(system_config)


# Set by the dashboard: a callable that answers True once someone pressed
# Stop on the sweep in progress. The sweep then captures no further T-code,
# makes no second attempt, sends no sheet and plans no report retry.
STOP_CHECK = None
# Set by the dashboard: a callable giving the reason an automatic RCA must not
# run on a system now (paused, snoozed, stopped, parked, maintenance), or None.
RCA_BLOCK = None


def _tasks_this_run(system_config: dict) -> list[dict]:
    """
    The T-codes of this sweep: the system's set from the Profile page
    (core/tcode_sets.py: ticks, order, settings), narrowed by the profile
    that started the run when that profile names its own T-codes. The list
    is kept on the config so the sheet, the PDF and the report gate use the
    same one.
    """
    name = system_config.get("name", "")
    only = None
    prof_id = system_config.get("_profile_id")
    if prof_id:
        try:
            from core.profiles import load_profiles, resolve_tcodes
            prof = next((p for p in load_profiles() if p.get("id") == prof_id), None)
            if prof and prof.get("tcodes") != "all":
                only = resolve_tcodes(prof) or None
        except Exception as exc:  # noqa: BLE001
            log.warning(f"{name}: profile {prof_id} T-codes not read ({exc}); the system's set is used")
    try:
        from core import tcode_sets
        tasks = tcode_sets.tasks_for(name, only)
        if only and not tasks:
            log.warning(f"{name}: none of profile {prof_id}'s T-codes ({', '.join(only)}) is ticked for this "
                        f"system on the Profile page; the system's own T-codes are captured instead")
            tasks = tcode_sets.tasks_for(name)
    except Exception as exc:  # noqa: BLE001 -- never lose the sweep over the set
        log.error(f"{name}: T-code set not read ({type(exc).__name__}: {exc}); every T-code is captured")
        tasks = get_monitoring_tasks()
    system_config["_tcodes_this_run"] = [str(t.get("tcode") or "").strip().upper() for t in tasks]
    log.info(f"{name}: {len(tasks)} T-code(s) this run: {', '.join(system_config['_tcodes_this_run'])}")
    return tasks


def _stop_requested() -> bool:
    # A run the watchdog cancelled stops like a Stop press: no further
    # T-code, no analysis, no snapshot, no delivery.
    if heartbeat.cancelled():
        return True
    try:
        return bool(STOP_CHECK and STOP_CHECK())
    except Exception:  # noqa: BLE001
        return False


def _run_pipeline_with_retries(system_config: dict) -> bool:
    name = system_config["name"]
    ctx = _ctx(system_config)
    ctx["attempts_max"] = SYSTEM_MAX_ATTEMPTS
    last_error = None

    for attempt in range(1, SYSTEM_MAX_ATTEMPTS + 1):
        ctx["attempt"] = attempt
        ctx["collected"] = False
        for key in ("result", "failed_stage", "failure_reason"):
            ctx.pop(key, None)
        try:
            success, value = _run_with_watchdog(system_config)
            if success:
                return value
            last_error = value  # the TimeoutError from the watchdog
            ctx.setdefault("failure_reason", str(value)[:300])
        except Exception as e:
            last_error = e
            try:
                from core import errors as _errors
                _errors.capture(e, "sweep", context=f"{name} attempt {attempt}/{SYSTEM_MAX_ATTEMPTS}")
            except Exception:
                pass
            ctx.setdefault("failure_reason", f"{type(e).__name__}: {e}"[:300])
            log.error(f"Pipeline attempt {attempt}/{SYSTEM_MAX_ATTEMPTS} for {name} failed: {e}", exc_info=True)

        if _stop_requested():
            ctx["result"] = "stopped-by-user"
            log.warning(f"{name}: stopped from the dashboard -- no further attempt.")
            return False

        # Failures are logged and go to the run history -- no "pipeline
        # failed (attempt n/2)" mail. A system that is DOWN is parked by the
        # attempt itself (see _handle_nothing_collected) and the reachability
        # check mails SYSTEM DOWN once, with its own de-duplication.
        why_not = None
        if ctx.get("collected"):
            why_not = "the failure came after the data was collected (nothing to gain from sweeping again)"
        elif ctx.get("result") == "parked":
            why_not = "the system is down and its sweeps are parked until someone clears the flag"
        elif ctx.get("result") == "abandoned":
            why_not = "the hung attempt's thread is still alive"
        elif ctx.get("result") == "skipped":
            why_not = ctx.get("failure_reason") or "skipped"
        ctx.setdefault("result", "failed")

        if attempt < SYSTEM_MAX_ATTEMPTS and why_not is None:
            log.info(f"{name}: attempt {attempt}/{SYSTEM_MAX_ATTEMPTS} failed before the data was "
                     f"collected ({ctx.get('failure_reason')}); resetting SAP processes and retrying.")
            try:
                kill_sap_processes()
            except Exception:
                pass
            time.sleep(5)
            continue
        if why_not and attempt < SYSTEM_MAX_ATTEMPTS:
            log.info(f"{name}: not retried -- {why_not}.")
        break

    log.error(f"{name}: sweep did not complete after attempt {ctx.get('attempt')}/{SYSTEM_MAX_ATTEMPTS} "
              f"-- skipping this system for this cycle. Last error: {last_error}")
    return False


class _CycleRun:
    """run_monitoring_cycle on a DAEMON thread (a hung RFC call must never block exit)."""

    def __init__(self, name: str, kwargs: dict):
        import contextvars
        self.name, self._done = name, threading.Event()
        self._result, self._error, self.joined = None, None, False
        ctx = contextvars.copy_context()           # same per-system thresholds in the worker
        self._thread = threading.Thread(target=lambda: ctx.run(self._run, kwargs),
                                        name=f"cycle-{name}", daemon=True)
        self._thread.start()

    def _run(self, kwargs: dict) -> None:
        try:
            self._result = run_monitoring_cycle(**kwargs)
        except BaseException as exc:  # noqa: BLE001 -- handed to result()
            self._error = exc
            if not self.joined:
                log.warning(f"{self.name}: RFC/SSH reads failed: {type(exc).__name__}: {exc}")
        finally:
            self._done.set()

    def result(self):
        self.joined = True
        # No heartbeat while waiting: a hung RFC call must still look hung to
        # the watchdog, exactly as when the reads ran inline.
        self._done.wait()
        if self._error is not None:
            raise self._error
        return self._result


def _start_cycle_thread(name: str, kwargs: dict) -> "_CycleRun":
    return _CycleRun(name, kwargs)


def _parallel_cycle_enabled() -> bool:
    """IBO_PARALLEL_RFC=0 puts the RFC/SSH reads back after the SAP GUI logon."""
    return os.getenv("IBO_PARALLEL_RFC", "1").strip().lower() not in ("0", "false", "no", "off")


def _run_pipeline_for_system_once(system_config: dict) -> bool:
    """
    A single attempt at the full pipeline for one system. Raises on any
    unhandled failure -- the caller (run_pipeline_for_system above)
    is responsible for catching, alerting, and retrying.

    Each system declares its own access level in config/systems.yaml:
        has_os_access: true/false   -- SSH access for Linux/SAP-process metrics
        has_gui_access: true/false  -- SAP GUI Scripting access for T-code evidence
    Both default to true if omitted, matching the original always-both
    behavior. A system with has_os_access=false never attempts SSH at
    all (regardless of any leftover ssh_host value in its config), and
    a system with has_gui_access=false never launches SAP Logon or
    attempts login -- it goes straight to OS-only monitoring.

    Returns True once the pipeline completes (report/email steps still
    run even if OS metrics were skipped or GUI evidence failed).
    """
    name = system_config["name"]
    # Everything graded in this sweep uses this system's thresholds
    # (config/thresholds_systems.yaml over config/thresholds.yaml).
    from core import thresholds as _thr
    _thr.set_system(name)
    log.info(f"===== Pipeline START for system {name} =====")
    heartbeat.beat(f"{name}:pipeline-start")

    has_os_access = system_config.get("has_os_access", True)
    has_gui_access = system_config.get("has_gui_access", True)

    if has_os_access:
        ssh_host = system_config.get("ssh_host", "").strip()
        if ssh_host:
            ssh_creds = {
                "host": ssh_host,
                "port": system_config.get("ssh_port", 22),
                "username": system_config.get("ssh_username", ""),
                "password": system_config.get("ssh_password", ""),
            }
        else:
            log.warning(f"{name}: has_os_access=true but no ssh_host configured -- skipping OS metrics.")
            ssh_creds = {"host": ""}
    else:
        log.info(f"{name}: has_os_access=false -- skipping OS/SSH metrics entirely (not attempting).")
        ssh_creds = {"host": ""}

    gui_available = True
    gui_skip_reason = ""
    desktop_note = ""
    session = None

    # The RFC/SSH reads need nothing from SAP GUI: run them on a worker thread
    # WHILE SAP Logon starts and logs on, instead of after it (they took
    # 20-120 s with the SAP session idle). Joined below where the result is
    # first needed. Only when SAP GUI is going to be used -- otherwise inline.
    instance_nr = system_config.get("sap_instance_nr") or None
    _cycle_future = None
    if has_gui_access and not GUI_IMPORT_ERROR and _parallel_cycle_enabled():
        _cycle_future = _start_cycle_thread(
            name, dict(system=name, client=system_config["client"], ssh_creds=ssh_creds,
                       instance_nr=instance_nr, run_ai=False, send_alert=False,
                       system_cfg=system_config))
        log.info(f"{name}: RFC/SSH reads started in parallel with the SAP GUI logon.")

    if has_gui_access and GUI_IMPORT_ERROR:
        log.warning(f"{name}: has_gui_access=true but SAP GUI scripting cannot run on "
                    f"this host ({GUI_IMPORT_ERROR}). Continuing with RFC/SSH collectors; "
                    f"screenshots need the Windows VM.")
        gui_available = False
        gui_skip_reason = (f"SAP GUI scripting is not available on this machine ({GUI_IMPORT_ERROR}); "
                           f"screenshots are taken by the Windows monitoring VM.")
    elif has_gui_access:
        # A SAP GUI failure must NOT abort the sweep.
        #
        # GUI is one of three collectors, not a precondition for the others.
        # Unguarded, a login-window timeout on TST propagated out of here,
        # burned both retries and skipped the system entirely -- losing its
        # working RFC and SSH metrics and sending six failure emails, for a
        # problem that said nothing about SAP's health.
        #
        # Common causes: connection_name not present in THIS machine's SAP
        # Logon Pad (names are per-workstation), SAP GUI not installed,
        # SAPLOGON_EXE_PATH wrong, or no interactive desktop (service or
        # disconnected RDP session).
        # A closed Remote Desktop leaves no screen for SAP GUI. The keep-desktop
        # task (install_keep_awake.ps1) moves the session to the console within
        # seconds; give it a minute before the logon. A locked screen is only
        # noted -- the sweep still runs, the report says why a picture is black.
        try:
            from sap_gui import desktop_state as _ds
            _desk = _ds.state()
            _waited = 0
            while _desk == "disconnected" and _waited < 60 and not _stop_requested():
                heartbeat.beat(f"{name}:waiting-for-desktop")
                time.sleep(5)
                _waited += 5
                _desk = _ds.state()
            if _desk in _ds.EXPLAIN:
                log.warning(f"{name}: {_ds.EXPLAIN[_desk]}")
                desktop_note = _ds.EXPLAIN[_desk]
        except Exception:  # noqa: BLE001 -- the check must never cost the sweep
            pass
        _checkpoint(name, "sap-gui-prepare")
        try:
            _kill_sap(name, "before the logon")

            launch_config = get_launch_config(
                connection_name=system_config["connection_name"]
            )

            heartbeat.beat(f"{name}:sap-gui-launch")
            launch_saplogon(launch_config["exe_path"])
            select_connection(launch_config["connection_name"])
            # No fixed wait here: the logon polls for its screen itself.
            heartbeat.beat(f"{name}:sap-gui-login")

            login_result = login(
                client=system_config["client"], username=system_config["username"],
                password=system_config["password"], language=system_config.get("language", "EN"),
                submit=True, verify=True,
            )
            if not login_result["success"]:
                log.warning(f"{name}: SAP GUI login failed -- continuing with RFC/SSH collectors.")
                gui_available = False
                if login_result.get("method") == "skipped-shared-desktop":
                    gui_skip_reason = ("other SAP GUI sessions were open on the monitoring VM, so the "
                                       "monitor did not log on (it never types into someone else's "
                                       "window). Close SAP Logon on the VM when it is not in use.")
                else:
                    _said = str(login_result.get("message") or "").strip()
                    gui_skip_reason = (f"the SAP GUI logon to SAP Logon entry "
                                       f"'{system_config.get('connection_name')}' did not succeed "
                                       + (f"-- SAP said: '{_said}' " if _said else "")
                                       + "(check the user, password and client).")
                # Close the logon screen at once: left open it sat there for the
                # rest of the run (RFC reads, AI, reports) and looked hung.
                _kill_sap(name, "after a failed logon")
            else:
                for attempt in range(5):
                    try:
                        session = get_scripting_session()
                        break
                    except Exception:
                        time.sleep(2)
                if session is None:
                    log.warning(f"{name}: no scripting session -- continuing with RFC/SSH collectors.")
                    gui_available = False
                    gui_skip_reason = "SAP GUI scripting gave no session after the logon."
        except Exception as e:
            log.warning(
                f"{name}: SAP GUI unavailable ({type(e).__name__}: {e}). "
                f"Continuing with RFC/SSH collectors."
            )
            gui_available = False
            gui_skip_reason = (f"SAP GUI could not be started for SAP Logon entry "
                               f"'{system_config.get('connection_name')}' ({type(e).__name__}: "
                               f"{str(e)[:160]}). Check that the entry exists on the monitoring VM "
                               f"with exactly this name.")
    else:
        log.info(f"{name}: has_gui_access=false -- skipping SAP GUI login and T-code evidence entirely (not attempting).")
        gui_available = False

    # --- Monitoring cycle: OS metrics run only if has_os_access and ssh_creds has a host ---
    _ctx_now = _ctx(system_config)
    if _stop_requested():
        _checkpoint(name, "monitoring-cycle")         # a cancelled run ends here
        log.warning(f"{name}: stopped from the dashboard before the RFC/SSH reads -- nothing collected.")
        _ctx_now.update(result="stopped-by-user", failed_stage="monitoring-cycle",
                        failure_reason="stopped from the dashboard")
        try:
            from core import report_gate as _rg
            _rg.clear(name)
        except Exception:  # noqa: BLE001
            pass
        _kill_sap(name, "after Stop")
        return False
    _checkpoint(name, "monitoring-cycle")
    if _cycle_future is not None:
        result = _cycle_future.result()          # raises what the worker raised, as before
    else:
        result = run_monitoring_cycle(
            system=name, client=system_config["client"], ssh_creds=ssh_creds, instance_nr=instance_nr,
            run_ai=False, send_alert=False,
            # Passing the whole entry lets the RFC collector run for systems that
            # define an "rfc" block -- headless, no SAP GUI and no OS access.
            system_cfg=system_config,
        )

    # --- GUI T-code evidence: only if GUI access is enabled and login/session succeeded ---
    gui_results = []
    result.gui_results = gui_results
    if gui_skip_reason:
        # The reports print this beside every check that has no screenshot
        # (reporting/rfc_fallback.py) instead of a bare "see screenshot".
        result.errors.append(f"sap_gui: {gui_skip_reason}")
    if desktop_note:
        result.errors.append(f"desktop: {desktop_note}")

    # Deliveries found during the run (alert mails, the report) are queued
    # here and handed to the outbox only once the data is saved -- see
    # core/outbox.py. They never run inside the watched section.
    _deliveries: list = []

    if gui_available:
        _checkpoint(name, "tcode-evidence")
        tasks = _tasks_this_run(system_config)
        gui_results = collect_tcode_evidence(tasks, system=system_config.get('name', 'UNKNOWN'),
                                             client=system_config.get('client', 'UNKNOWN'),
                                             should_stop=_stop_requested)

        # Make SAP GUI evidence available to the AI analysis layer.
        result.gui_results = gui_results

        _broken = [r for r in gui_results
                   if (getattr(r, "extra_data", {}) or {}).get("parser_health") == "broken"]
        if _broken and _email_enabled():
            def _parser_mail(_broken=_broken):
                from notifications.mailer import send_html
                from core import recipients as _rcp
                from html import escape as _esc     # SAP / OCR text into HTML
                rows = "".join(
                    f"<tr><td>{_esc(str(r.tcode))}</td>"
                    f"<td>{_esc((r.detail or '').split('PARSER CHECK:')[-1].strip())}</td></tr>"
                    for r in _broken)
                return send_html(
                    subject=f"[CHECK] InfraBeatOps -- {name}: {len(_broken)} T-code(s) captured but extracted nothing",
                    html=(f"<p>The sweep took the screenshots, but extraction returned no data for "
                          f"T-codes that used to yield values. This is usually a SAP GUI theme, DPI "
                          f"scaling, column layout or language change on the monitoring VM -- the "
                          f"metric is not missing, the parser is.</p>"
                          f"<table border='1' cellpadding='4'><tr><th>T-code</th><th>Finding</th></tr>{rows}</table>"
                          f"<p>Open the screenshot under the day's evidence folder and compare it with "
                          f"config/ocr_patterns.yaml. After fixing the pattern, reset the baseline: "
                          f"POST /api/parser-health/reset?system={name}</p>"),
                    to=_rcp.basis(system_config), smtp_config=get_smtp_config(system_config),
                    system=name, kind="ocr_parser", key=[r.tcode for r in _broken],
                    min_interval_s=6 * 3600, severity="WARNING")
            _deliveries.append(("parser_health", _parser_mail, f"{len(_broken)} T-code(s) extracted nothing"))

        failed_tcodes = [
            r.tcode
            for r in gui_results
            if r.display_value == "failed"
        ]
        if failed_tcodes:
            log.warning(f"{name}: {len(failed_tcodes)} T-code(s) failed to capture: {failed_tcodes}")
            if _email_enabled():
                def _tcode_mail(failed_tcodes=list(failed_tcodes)):
                    from core import report_gate as _rg
                    _tc_smtp = get_smtp_config(system_config)
                    if _rg.enabled():
                        _tc_smtp = _rg.internal_smtp(system_config, _tc_smtp)
                    return send_tcode_failure_alert(name, failed_tcodes, _tc_smtp)
                _deliveries.append(("tcode_failure", _tcode_mail, ", ".join(failed_tcodes)))
            else:
                log.info(f"{name}: T-code failure alert email disabled (IBO_ENABLE_EMAIL=0); skipping.")
    else:
        log.info(f"Skipping T-code GUI evidence for {name} -- GUI access disabled or no active SAP session.")

    # The data of this run is collected: a failure from here on is not
    # retried by sweeping SAP again (see _run_pipeline_with_retries).
    _ctx_now["collected"] = True
    _ctx_now["screenshots_captured"] = _count_screenshots(gui_results)

    if _stop_requested():
        _checkpoint(name, "analysis")                 # a cancelled run ends here
        # Stop pressed: nothing more on this system -- no analysis, alerts,
        # snapshot (it would replace the last complete one) or sheet.
        log.warning(f"{name}: stopped from the dashboard -- no analysis, alerts, snapshot or sheet; no retry.")
        _ctx_now.update(result="stopped-by-user", failed_stage="tcode-evidence",
                        failure_reason="stopped from the dashboard")
        try:
            from core import report_gate as _rg
            _rg.clear(name)
        except Exception:
            pass
        if gui_available:
            _kill_sap(name, "after Stop")
        return False
    _checkpoint(name, "analysis")

    # Finalize only after OS and GUI collectors have both contributed.
    thresholds = get_thresholds()
    # One row per metric name. The GUI collector reuses the RFC collector's
    # metric names on purpose, so a bare extend() published every shared
    # metric twice -- a real report showed sap.sm12.lock_count as both
    # "2 count" and "3 count" in adjacent rows. See
    # core.tcode_metrics.merge_normalized_metrics for the reconciliation rule.
    result.metrics, _metric_conflicts = merge_normalized_metrics(
        result.metrics, normalize_tcode_results(gui_results, system=result.system)
    )
    for _note in _metric_conflicts:
        log.info(f"[{result.system}] metric reconciled -- {_note}")
    # Extra checks (collectors/extra_checks.py): certificate / licence expiry,
    # missed and overdue jobs, users, destinations, spool, transports, HANA.
    # Already graded. The last stored result (shared by every process) is used
    # when it is fresh; otherwise they run in the RFC worker process
    # (core/rfc_proc.py) -- never on this thread, which may be the web server's.
    if system_config.get("rfc") or system_config.get("hana"):
        try:
            from collectors import extra_checks as _xc
            _xm, _xage = _xc.stored(name) if _xc.enabled() else ([], None)
            if _xc.enabled() and (_xage is None or _xage > _xc.ttl_seconds()):
                from collectors.checks_job import collect_checks_job
                from core import rfc_proc
                _limit = _env_seconds("IBO_SWEEP_EXTRA_CHECKS_WAIT", 150)
                try:
                    # Waited for WITH heartbeats and a bound: the background
                    # worker may be busy with other systems' checks, and a
                    # silent 600 s wait made the watchdog kill the sweep as
                    # "hung" at 180 s -- after every screenshot was taken.
                    _res = _wait_beating(lambda: rfc_proc.call(collect_checks_job, name, system_config,
                                                               False, timeout=_limit + 5),
                                         f"{name}:extra-checks", _limit)
                    _xm = [_xc._from_json(d) for d in _res.get("metrics") or []]
                except TimeoutError:
                    log.warning(f"{name}: extra checks did not finish within {_limit}s; using the last "
                                f"stored values ({'none' if _xage is None else f'{_xage / 60:.0f} min old'}).")
            _have = {m.name for m in result.metrics}
            result.metrics.extend(m for m in _xm if m.name not in _have)
        except Exception as _exc:  # noqa: BLE001
            log.warning(f"{name}: extra checks skipped: {type(_exc).__name__}: {_exc}")
    heartbeat.beat(f"{name}:grading")
    result.metrics = evaluate_all(result.metrics, thresholds, system=result.system)
    heartbeat.beat(f"{name}:learned-baseline")
    # Learned normal values (core/learned_baseline.py): "unusual for this
    # system at this time of day". Annotates, and in the default "smart"
    # mode turns a WARNING that is normal for this hour back to NORMAL and
    # an unusual reading into a WARNING. CRITICAL is never touched.
    try:
        from core.learned_baseline import apply_to_metrics as _apply_learned
        _apply_learned(name, result.metrics)
    except Exception as _exc:  # noqa: BLE001 -- never fail a sweep over this
        log.warning(f"{name}: learned baseline skipped: {type(_exc).__name__}: {_exc}")
    # Sweep readings feed the same history as live reads, so baselines and
    # capacity forecasts also learn from GUI-only systems.
    heartbeat.beat(f"{name}:history")
    try:
        from core.metric_history import append_metrics as _append_history
        _append_history(name, result.metrics)
    except Exception:
        pass
    result.compute_overall_status()
    heartbeat.beat(f"{name}:alerts")
    # Alerts chosen per value on the Thresholds page (core/alert_engine.py),
    # also for the values only a sweep reads (SM21, SQLM, ST03, ASE, ...).
    try:
        from core import alert_engine
        alert_engine.observe(system_config, alert_engine.items_from_metrics(result.metrics),
                             source="sweep")
    except Exception as _exc:  # noqa: BLE001 -- a sweep must never fail on alerting
        log.warning(f"{name}: alert check failed: {type(_exc).__name__}: {_exc}")
    heartbeat.beat(f"{name}:events")
    result.events = EventEngine(result.system, result.client).process_metrics(result.metrics)
    # Per-item detail for the correlation evidence: which job failed, run by
    # whom, for how long -- and which accounts locked, and why. A count tells
    # nobody what to do; "DBA:DATABACKUP ended at its start time, so it never
    # ran" does.
    _detail = {}
    try:
        _live = _live_payload_for_sweep(name, system_config) or {}
        _detail = {"job_detail": _live.get("job_detail") or {},
                   "locked_user_detail": _live.get("locked_user_detail") or {}}
    except Exception as _exc:
        # Detail is an enrichment. Losing it must not cost the incident.
        log.warning(f"{name}: could not read job/user detail: {type(_exc).__name__}: {_exc}")
    heartbeat.beat(f"{name}:correlation")
    result.incidents = CorrelationEngine(result.system, result.client).correlate(
        result.metrics, result.events, detail=_detail
    )

    # Tell whoever scheduled a failed or long-running job, from the SAME
    # mailbox the Basis alerts use. Off unless NOTIFY_JOB_OWNERS is true;
    # system accounts are never mailed; one mail per job per day.
    # Owner mails are skipped inside a maintenance window (core/alert_control.py),
    # like every other alert.
    try:
        from core.alert_control import in_maintenance as _in_maint
        _quiet = _in_maint(name)
    except Exception:
        _quiet = False
    # Both are mail deliveries: queued for the outbox (sent after the data
    # is saved, outside the watched section). A notification problem must
    # never fail a sweep: the metrics, the snapshot and the report are the
    # product.
    if _quiet:
        log.info(f"{name}: job/dump owner notifications skipped -- in maintenance window.")
    else:
        def _job_owner_mail(job_detail=dict(_detail.get("job_detail") or {})):
            from notifications.job_owner import notify_job_owners
            _owner = notify_job_owners(name, job_detail, get_smtp_config(system_config))
            if _owner.get("sent"):
                log.info(f"{name}: notified {_owner['sent']} job owner(s).")
            return True

        # Tell the team that owns a memory-dump spike -- ABAP, Basis or
        # functional, decided by SAP_MEMORY_DUMP_ATTRIBUTION. Same guards as
        # job owners: opt-in, Basis always copied, once per culprit per day.
        def _dump_owner_mail(incidents=list(result.incidents or [])):
            from notifications.dump_owner import notify_dump_owner
            _dump = notify_dump_owner(name, incidents, get_smtp_config(system_config))
            if _dump.get("sent"):
                log.info(f"{name}: notified {_dump['sent']} dump owner team(s).")
            return True
        _deliveries.append(("job_owner", _job_owner_mail, "job owner notification"))
        _deliveries.append(("dump_owner", _dump_owner_mail, "dump owner notification"))

    heartbeat.beat(f"{name}:ai-analysis")
    # Operational intelligence is evaluated only after the complete metric and
    # incident picture exists. The runtime is per-system, so repeated scheduler
    # cycles build baseline history without mixing systems.
    _ai_async = os.environ.get("IBO_AI_ASYNC", "0").strip().lower() in ("1", "true", "yes")
    if not _ai_enabled():
        log.info(f"{name}: AI analysis disabled (IBO_ENABLE_AI=0); skipping.")
        result.ai_analysis = None
    elif _ai_async:
        # Hand the model call to the jobs service so a slow or failing
        # provider never holds the sweep (and the SAP GUI desktop) hostage.
        # The result lands in the snapshot, not in this cycle's PDF.
        result.ai_analysis = None
        try:
            from core import store as _store
            _store.enqueue("ai_analysis", {"system": name}, delay_seconds=5)
            log.info(f"{name}: AI analysis queued for the jobs service (IBO_AI_ASYNC=1).")
        except Exception as _exc:
            log.warning(f"{name}: could not queue AI analysis: {_exc}")
    else:
        _ai_box: dict = {}

        def _ai_work():
            try:
                intelligence_runtime = get_intelligence_runtime(result.system)
                intelligence = attach_operational_intelligence(result, intelligence_runtime)
                _ai_box["value"] = run_ai_analysis(result, intelligence=intelligence)
            except Exception as e:  # noqa: BLE001
                _ai_box["error"] = e

        # The model call has a time budget of its own. With slow or exhausted
        # AI keys (several providers x retries x 60 s) this stage alone went
        # past the 180 s hang detector, which then killed SAP processes for a
        # stage that does not even use SAP GUI. Waiting here beats the
        # heartbeat; past the budget the report goes out without AI text.
        try:
            _ai_budget = max(int(float(os.environ.get("IBO_AI_BUDGET_SECONDS", "120") or 120)), 10)
        except (TypeError, ValueError):
            _ai_budget = 120
        # copy_context: the AI thread grades with THIS system's thresholds too.
        import contextvars as _cv
        _ai_thread = threading.Thread(target=_cv.copy_context().run, args=(_ai_work,), daemon=True,
                                      name=f"ai-{name}")
        _ai_started = time.monotonic()
        _ai_thread.start()
        while _ai_thread.is_alive() and time.monotonic() - _ai_started < _ai_budget:
            _ai_thread.join(timeout=10)
            heartbeat.beat(f"{name}:ai-analysis")
        if _ai_thread.is_alive():
            log.warning(f"{name}: AI analysis did not finish within {_ai_budget}s (AI provider slow or "
                        f"keys exhausted); report sent without AI text. Switch AI analysis off on the "
                        f"Settings page if no paid AI key is used.")
            result.ai_analysis = None
            result.errors.append(f"ai_analyzer: no answer within {_ai_budget}s")
        elif "error" in _ai_box:
            e = _ai_box["error"]
            log.error(f"AI/intelligence analysis failed after final metric collection: {type(e).__name__}")
            result.errors.append(f"ai_analyzer: {type(e).__name__}")
        else:
            analyses = _ai_box.get("value")
            result.ai_analysis = analyses[0] if analyses else None

    if result.overall_status.value == "CRITICAL":
        def _critical_mail():
            return send_critical_alert(result, get_smtp_config(system_config))
        _deliveries.append(("critical_alert", _critical_mail, "CRITICAL alert"))

    _checkpoint(name, "snapshot")
    save_snapshot(result, gui_results=gui_results, system_name=name)

    _checkpoint(name, "reports")
    # The sheet and the PDF list this run's T-codes, in the system's order.
    result.tcodes_this_run = list(system_config.get("_tcodes_this_run") or [])
    report_paths = generate_system_reports(result, gui_results=gui_results)
    pdf_path = report_paths["pdf"]
    excel_history_path = report_paths["excel"]
    metrobrands_path = fill_metrobrands_template(
        template_path=TEMPLATE_PATH,
        output_path=str(system_template_path(name, result.cycle_timestamp)),
        gui_results=gui_results,
        result=result,
    )

    # Per-system alerting: a PRD alert should not land in a sandbox inbox
    # just because both use the same mail server.
    # The sweep's own SMLG reading is a trigger source, not only the
    # dashboard's live poll.
    try:
        if not _stop_requested():
            _rca_from_sweep(name, system_config, gui_results)
    except Exception as exc:   # noqa: BLE001 -- never fail a sweep over this
        log.warning(f"{name}: RCA trigger from sweep skipped: {type(exc).__name__}: {exc}")

    _checkpoint(name, "delivery-decision")
    # Nothing measured: no SAP GUI session and no RFC metrics. The 14:07 PS4
    # run emailed a full "report" with 0 metrics and stale screens carried
    # forward from an earlier run. No report then -- and no failure mail
    # either: a DOWN system is parked (below) and the reachability check
    # mails SYSTEM DOWN once, with its own de-duplication.
    nothing_collected = not gui_available and not result.metrics
    # The monitoring sheet goes to the client only when the run was complete
    # (SAP GUI logon, every screenshot, every sheet row filled) --
    # core/report_gate.py. Otherwise it is held (logged, timeline, run
    # history -- no "REPORT HELD" mail) and, where it can help, retried.
    _gate = None
    _stopped = _stop_requested()
    try:
        from core import report_gate as _rg
        if _rg.enabled() and not _stopped:
            _gate = _rg.check(system_config, gui_available, gui_skip_reason, gui_results, metrobrands_path)
    except Exception as _exc:  # noqa: BLE001 -- a broken gate must not send a broken report
        log.error(f"{name}: report completeness check failed: {_exc}")
        _gate = {"complete": False, "reasons": [f"completeness check failed: {_exc}"], "retry": True,
                 "missing": []}
    _stopped = _stopped or _stop_requested()        # Stop pressed during the check above
    _checkpoint(name, "delivery-decision")
    if _gate is not None and _gate.get("complete") and not _stopped:
        # Complete: the retry chain ends here, whatever happens to delivery
        # (no channel, WhatsApp only, mail off or failing) -- review 28.10.2026.
        try:
            from core import report_gate as _rg
            _rg.clear(name)
        except Exception:  # noqa: BLE001
            pass
    if _stopped:
        log.warning(f"{name}: stopped from the dashboard -- no monitoring sheet sent, no retry.")
        _ctx_now.update(result="stopped-by-user", failed_stage="delivery-decision",
                        failure_reason="stopped from the dashboard")
        _deliveries = []
        try:
            from core import report_gate as _rg
            _rg.clear(name)
        except Exception:
            pass
    elif nothing_collected:
        _handle_nothing_collected(system_config, result, _ctx_now)
    elif _gate is not None and not _gate["complete"]:
        _held = _hold_report_quietly(system_config, _gate, _ctx_now)
        if _held.get("send_partial") and not _report_allowed(_ctx_now):
            # Partial send chosen, but no screenshot at all: nothing can go out.
            # Tell Basis instead of ending silently (test review 28.10.2026).
            from core import report_gate as _rg
            from datetime import datetime as _dtn
            _ctx_now["report"] = "not sent: last try incomplete and no screenshot captured"
            try:
                _rg._tell_basis(system_config, _gate, None, _rg.retry_max() + 1, _dtn.now())
            except Exception as _e:  # noqa: BLE001
                log.error(f"{name}: 'report not sent' mail to Basis failed: {_e}")
        elif _held.get("send_partial") and _report_allowed(_ctx_now):
            # Last try, logon fine, only a few checks missing: send it, naming them.
            from core import report_gate as _rg
            _deliveries += _report_deliveries(system_config, result, gui_results, pdf_path,
                                              metrobrands_path, True, _ctx_now,
                                              note_html=_rg.partial_note(_gate))
            _ctx_now["report"] = ("sent with " + str(len(_gate.get("missing") or [])) + " check(s) missing: "
                                  + ", ".join(_gate.get("missing") or []))
    elif not _report_allowed(_ctx_now):
        log.warning(f"{name}: monitoring report NOT sent -- no SAP GUI screenshot was captured in this run "
                    f"(IBO_REPORT_REQUIRE_SCREENSHOTS=1).")
        _ctx_now["report"] = "not sent: no screenshots captured"
    else:
        _deliveries += _report_deliveries(system_config, result, gui_results, pdf_path,
                                          metrobrands_path, _gate is not None, _ctx_now)

    # The watched part of the run ends here: data, snapshot and reports are
    # saved. Deliveries go to the outbox and are sent by the delivery worker,
    # each with its own time budget; a slow or failing mail server can no
    # longer look like a hung SAP GUI.
    if _deliveries:
        from core import outbox
        for kind, fn, what in _deliveries:
            outbox.enqueue(kind, name, fn, run_id=_ctx_now.get("run_id"), description=what)
        _ctx_now["deliveries"] = [k for k, _f, _w in _deliveries]

    log.info(f"===== Pipeline COMPLETE for system {name} (gui_available={gui_available}) =====")

    # Acknowledged / ignored-until-fixed alerts whose problem this sweep no
    # longer sees are cleared, so the next occurrence alerts again.
    try:
        from core.alert_control import clear_until_fixed as _clear_ack
        _cleared = {"sweep_failure"} if not nothing_collected else set()
        if not nothing_collected and result.overall_status.value != "CRITICAL":
            _cleared.add("critical")
        if gui_available and not [r for r in gui_results if r.display_value == "failed"]:
            _cleared.add("tcode_failure")
        if _cleared:
            _clear_ack(name, _cleared)
    except Exception:
        pass

    # Close SAP Logon / SAP GUI after a SUCCESSFUL run too. It was only ever
    # killed on failure or at the start of the next run, so a good sweep
    # left the GUI sitting on the last T-code (ST06 on the operator's
    # screen) until the next cycle -- holding a session and a licence, and
    # confusing whoever sits at the desktop. Best-effort; a close failing
    # must not turn a completed sweep into a failed one.
    if gui_available and os.environ.get("IBO_GUI_CLOSE_AFTER_SWEEP", "1").strip() not in ("0", "false", "no"):
        if _kill_sap(name, "after the sweep"):
            log.info(f"{name}: SAP GUI closed after successful sweep.")
    if _stopped:
        return False
    _ctx_now.setdefault("result", "parked" if _ctx_now.get("parked") else
                        "failed" if nothing_collected else "success")
    if nothing_collected:
        return False
    return True


def _count_screenshots(gui_results) -> int:
    """SAP GUI screenshots this run actually captured (files that exist)."""
    n = 0
    for r in gui_results or []:
        if not str(getattr(r, "name", "") or "").startswith("screenshot_"):
            continue
        if not str(getattr(r, "display_value", "") or "").startswith("captured"):
            continue
        paths = list(getattr(r, "screenshot_paths", None) or [])
        if getattr(r, "screenshot_path", None):
            paths.append(r.screenshot_path)
        if any(p and os.path.exists(p) for p in paths):
            n += 1
    return n


def _report_allowed(ctx: dict) -> bool:
    """The report is sent only when SAP GUI screenshots were captured
    (IBO_REPORT_REQUIRE_SCREENSHOTS=0 restores the old behaviour)."""
    if os.getenv("IBO_REPORT_REQUIRE_SCREENSHOTS", "1").strip().lower() in ("0", "false", "no", "off"):
        return True
    return int(ctx.get("screenshots_captured") or 0) > 0


def _park_states() -> set:
    raw = os.getenv("IBO_SWEEP_PARK_STATES", "system_down")
    return {x.strip().lower().replace(" ", "_") for x in raw.split(",") if x.strip()}


def _handle_nothing_collected(system_config: dict, result, ctx: dict) -> None:
    """
    No SAP GUI and no metrics. No mail per run (the old failure notice went
    out every cycle for a DOWN system). When the reachability check says the
    system is DOWN (IBO_SWEEP_PARK_STATES, default system_down), its sweeps
    are PARKED -- the existing park flag (core/alert_control.py): scheduled
    sweeps stop until someone clears it on the wall. An automatic DOWN park
    is never cleared automatically (only credential parks are).
    """
    name = system_config.get("name", "")
    reason = "; ".join(str(e) for e in (result.errors or [])) or "SAP GUI unavailable, no RFC metrics"
    ctx["failed_stage"] = ctx.get("failed_stage") or "monitoring-cycle"
    ctx["failure_reason"] = f"no monitoring data collected: {reason}"[:600]
    state = None
    try:
        from core import reachability
        state = (reachability.current_failures().get(name) or {}).get("state")
    except Exception:  # noqa: BLE001
        state = None
    if state and str(state).lower().replace(" ", "_") in _park_states():
        try:
            from core import alert_control
            alert_control.park(name, reason=f"automatic: {state} (sweep collected nothing)",
                               by="system", auto=True)
            ctx["parked"] = True
            ctx["result"] = "parked"
            ctx["next_run_reason"] = "parked until cleared"
            log.warning(f"{name}: {state} -- no data collected; scheduled sweeps PARKED until someone "
                        f"clears the flag on the wall. No failure mail is sent.")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning(f"{name}: could not park the system: {exc}")
    log.warning(f"{name}: no data collected this cycle ({reason}); no report sent (logged, not mailed).")


def _hold_report_quietly(system_config: dict, gate: dict, ctx: dict) -> dict:
    """
    The run was incomplete: the sheet is NOT sent yet. While another automatic
    try is left it is planned quietly (Timeline, run history). After the LAST
    try: a run that only misses a few checks is sent anyway (send_partial,
    report_gate.partial_ok); otherwise Basis gets ONE "not sent" mail saying
    why -- a held sheet is never lost silently again (PS4, 28.10.2026 12:00).
    """
    from datetime import datetime as _dt
    from core import report_gate as _rg
    name = system_config.get("name", "")
    reasons = "; ".join(gate.get("reasons") or []) or "run incomplete"
    why = "; ".join(f"{t}: {r}" for t, r in (gate.get("details") or {}).items())
    due, attempt = None, 1
    try:
        attempt = _rg.next_attempt(name, manual=_rg.manual_run())
        if gate.get("retry") and _rg.scheduler_alive():
            due = _rg.plan_retry(name, attempt, reasons + (f" ({why})" if why else ""),
                                 profile_id=system_config.get("_profile_id"))
        else:
            _rg.clear(name)
    except Exception as exc:  # noqa: BLE001
        log.error(f"{name}: report retry not planned: {exc}")
    ctx["report"] = "held: " + (reasons + (f" -- why: {why}" if why else ""))[:600]
    if due is None:
        if _rg.partial_ok(gate):
            log.warning(f"{name}: last try still misses {gate.get('missing')} -- sending the report "
                        f"with those checks named.")
            try:
                from core import timeline
                timeline.add(name, "sweep", "Monitoring sheet sent with checks missing",
                             f"after {attempt} tries: {', '.join(gate.get('missing') or [])}", severity="warning")
            except Exception:  # noqa: BLE001
                pass
            return {"due": None, "send_partial": True}
        try:
            _rg._tell_basis(system_config, gate, None, attempt, _dt.now())
        except Exception as exc:  # noqa: BLE001
            log.error(f"{name}: 'report not sent' mail to Basis failed: {exc}")
    if due:
        ctx["next_run_at"] = due.isoformat(timespec="seconds")
        ctx["next_run_reason"] = f"retry in {max(0, int((due - _dt.now()).total_seconds()))}s"
    try:
        from core import timeline
        timeline.add(name, "sweep", "Monitoring sheet held back (run incomplete)",
                     reasons[:900] + (f"; retry at {due:%H:%M}" if due else "; not retried"),
                     severity="warning")
    except Exception:  # noqa: BLE001
        pass
    log.warning(f"{name}: monitoring sheet NOT sent to the client: {reasons}"
                + (f" -- retry at {due:%H:%M}" if due else " -- not retried automatically"))
    return {"due": due, "send_partial": False}


def _run_profile(system_config: dict) -> dict | None:
    """The monitoring profile that started this run, if any."""
    pid = system_config.get("_profile_id")
    if not pid:
        return None
    try:
        from core.profiles import load_profiles
        return next((p for p in load_profiles() if p.get("id") == pid), None)
    except Exception:  # noqa: BLE001
        return None


def _report_deliveries(system_config: dict, result, gui_results, pdf_path, sheet_path,
                       gated: bool, ctx: dict, note_html: str = "") -> list:
    """
    The report deliveries of a complete run, honouring the profile that
    started it: its `deliver` channels (email / whatsapp; none = nothing
    sent) and its own email_to / email_cc. A run without a profile (manual,
    Scheduler page, retry) mails the report as before.
    """
    name = system_config.get("name", "")
    profile = _run_profile(system_config)
    channels = ["email"] if profile is None else list(profile.get("deliver") or [])
    out = []
    if profile is not None and not channels:
        log.info(f"{name}: profile {profile['id']} has no delivery channel -- report saved, not sent.")
        ctx["report"] = f"not sent: profile {profile['id']} has no delivery channel"
    if "email" in channels:
        if _email_enabled():
            def _report_mail():
                smtp_config = get_smtp_config(system_config)
                if profile is not None and profile.get("email_to"):
                    smtp_config = dict(smtp_config, to_emails=list(profile["email_to"]))
                if profile is not None and profile.get("email_cc"):
                    smtp_config = dict(smtp_config, cc_emails=list(profile["email_cc"]))
                sent = (send_final_report(result, smtp_config, pdf_path, sheet_path, note_html=note_html)
                        if note_html else send_final_report(result, smtp_config, pdf_path, sheet_path))
                if sent and gated:
                    try:
                        from core import report_gate as _rg
                        _rg.sent(system_config, result.cycle_timestamp,
                                 list(smtp_config.get("to_emails") or []))
                    except Exception as e:  # noqa: BLE001
                        log.error(f"{name}: report-sent notice failed: {e}")
                return sent
            out.append(("final_report", _report_mail, "monitoring report (PDF + sheet)"))
        else:
            log.info(f"{name}: final report email disabled (IBO_ENABLE_EMAIL=0); skipping.")
    if "whatsapp" in channels:
        evidence = []
        for r in gui_results or []:
            path = getattr(r, "screenshot_path", None)
            if path:
                evidence.append({"tcode": getattr(r, "tcode", ""), "screenshot": path})
        summary = f"{profile.get('label') or profile['id']} · {result.overall_status.value}"

        def _whatsapp():
            from notifications.whatsapp import send_screenshots
            outcome = send_screenshots(name, evidence, summary=summary)
            if not outcome.get("sent"):
                log.info(f"{name}: WhatsApp not sent: {outcome.get('reason', 'see log')}")
            return bool(outcome.get("sent"))
        out.append(("whatsapp", _whatsapp, f"{len(evidence)} screenshot(s)"))
    return out


def run_all_configured_systems():
    """
    Runs the monitoring pipeline for every system listed in
    config/systems.yaml, one after another (sequentially, not in
    parallel -- SAP Logon/GUI scripting only supports one active
    session at a time on this machine). Used as the default entry
    point for `python main.py` / run_pipeline.bat, so a single
    scheduled task (e.g. every 2 hours via Windows Task Scheduler)
    covers all configured systems instead of just one.
    """
    systems = get_systems()
    if not systems:
        log.error("No systems configured in config/systems.yaml. Nothing to run.")
        sys.exit(1)

    log.info(f"Running pipeline for {len(systems)} configured system(s): "
              f"{', '.join(s['name'] for s in systems)}")

    from datetime import datetime as _dt
    from core import run_history
    for system_config in systems:
        started, ok, err = _dt.now(), False, None
        try:
            ok = bool(run_pipeline_for_system(system_config))
        except Exception as e:
            err = e
            log.error(f"Unhandled error running pipeline for {system_config['name']}: {e}")
            # Continue to the next system rather than aborting the whole batch.
        run_history.record_run(system_config, trigger="cli", started_at=started, ok=ok, error=err,
                               note="python main.py")

    # Deliveries run on the outbox worker; wait for them before the process
    # (and its daemon worker thread) exits.
    from core import outbox
    if not outbox.drain(timeout=float(os.getenv("IBO_DELIVERY_DRAIN_SECONDS", "900") or 900)):
        log.warning(f"{outbox.pending()} delivery(ies) still pending at exit; see dashboard/outbox.")
    log.info("===== All configured systems complete =====")


if __name__ == "__main__":
    try:
        from core import errors as _errors
        _errors.install()
    except Exception:
        pass
    run_all_configured_systems()
