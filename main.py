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
from sap_gui.launcher import kill_sap_processes, launch_saplogon, select_connection
from sap_gui.connection import login
from sap_gui.scripting_connection import get_scripting_session
from core.orchestrator import run_monitoring_cycle
from core.tcode_metrics import normalize_tcode_results
from core.event_engine import EventEngine
from core.correlation import CorrelationEngine
from evaluation.threshold_engine import evaluate_all
from evaluation.ai_analyzer import analyze_incidents as run_ai_analysis
from collectors.sap_gui_collector import collect_tcode_evidence
from reporting.latex_report_builder import generate_latex_pdf_report
from reporting.excel_writer import append_result_to_excel
from reporting.excel_template_writer import fill_metrobrands_template
from notifications.email_report import send_final_report, send_failure_alert, send_tcode_failure_alert
from notifications.email_alert import send_critical_alert
from core.status_snapshot import save_snapshot
from core.intelligence_runtime import get_intelligence_runtime, attach_operational_intelligence
from utils.logger import get_logger

log = get_logger(__name__, "application")

TEMPLATE_PATH = "config/templates/MetroBrands_template.xlsx"


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

    tasks = get_monitoring_tasks()
    gui_results = collect_tcode_evidence(
        tasks,
        system=system,
        client=client,
    )

    # Make SAP GUI evidence available to the AI analysis layer.
    result.gui_results = gui_results

    # Finalize the complete result only after both OS and SAP GUI evidence are available.
    thresholds = get_thresholds()
    result.metrics.extend(normalize_tcode_results(gui_results))
    result.metrics = evaluate_all(result.metrics, thresholds)
    result.compute_overall_status()
    result.events = EventEngine(result.system, result.client).process_metrics(result.metrics)
    result.incidents = CorrelationEngine(result.system, result.client).correlate(
        result.metrics, result.events
    )

    # Operational intelligence is evaluated only after the complete metric and
    # incident picture exists. The runtime is per-system, so repeated scheduler
    # cycles build baseline history without mixing systems.
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

    smtp_config = get_smtp_config()
    send_final_report(result, smtp_config, pdf_path, metrobrands_path)

    log.info("===== SAP BASIS Monitoring Pipeline: COMPLETE =====")
    print(f"\nPDF (LaTeX): {pdf_path}")
    print(f"Excel history: {excel_history_path}")
    print(f"MetroBrands report: {metrobrands_path}")


SYSTEM_MAX_ATTEMPTS = 2  # 1 initial try + 1 full retry, per system, per cycle
PIPELINE_TIMEOUT_SECONDS = 5 * 60  # TEMPORARY: lowered for testing the watchdog -- raise back to 20*60 for normal runs, a full TST cycle (GUI+OS) can take longer than 5 minutes


