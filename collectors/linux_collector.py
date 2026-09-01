"""
Linux server metrics collector via SSH (paramiko).
Connects to the SAP application server (nwtest) -- NOT the Windows dev machine.
Phase 2 scope: CPU, Memory, Swap, Disk, Load average.
No threshold evaluation here -- this module only returns raw collected values.
"""

import paramiko

from utils.logger import get_logger

log = get_logger(__name__, "monitoring")


def _connect(host: str, port: int, username: str, password: str, timeout: int = 10) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, port=port, username=username, password=password, timeout=timeout)
    return client


def _run(client: paramiko.SSHClient, command: str) -> str:
    stdin, stdout, stderr = client.exec_command(command)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if err:
        log.debug(f"Command produced stderr: {command} -> {err}")
    return out


def collect_linux_metrics(host: str, port: int, username: str, password: str) -> dict:
    """
    Returns a dict of raw Linux metrics. Does not evaluate thresholds.
    On connection failure, returns {"error": "..."} instead of raising,
    so the orchestrator can log it and continue with other collectors.
    """
    try:
        client = _connect(host, port, username, password)
    except Exception as e:
        log.error(f"Linux SSH connection failed: {e}")
        return {"error": str(e)}

    metrics = {}

    try:
        # --- CPU utilization (using top in batch mode, 1 iteration) ---
        cpu_raw = _run(client, "top -bn1 | grep '%Cpu'")
        metrics["cpu_raw"] = cpu_raw

        # --- Memory (free -m gives MB) ---
        mem_raw = _run(client, "free -m")
        metrics["memory_raw"] = mem_raw

        # --- Swap is included in free -m output, parsed separately later ---

        # --- Disk usage for key filesystems ---
        disk_raw = _run(client, "df -h / /usr/sap /sapmnt")
        metrics["disk_raw"] = disk_raw

        # --- Load average ---
        load_raw = _run(client, "cat /proc/loadavg")
        metrics["load_raw"] = load_raw

        log.info("Linux metrics collected successfully.")

    except Exception as e:
        log.error(f"Error while collecting Linux metrics: {e}")
        metrics["error"] = str(e)

    finally:
        client.close()

    return metrics

import re
from core.models import MetricResult, Status


def _parse_cpu(cpu_raw: str) -> float | None:
    """
    Input example:
    '%Cpu(s):  2.8 us,  2.8 sy,  0.0 ni, 94.4 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st'
    Returns CPU utilization % (100 - idle).
    """
    match = re.search(r"([\d.]+)\s*id", cpu_raw)
    if not match:
        return None
    idle = float(match.group(1))
    return round(100 - idle, 1)


def _parse_memory(memory_raw: str) -> dict:
    """
    Input example (free -m):
    '            total   used   free   shared  buff/cache  available'
    'Mem:        23773   4533   896    14022    18343       4827'
    'Swap:       26200   505    25695'
    Returns dict with memory_percent and swap_percent (used/total).
    """
    result = {"memory_percent": None, "swap_percent": None,
              "memory_total_mb": None, "memory_used_mb": None,
              "swap_total_mb": None, "swap_used_mb": None}

    for line in memory_raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0].startswith("Mem:") and len(parts) >= 3:
            total, used = float(parts[1]), float(parts[2])
            result["memory_total_mb"] = total
            result["memory_used_mb"] = used
            result["memory_percent"] = round((used / total) * 100, 1) if total else None
        elif parts[0].startswith("Swap:") and len(parts) >= 3:
            total, used = float(parts[1]), float(parts[2])
            result["swap_total_mb"] = total
            result["swap_used_mb"] = used
            result["swap_percent"] = round((used / total) * 100, 1) if total > 0 else 0.0

    return result


def _parse_disk(disk_raw: str) -> dict:
    """
    Input example (df -h):
    'Filesystem   Size  Used  Avail  Use%  Mounted on'
    '/dev/sda2     16G  8.5G   5.3G   62%  /'
    Returns dict keyed by mount point -> usage percent.
    """
    result = {}
    for line in disk_raw.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        if parts[0] == "Filesystem":  # header row
            continue
        use_percent_str = parts[4].replace("%", "")
        mount_point = parts[5]
        try:
            result[mount_point] = float(use_percent_str)
        except ValueError:
            continue
    return result


def _parse_load(load_raw: str) -> dict:
    """
    Input example (/proc/loadavg):
    '0.20 0.15 0.05 1/900 22561'
    """
    parts = load_raw.split()
    if len(parts) < 3:
        return {"load_1m": None, "load_5m": None, "load_15m": None}
    return {
        "load_1m": float(parts[0]),
        "load_5m": float(parts[1]),
        "load_15m": float(parts[2]),
    }


def parse_linux_metrics(raw: dict) -> list[MetricResult]:
    """
    Converts raw collected text into a list of MetricResult objects.
    Status is left as UNKNOWN here -- threshold evaluation happens
    in evaluation/threshold_engine.py (Phase 4), not here.
    """
    results = []

    if "error" in raw:
        log.error(f"Skipping parse -- collector reported error: {raw['error']}")
        return results

    cpu_percent = _parse_cpu(raw.get("cpu_raw", ""))
    if cpu_percent is not None:
        results.append(MetricResult(
            name="cpu", value=cpu_percent, display_value=f"{cpu_percent}%",
            status=Status.UNKNOWN, source="linux_collector"
        ))

    mem = _parse_memory(raw.get("memory_raw", ""))
    if mem["memory_percent"] is not None:
        results.append(MetricResult(
            name="memory", value=mem["memory_percent"], display_value=f"{mem['memory_percent']}%",
            status=Status.UNKNOWN, source="linux_collector",
            detail=f"{mem['memory_used_mb']}MB / {mem['memory_total_mb']}MB"
        ))
    if mem["swap_percent"] is not None:
        results.append(MetricResult(
            name="swap", value=mem["swap_percent"], display_value=f"{mem['swap_percent']}%",
            status=Status.UNKNOWN, source="linux_collector",
            detail=f"{mem['swap_used_mb']}MB / {mem['swap_total_mb']}MB"
        ))

    disks = _parse_disk(raw.get("disk_raw", ""))
    for mount, percent in disks.items():
        results.append(MetricResult(
            name=f"disk_{mount}", value=percent, display_value=f"{percent}%",
            status=Status.UNKNOWN, source="linux_collector", detail=f"mount={mount}"
        ))

    load = _parse_load(raw.get("load_raw", ""))
    if load["load_1m"] is not None:
        results.append(MetricResult(
            name="load_1m", value=load["load_1m"], display_value=str(load["load_1m"]),
            status=Status.UNKNOWN, source="linux_collector"
        ))

    log.info(f"Parsed {len(results)} Linux metrics.")
    return results