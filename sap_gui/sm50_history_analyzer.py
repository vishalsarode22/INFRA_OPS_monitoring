import os
import json
from datetime import datetime


def load_snapshots(
    system,
    client,
    history_root="reports/sm50_history",
):
    prefix = f"{system}_{client}_"

    if not os.path.exists(history_root):
        return []

    files = [
        f
        for f in os.listdir(history_root)
        if f.startswith(prefix) and f.endswith(".json")
    ]

    files.sort()

    snapshots = []

    for filename in files:
        path = os.path.join(history_root, filename)

        try:
            with open(path, "r", encoding="utf-8") as f:
                snapshots.append(json.load(f))
        except Exception:
            continue

    return snapshots


def _parse_timestamp(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _parse_hms(value):
    """
    Parse SAP time values such as:

        00:55:41
        01:02:15

    Returns seconds.
    """

    if not value:
        return None

    try:
        parts = str(value).strip().split(":")

        if len(parts) != 3:
            return None

        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])

        return (
            hours * 3600
            + minutes * 60
            + seconds
        )

    except (TypeError, ValueError):
        return None


def analyze_persistent_running(
    snapshots,
    minimum_samples=3,
):
    if len(snapshots) < minimum_samples:
        return []

    running_history = {}

    for snapshot in snapshots:

        timestamp = snapshot.get("timestamp")

        for process in snapshot.get("processes", []):

            if process.get("state") != "Running":
                continue

            pid = str(
                process.get("pid", "")
            ).strip()

            if not pid:
                continue

            running_history.setdefault(
                pid,
                []
            ).append({
                "timestamp": timestamp,
                "wp_index": process.get("wp_index"),
                "wp_type": process.get("wp_type"),
                "state": process.get("state"),
                "cpu": process.get("cpu"),
                "elapsed_time": process.get(
                    "elapsed_time"
                ),
                "program": process.get("program"),
                "user": process.get("user"),
                "client": process.get("client"),
                "priority": process.get(
                    "priority"
                ),
                "current_action": process.get(
                    "current_action"
                ),
            })

    findings = []

    for pid, samples in running_history.items():

        if len(samples) < minimum_samples:
            continue

        first = samples[0]
        latest = samples[-1]

        first_timestamp = _parse_timestamp(
            first["timestamp"]
        )

        latest_timestamp = _parse_timestamp(
            latest["timestamp"]
        )

        monitoring_seconds = None

        if first_timestamp and latest_timestamp:
            monitoring_seconds = (
                latest_timestamp
                - first_timestamp
            ).total_seconds()

        # -----------------------------------------------------
        # CPU progression
        # -----------------------------------------------------

        cpu_values = []

        for sample in samples:
            cpu_seconds = _parse_hms(
                sample.get("cpu")
            )

            if cpu_seconds is not None:
                cpu_values.append(cpu_seconds)

        cpu_growth_seconds = None

        if len(cpu_values) >= 2:
            cpu_growth_seconds = (
                cpu_values[-1]
                - cpu_values[0]
            )

        # -----------------------------------------------------
        # Program stability
        # -----------------------------------------------------

        programs = {
            str(s.get("program", "")).strip()
            for s in samples
            if str(s.get("program", "")).strip()
        }

        stable_program = (
            len(programs) == 1
        )

        # -----------------------------------------------------
        # Build finding
        # -----------------------------------------------------

        finding = {
            "severity": "WARNING",
            "category": "persistent_running_work_process",

            "reason": (
                "The same work process remained "
                "in Running state across multiple "
                "monitoring samples."
            ),

            "pid": pid,
            "samples": len(samples),

            "first_seen": first["timestamp"],
            "last_seen": latest["timestamp"],

            "monitoring_seconds":
                monitoring_seconds,

            "cpu_growth_seconds":
                cpu_growth_seconds,

            "stable_program":
                stable_program,

            "programs":
                sorted(programs),

            "process": latest,
        }

        findings.append(finding)

    return findings