def _run_with_watchdog(system_config: dict) -> tuple:
    """
    Runs _run_pipeline_for_system_once() in a background thread with a
    hard timeout, because some failures don't raise a Python exception
    at all -- they just block forever (e.g. a modal SAP dialog left in
    a state that makes a COM call hang instead of erroring out, which
    can happen if someone manually interferes with the SAP GUI window
    mid-run). Retrying only helps for failures that actually raise; a
    genuine hang needs something external to break it, since Python
    cannot forcibly interrupt a blocked COM call from another thread.

    If the thread hasn't finished within PIPELINE_TIMEOUT_SECONDS, this
    force-kills the SAP processes (which unblocks whatever the thread
    was stuck on, usually causing it to error out on its own shortly
    after) and returns immediately as a failure, WITHOUT waiting for
    the stuck thread to actually exit -- otherwise a single hang would
    freeze the scheduler forever, since run_pipeline_for_system's caller
    (the dashboard scheduler) can't proceed to the next cycle until this
    returns. The old thread is abandoned; it may log a delayed error
    later once its blocked call finally unblocks, which is harmless.

    Returns (success: bool, result_or_error).
    """
    result_box = {}

    def target():
        try:
            result_box["value"] = _run_pipeline_for_system_once(system_config)
        except Exception as e:
            result_box["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=PIPELINE_TIMEOUT_SECONDS)

    if thread.is_alive():
        name = system_config["name"]
        log.error(
            f"Pipeline for {name} appears stuck -- no progress within "
            f"{PIPELINE_TIMEOUT_SECONDS}s. Force-killing SAP processes to "
            f"unblock it and moving on (this attempt counts as failed)."
        )
        try:
            kill_sap_processes()
        except Exception:
            pass
        return False, TimeoutError(f"Pipeline for {name} did not complete within {PIPELINE_TIMEOUT_SECONDS}s (hung).")

    if "error" in result_box:
        raise result_box["error"]
    return True, result_box.get("value", True)


def run_pipeline_for_system(system_config: dict) -> bool:
    system_template = system_template_path(system_config.get("name", "UNKNOWN"))

    system_excel = system_excel_path(system_config.get('name', 'UNKNOWN'))

    system_pdf = system_pdf_path(system_config.get('name', 'UNKNOWN'))

    """
    Runs the full pipeline for one system, with automatic recovery from
    a catastrophic failure (SAP Logon crashing, login never completing,
    an unhandled exception anywhere in the run, OR the run simply
    hanging with no exception at all -- see _run_with_watchdog above):
    on failure, sends an immediate email alert, resets state (kills any
    stray SAP processes), and retries the entire system once more
    before giving up for this cycle. This is separate from the smaller,
    per-T-code retry inside collect_tcode_evidence() -- this one is for
    failures big enough that the whole run needs to start over, not
    just one T-code.

    Returns True if the pipeline completed (with or without a retry),
    False if all attempts were exhausted.
    """
    name = system_config["name"]
    last_error = None

    for attempt in range(1, SYSTEM_MAX_ATTEMPTS + 1):
        try:
            success, value = _run_with_watchdog(system_config)
            if success:
                return value
            last_error = value  # the TimeoutError from the watchdog
        except Exception as e:
            last_error = e
            log.error(f"Pipeline attempt {attempt}/{SYSTEM_MAX_ATTEMPTS} for {name} failed: {e}", exc_info=True)

        try:
            smtp_config = get_smtp_config()
            send_failure_alert(name, str(last_error), attempt, SYSTEM_MAX_ATTEMPTS, smtp_config)
        except Exception as alert_err:
            log.error(f"Could not send failure alert for {name} (SMTP config issue?): {alert_err}")

        if attempt < SYSTEM_MAX_ATTEMPTS:
            log.info(f"Resetting SAP processes before retrying {name}...")
            try:
                kill_sap_processes()
            except Exception:
                pass
            time.sleep(5)

    log.error(f"All {SYSTEM_MAX_ATTEMPTS} attempts exhausted for {name} -- skipping this system for this cycle. "
              f"Last error: {last_error}")
    return False


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
    log.info(f"===== Pipeline START for system {name} =====")

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
    session = None

    if has_gui_access:
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
        try:
            kill_sap_processes()

            launch_config = get_launch_config(
                connection_name=system_config["connection_name"]
            )

            launch_saplogon(launch_config["exe_path"])
            select_connection(launch_config["connection_name"])
            time.sleep(2)

            login_result = login(
                client=system_config["client"], username=system_config["username"],
                password=system_config["password"], language=system_config.get("language", "EN"),
                submit=True, verify=True,
            )
            if not login_result["success"]:
                log.warning(f"{name}: SAP GUI login failed -- continuing with RFC/SSH collectors.")
                gui_available = False
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
        except Exception as e:
            log.warning(
                f"{name}: SAP GUI unavailable ({type(e).__name__}: {e}). "
                f"Continuing with RFC/SSH collectors."
            )
            gui_available = False
    else:
        log.info(f"{name}: has_gui_access=false -- skipping SAP GUI login and T-code evidence entirely (not attempting).")
        gui_available = False

    # --- Monitoring cycle: OS metrics run only if has_os_access and ssh_creds has a host ---
    instance_nr = system_config.get("sap_instance_nr") or None
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

    if gui_available:
        tasks = get_monitoring_tasks()
        gui_results = collect_tcode_evidence(
            tasks,
            system=system_config.get("name", "UNKNOWN"),
            client=system_config.get("client", "UNKNOWN"),
        )

        # Make SAP GUI evidence available to the AI analysis layer.
        result.gui_results = gui_results

        failed_tcodes = [
            r.tcode
            for r in gui_results
            if r.display_value == "failed"
        ]
        if failed_tcodes:
            log.warning(f"{name}: {len(failed_tcodes)} T-code(s) failed to capture: {failed_tcodes}")
            try:
                send_tcode_failure_alert(name, failed_tcodes, get_smtp_config(system_config))
            except Exception as e:
                log.error(f"Could not send T-code failure alert for {name}: {e}")
    else:
        log.info(f"Skipping T-code GUI evidence for {name} -- GUI access disabled or no active SAP session.")

    # Finalize only after OS and GUI collectors have both contributed.
    thresholds = get_thresholds()
    result.metrics.extend(normalize_tcode_results(gui_results))
    result.metrics = evaluate_all(result.metrics, thresholds)
    result.compute_overall_status()
    result.events = EventEngine(result.system, result.client).process_metrics(result.metrics)
    result.incidents = CorrelationEngine(result.system, result.client).correlate(
        result.metrics, result.events
    )

    # Operational intelligence is evaluated only after the complete metric and
    # incident picture exists. The runtime is per-system, so repeated scheduler
    # cycles build baseline history without mixing systems.
    try:
        intelligence_runtime = get_intelligence_runtime(result.system)
        intelligence = attach_operational_intelligence(result, intelligence_runtime)
        analyses = run_ai_analysis(result, intelligence=intelligence)
        result.ai_analysis = analyses[0] if analyses else None
    except Exception as e:
        log.error(f"AI/intelligence analysis failed after final metric collection: {type(e).__name__}")
        result.errors.append(f"ai_analyzer: {type(e).__name__}")

    if result.overall_status.value == "CRITICAL":
        try:
            send_critical_alert(result, get_smtp_config(system_config))
        except Exception as e:
            log.error(f"Failed to send final critical alert for {name}: {e}")
            result.errors.append(f"email_alert: {e}")

    save_snapshot(result, gui_results=gui_results, system_name=name)

    report_paths = generate_system_reports(result, gui_results=gui_results)
    pdf_path = report_paths["pdf"]
    excel_history_path = report_paths["excel"]
    metrobrands_path = fill_metrobrands_template(
        template_path=TEMPLATE_PATH,
        output_path=str(system_template_path(name, result.cycle_timestamp)),
        gui_results=gui_results,
    )

    # Per-system alerting: a PRD alert should not land in a sandbox inbox
    # just because both use the same mail server.
    smtp_config = get_smtp_config(system_config)
    send_final_report(result, smtp_config, pdf_path, metrobrands_path)

    log.info(f"===== Pipeline COMPLETE for system {name} (gui_available={gui_available}) =====")
    return True


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

    for system_config in systems:
        try:
            run_pipeline_for_system(system_config)
        except Exception as e:
            log.error(f"Unhandled error running pipeline for {system_config['name']}: {e}")
            # Continue to the next system rather than aborting the whole batch.

    log.info("===== All configured systems complete =====")


if __name__ == "__main__":
    run_all_configured_systems()