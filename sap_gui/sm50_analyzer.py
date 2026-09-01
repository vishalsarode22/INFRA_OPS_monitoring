"""
SM50 diagnostic analyzer.

Consumes the structured SM50 data produced by action_sm50()
and converts observations into monitoring findings.

This module does NOT interact with SAP GUI.
"""

from typing import Any, Dict, List


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def analyze_sm50(extracted_data: Dict[str, Any]) -> Dict[str, Any]:
    processes = extracted_data.get("processes", [])

    findings: List[Dict[str, Any]] = []

    state_counts = extracted_data.get("state_counts", {})

    total = len(processes)
    running = _to_int(extracted_data.get("running_processes"))
    waiting = _to_int(extracted_data.get("waiting_processes"))
    stopped = _to_int(extracted_data.get("stopped_processes"))
    held = _to_int(extracted_data.get("held_processes"))

    # ---------------------------------------------------------
    # Process-level observations
    # ---------------------------------------------------------

    for process in processes:
        state = process.get("state", "").strip()
        state_info = process.get("state_info", "").strip()
        failures = process.get("failures", "").strip()
        sem_locked = process.get("sem_locked", "").strip()
        sem_locking = process.get("sem_locking", "").strip()

        finding_base = {
            "wp_index": process.get("wp_index", ""),
            "pid": process.get("pid", ""),
            "wp_type": process.get("wp_type", ""),
            "state": state,
            "program": process.get("program", ""),
            "user": process.get("user", ""),
            "client": process.get("client", ""),
        }

        # Explicit failure information
        if failures:
            findings.append({
                **finding_base,
                "severity": "CRITICAL",
                "category": "work_process_failure",
                "reason": "SM50 reports a work-process failure indicator.",
                "evidence": {
                    "failures": failures,
                },
            })

        # Semaphore information
        if sem_locked or sem_locking:
            findings.append({
                **finding_base,
                "severity": "WARNING",
                "category": "semaphore",
                "reason": "SM50 reports semaphore locking information.",
                "evidence": {
                    "sem_locked": sem_locked,
                    "sem_locking": sem_locking,
                },
            })

        # State information
        if state_info:
            findings.append({
                **finding_base,
                "severity": "INFO",
                "category": "state_information",
                "reason": "SM50 provides additional state information.",
                "evidence": {
                    "state_info": state_info,
                },
            })

        # Held / stopped processes are important observations.
        if state == "Stopped":
            findings.append({
                **finding_base,
                "severity": "CRITICAL",
                "category": "stopped_work_process",
                "reason": "A work process is in Stopped state.",
                "evidence": {
                    "state": state,
                },
            })

        elif state == "Held":
            findings.append({
                **finding_base,
                "severity": "WARNING",
                "category": "held_work_process",
                "reason": "A work process is in Held state.",
                "evidence": {
                    "state": state,
                },
            })

    # ---------------------------------------------------------
    # Overall health
    # ---------------------------------------------------------

    if stopped > 0:
        health = "CRITICAL"
    elif held > 0:
        health = "WARNING"
    elif findings:
        health = "WARNING"
    else:
        health = "HEALTHY"

    return {
        "health": health,
        "summary": {
            "total": total,
            "running": running,
            "waiting": waiting,
            "stopped": stopped,
            "held": held,
            "state_counts": state_counts,
        },
        "findings": findings,
    }