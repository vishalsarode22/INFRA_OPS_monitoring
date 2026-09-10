"""
Orchestrator: runs one full monitoring cycle for a single SAP system/client.
Combines all collectors (Linux, SAP process, ...) into a single MonitoringResult.
Each collector is isolated -- if one fails, others still run and the failure
is recorded in MonitoringResult.errors rather than crashing the cycle.

Linux/SAP-process (OS-level) metrics are only collected if ssh_creds with
a host is provided. Systems without SSH/OS access simply skip that section
cleanly, without erroring -- SAP GUI T-code monitoring (handled separately
in main.py) still runs regardless.
"""

from core.models import MonitoringResult
from core.config_loader import (
    get_linux_ssh_credentials,
    get_sap_instance_nr,
    get_thresholds,
    get_smtp_config,
)
from collectors.linux_collector import collect_linux_metrics, parse_linux_metrics
from collectors.sap_process_collector import collect_sap_process_list, parse_sap_process_list
from collectors.rfc_collector import collect_rfc_metrics
from collectors.ase_collector import collect_ase_metrics
from collectors.rfc_perf import collect_perf_metrics
from evaluation.threshold_engine import evaluate_all
from evaluation.ai_analyzer import analyze as run_ai_analysis
from notifications.email_alert import send_critical_alert
from utils.logger import get_logger

log = get_logger(__name__, "monitoring")


