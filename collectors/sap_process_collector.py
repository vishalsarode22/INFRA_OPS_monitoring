"""
SAP work process monitoring via sapcontrol.
Runs sapcontrol -nr <instance> -function GetProcessList over SSH on the SAP host.
Reuses the same SSH connection approach as linux_collector.py.
"""

import re
import paramiko

from utils.logger import get_logger
from core.models import MetricResult, Status

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


def _run_with_stderr(client: paramiko.SSHClient, command: str) -> tuple[str, str]:
    """Runs a command and returns (stdout, stderr), both stripped."""
    _, stdout, stderr = client.exec_command(command)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if err:
        log.debug(f"stderr from {command!r}: {err[:200]}")
    return out, err


def collect_sap_process_list(host: str, port: int, username: str, password: str,
                             instance_nr: str, sid: str = "", sid_adm: str = None) -> dict:
    """
    Returns raw sapcontrol GetProcessList output.

    Tries several invocations because a bare `sapcontrol` is almost never on
    root's PATH. The previous version ran exactly one command, logged
    "collected successfully" regardless, and returned an empty string -- so
    the caller saw a success message followed by "no process rows could be
    parsed", with no clue that the command had simply not been found.

    Order:
      1. su - <sid>adm  -- the account whose profile sets the SAP environment
      2. /usr/sap/hostctrl/exe/sapcontrol  -- host agent, present on most hosts
      3. /usr/sap/<SID>/<prefix><nr>/exe/sapcontrol  -- instance-local binary

    stderr is captured and returned so a permission or path problem is
    visible instead of silently becoming an empty result.
    """
    client = None
    sid = str(sid or "").strip().upper()
    admin = (sid_adm or (f"{sid.lower()}adm" if sid else "")).strip()
    nr = str(instance_nr).zfill(2)

    try:
        client = _connect(host, port, username, password)
    except Exception as e:
        log.error(f"SSH connection failed (sap_process_collector): {e}")
        return {"error": str(e)}

    commands = []
    if admin:
        commands.append(f"su - {admin} -c 'sapcontrol -nr {nr} -function GetProcessList'")
    commands.append(f"/usr/sap/hostctrl/exe/sapcontrol -nr {nr} -function GetProcessList")
    if sid:
        for prefix in ("D", "DVEBMGS", "ASCS", "J"):
            commands.append(
                f"/usr/sap/{sid}/{prefix}{nr}/exe/sapcontrol "
                f"-nr {nr} -function GetProcessList")
    commands.append(f"sapcontrol -nr {nr} -function GetProcessList")

    attempts = []
    try:
        for command in commands:
            out, err = _run_with_stderr(client, command)
            if out and "GetProcessList" in out:
                log.info(f"SAP process list collected via: {command.split()[0]}")
                return {"raw": out, "command": command}
            attempts.append(f"{command.split()[0]} -> {(err or out or 'empty').splitlines()[0][:90]}"
                            if (err or out) else f"{command.split()[0]} -> empty")

        detail = " | ".join(attempts[:4])
        log.error(f"sapcontrol produced no usable output. Tried: {detail}")
        return {"error": f"sapcontrol not usable over SSH. Tried: {detail}"}
    except Exception as e:
        log.error(f"Error collecting SAP process list: {e}")
        return {"error": str(e)}
    finally:
        if client:
            client.close()



def parse_sap_process_list(raw_result: dict) -> list[MetricResult]:
    """
    Parses sapcontrol GetProcessList output into MetricResult objects.

    Handles BOTH output shapes, because which one you get depends on the
    kernel release, the locale and whether -format script was used:

      CSV (default)
        name, description, dispstatus, textstatus, starttime, elapsedtime, pid
        disp+work, Dispatcher, GREEN, Running, 2026 08 31 06:12:04, 6:44:02, 4711

      script (-format script)
        0 name: disp+work
        0 dispstatus: GREEN
        0 textstatus: Running

    The previous version required a header line beginning with "name," and
    returned NOTHING when it was absent -- which is why TST logged
    "collected successfully" and then "Could not find header line", losing
    the dispatcher, ICM and gateway states even though sapcontrol had run
    fine. It now finds data rows by their content instead of trusting a
    header to exist.

    Status: GREEN -> NORMAL, YELLOW -> WARNING, RED -> CRITICAL,
    GRAY/anything else -> UNKNOWN (never NORMAL: an unreadable process
    state must not read as healthy).
    """
    results: list[MetricResult] = []

    if "error" in raw_result:
        log.error(f"Skipping parse -- collector reported error: {raw_result['error']}")
        return results

    raw = raw_result.get("raw", "") or ""
    lines = [ln.rstrip() for ln in raw.splitlines()]

    status_map = {
        "GREEN": Status.NORMAL,
        "YELLOW": Status.WARNING,
        "RED": Status.CRITICAL,
        "GRAY": Status.UNKNOWN,
        "GREY": Status.UNKNOWN,
    }
    COLOURS = set(status_map)

    def add(name: str, colour: str, text: str, extra: dict | None = None):
        colour = (colour or "").strip().upper()
        results.append(MetricResult(
            name=f"sap_process_{name.strip()}",
            value=None,
            display_value=colour or "UNKNOWN",
            status=status_map.get(colour, Status.UNKNOWN),
            source="sap_process_collector",
            detail=(text or "").strip(),
            extra_data=extra or {},
        ))

    # ---- shape 2: "-format script" (key: value, prefixed by an index) ----
    if any(re.match(r"^\s*\d+\s+name\s*:", ln) for ln in lines):
        current: dict = {}
        for line in lines:
            m = re.match(r"^\s*(\d+)\s+(\w+)\s*:\s*(.*)$", line)
            if not m:
                continue
            index, key, value = m.group(1), m.group(2).lower(), m.group(3).strip()
            if key == "name" and current.get("name"):
                add(current.get("name", "?"), current.get("dispstatus", ""),
                    current.get("textstatus", ""))
                current = {}
            current[key] = value
        if current.get("name"):
            add(current["name"], current.get("dispstatus", ""), current.get("textstatus", ""))
        if results:
            log.info(f"Parsed {len(results)} SAP process statuses (script format).")
            return results

    # ---- shape 1: CSV, with or without a header ----
    for line in lines:
        if "," not in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        if parts[0].lower() == "name":          # header row
            continue
        # A data row is identified by carrying a status colour, not by its
        # position relative to a header that may not be there.
        colour_at = next((i for i, p in enumerate(parts) if p.upper() in COLOURS), None)
        if colour_at is None or not parts[0]:
            continue
        add(parts[0], parts[colour_at],
            parts[colour_at + 1] if len(parts) > colour_at + 1 else "")

    if results:
        log.info(f"Parsed {len(results)} SAP process statuses.")
    else:
        # Log a bounded sample so an unfamiliar format can be diagnosed
        # without another round trip.
        sample = " | ".join(ln.strip() for ln in lines[:6] if ln.strip())
        log.error(
            "sapcontrol returned output but no process rows could be parsed. "
            f"First lines: {sample[:400]}"
        )
    return results