def run_monitoring_cycle(system: str, client: str, ssh_creds: dict = None, instance_nr: str = None, run_ai: bool = True, send_alert: bool = True, system_cfg: dict = None) -> MonitoringResult:
    """
    Runs one monitoring cycle: collects Linux + SAP process metrics
    (if SSH access is available), evaluates thresholds, optionally runs AI,
    sends alert if critical, and returns a single MonitoringResult.

    The full GUI pipeline passes run_ai=False and send_alert=False so T-code
    metrics can be normalized and included before final health/alert/AI decisions.

    ssh_creds: optional per-system SSH connection details
    (host/port/username/password).
      - If None: falls back to .env-based single-system credentials
        (original single-system behavior, e.g. python main.py).
      - If a dict with an empty/missing "host": OS-level collection is
        skipped entirely (system has GUI-only access, no SSH).
      - If a dict with a real "host": used directly for OS-level collection.

    instance_nr: SAP instance number for the SAP-process collector.
      - If None: falls back to the .env SAP_INSTANCE_NR (single-system
        default). Multi-system callers should pass each system's own
        instance number from config/systems.yaml.

    system_cfg: the full systems.yaml entry. When it carries an "rfc" block
      the RFC collector runs, giving T-code counters (ST22, SM12, SM13,
      SM37, SM58, SMQ1/2, SM21, AL08) without SAP GUI or SSH. This is the
      only path that works on RISE and hosted systems, where neither a
      Windows GUI host nor OS access is available.
    """
    result = MonitoringResult(system=system, client=client)
    thresholds = get_thresholds()

    has_ssh = ssh_creds is not None and bool(ssh_creds.get("host"))

    if not has_ssh and ssh_creds is None:
        # No explicit ssh_creds passed at all -- fall back to .env defaults
        try:
            ssh_creds = get_linux_ssh_credentials()
            has_ssh = True
        except Exception:
            has_ssh = False

    if has_ssh:
        # --- Linux metrics ---
        try:
            raw_linux = collect_linux_metrics(**ssh_creds)
            linux_metrics = parse_linux_metrics(raw_linux)
            linux_metrics = evaluate_all(linux_metrics, thresholds)
            result.metrics.extend(linux_metrics)
            if "error" in raw_linux:
                result.errors.append(f"linux_collector: {raw_linux['error']}")
        except Exception as e:
            log.error(f"Linux collector failed entirely: {e}")
            result.errors.append(f"linux_collector: {e}")

        # --- SAP process metrics ---
        try:
            resolved_instance_nr = instance_nr if instance_nr else get_sap_instance_nr()
            raw_sap = collect_sap_process_list(
                **ssh_creds,
                instance_nr=resolved_instance_nr,
                # sid lets the collector build the <sid>adm and instance-path
                # fallbacks; a bare `sapcontrol` is not on root's PATH.
                sid=(system_cfg or {}).get("sap_system_id") or system,
                sid_adm=(system_cfg or {}).get("sid_adm"),
            )
            sap_metrics = parse_sap_process_list(raw_sap)
            result.metrics.extend(sap_metrics)
            if "error" in raw_sap:
                result.errors.append(f"sap_process_collector: {raw_sap['error']}")
        except Exception as e:
            log.error(f"SAP process collector failed entirely: {e}")
            result.errors.append(f"sap_process_collector: {e}")
    else:
        log.info(f"No SSH access configured for {system} -- skipping Linux/OS-level metrics (SAP GUI evidence only).")

    # --- RFC metrics (headless; no SAP GUI, no OS access needed) ---
    #
    # Runs independently of SSH. A system with only an "rfc" block still
    # reports T-code counters. A failure here is recorded as an error and
    # leaves the metrics absent -- it must NEVER be reported as healthy.
    if system_cfg and system_cfg.get("rfc"):
        try:
            rfc_metrics, rfc_error = collect_rfc_metrics(system, system_cfg)
            if rfc_metrics:
                # RFC deliberately uses the SAME metric names as the SSH and
                # GUI collectors (cpu, memory, load_1m, sap.sm12.lock_count
                # ...) so one signal has one history. That means a system
                # with both SSH and RFC would otherwise report "cpu" twice.
                #
                # First reading wins. SSH runs before this and measures the
                # host directly, so it is the better source where present;
                # RFC fills only what SSH could not provide.
                seen = {m.name for m in result.metrics}
                fresh = [m for m in rfc_metrics if m.name not in seen]
                duplicates = len(rfc_metrics) - len(fresh)
                result.metrics.extend(fresh)
                log.info(
                    f"[{system}] RFC collector: {len(fresh)} metrics added"
                    + (f", {duplicates} already covered by another collector." if duplicates else ".")
                )
            if rfc_error:
                result.errors.append(f"rfc_collector: {rfc_error}")
                log.warning(f"[{system}] RFC collector: {rfc_error}")
        except Exception as e:
            log.error(f"RFC collector failed entirely: {e}")
            result.errors.append(f"rfc_collector: {e}")

    # --- RFC performance metrics (SM50/SM66/ST03N/SM12/SQLM, all instances) ---
    #
    # Second RFC logon per cycle, deliberately separate from the counters
    # above: this one is heavier (per-instance TH_WPINFO, STAT records,
    # SQLMD) and a failure here must not cost the T-code counters. Names
    # are new (sap.sm50.priv_mode_wp, sap.st03.dialog_resp_ms ...) so
    # nothing is deduped away.
    if system_cfg and system_cfg.get("rfc"):
        try:
            perf_metrics, perf_error = collect_perf_metrics(system, system_cfg)
            if perf_metrics:
                result.metrics.extend(perf_metrics)
                log.info(f"[{system}] RFC perf collector: {len(perf_metrics)} metrics added.")
            if perf_error:
                result.errors.append(f"rfc_perf: {perf_error}")
                log.warning(f"[{system}] RFC perf collector: {perf_error}")
        except Exception as e:
            log.error(f"RFC perf collector failed entirely: {e}")
            result.errors.append(f"rfc_perf: {e}")

    # --- Database metrics (ASE / Sybase MDA tables; the ST04 view) ---
    #
    # Reads the ASE monitoring tables directly with a read-only mon_role
    # login. Needs neither RFC nor OS access. db.ase.* names collide with
    # nothing else, so no dedupe against earlier collectors is needed.
    # Same contract as RFC: a failure is an error entry, never a healthy 0.
    if system_cfg and system_cfg.get("db"):
        try:
            db_metrics, db_error = collect_ase_metrics(system, system_cfg)
            if db_metrics:
                result.metrics.extend(db_metrics)
                log.info(f"[{system}] ASE collector: {len(db_metrics)} metrics added.")
            if db_error:
                result.errors.append(f"ase_collector: {db_error}")
                log.warning(f"[{system}] ASE collector: {db_error}")
        except Exception as e:
            log.error(f"ASE collector failed entirely: {e}")
            result.errors.append(f"ase_collector: {e}")

    result.compute_overall_status()

    log.info(
        f"Monitoring cycle complete for {system}/{client}: "
        f"overall={result.overall_status.value}, "
        f"metrics={len(result.metrics)}, errors={len(result.errors)}"
    )

    # --- Immediate alert on CRITICAL ---
    if send_alert and result.overall_status.value == "CRITICAL":
        try:
            smtp_config = get_smtp_config()
            send_critical_alert(result, smtp_config)
        except Exception as e:
            log.error(f"Failed to send critical alert: {e}")
            result.errors.append(f"email_alert: {e}")

    # --- Optional AI analysis ---
    if run_ai:
        try:
            result.ai_analysis = run_ai_analysis(result)
        except Exception as e:
            log.error(f"AI analysis step failed entirely: {e}")
            result.errors.append(f"ai_analyzer: {e}")

    return result