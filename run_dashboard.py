r"""
IBOPS dashboard launcher - prevents orphaned worker processes.

Use this instead of:  python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8000
Run it as:            .venv\Scripts\python.exe run_dashboard.py

What it guarantees (no admin rights needed):
  1. Job Object with "kill on close": every worker process the dashboard starts is
     placed in a Windows job. When the dashboard process ends - normally, by crash,
     by Task Manager, by Stop-Process - Windows kills all its workers automatically.
     Workers can no longer outlive the server and hold SAP RFC sessions.
  2. Startup cleanup: before starting, it ends any worker processes left orphaned
     by earlier runs (python multiprocessing workers whose parent is gone).
  3. Periodic cleanup: every 5 minutes it repeats that check, as a safety net.

Place this file in the repo root (next to the 'dashboard' folder).
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8000
APP = "dashboard.app:app"
SWEEP_EVERY_SEC = 300
MIN_ORPHAN_AGE_SEC = 60

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
log = logging.getLogger("ibops.launcher")

_job_handle = None  # must stay referenced for the life of the process


def _setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_DIR / "dashboard_launcher.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


# ---------------------------------------------------------------- job object
def put_self_in_kill_on_close_job() -> bool:
    """Assign this process (and so all future children) to a kill-on-close job."""
    global _job_handle
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        log.warning("CreateJobObject failed (error %s)", ctypes.get_last_error())
        return False

    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)):
        log.warning("SetInformationJobObject failed (error %s)", ctypes.get_last_error())
        return False

    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        log.warning("AssignProcessToJobObject failed (error %s) - "
                    "relying on periodic cleanup only", ctypes.get_last_error())
        return False

    _job_handle = job
    log.info("kill-on-close job active: workers will end with this process")
    return True


# ---------------------------------------------------------------- orphan sweep
_PS_FIND_ORPHANS = r"""
$all = Get-CimInstance Win32_Process
$byId = @{}
foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
$now = Get-Date
$out = foreach ($p in $all) {
  if ($p.Name -ne 'python.exe' -or $p.CommandLine -notlike '*multiprocessing-fork*') { continue }
  $parent = $byId[[int]$p.ParentProcessId]
  # orphan = parent gone, or the PID was reused by a process newer than the child
  $orphan = (-not $parent) -or ($parent.CreationDate -gt $p.CreationDate)
  $age = ($now - $p.CreationDate).TotalSeconds
  if ($orphan) { [pscustomobject]@{ pid = [int]$p.ProcessId; age = [int]$age } }
}
@($out) | ConvertTo-Json -Compress
"""


def sweep_orphans() -> int:
    """End orphaned python multiprocessing workers. Returns how many were ended."""
    if os.name != "nt":
        return 0
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_FIND_ORPHANS],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = (res.stdout or "").strip()
        if not raw:
            return 0
        found = json.loads(raw)
        if isinstance(found, dict):
            found = [found]
    except Exception as exc:  # never let the sweep break the dashboard
        log.warning("orphan sweep failed: %s", exc)
        return 0

    ended = 0
    for item in found:
        pid, age = int(item.get("pid", 0)), int(item.get("age", 0))
        if pid <= 0 or pid == os.getpid() or age < MIN_ORPHAN_AGE_SEC:
            continue
        try:
            os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
            ended += 1
        except OSError as exc:
            log.warning("could not end orphan pid %s: %s", pid, exc)
    if ended:
        log.info("ended %d orphaned worker process(es)", ended)
    return ended


def _periodic_sweep() -> None:
    while True:
        time.sleep(SWEEP_EVERY_SEC)
        sweep_orphans()


# ---------------------------------------------------------------- main
def main() -> None:
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    _setup_logging()
    log.info("launcher starting (pid %s)", os.getpid())

    n = sweep_orphans()
    log.info("startup cleanup: %d orphan(s) ended", n)

    put_self_in_kill_on_close_job()
    threading.Thread(target=_periodic_sweep, name="orphan-sweep", daemon=True).start()

    import uvicorn
    from dashboard import serve
    serve._load_env()
    # 127.0.0.1 unless IBO_BIND_HOST opts in to the network (dashboard/serve.py).
    host, port = serve.bind_host(), serve.bind_port(PORT)
    tls = serve.tls_options()            # HTTPS when IBO_TLS_CERTFILE/KEYFILE are set
    if tls:
        serve.start_http_redirect(host, port)
    log.info("dashboard on %s://%s:%s", "https" if tls else "http", host, port)
    uvicorn.run(APP, host=host, port=port,
                proxy_headers=serve.trust_proxy(),
                forwarded_allow_ips=os.getenv("IBO_TRUSTED_PROXIES", "127.0.0.1"), **tls)
    log.info("dashboard stopped")


if __name__ == "__main__":  # required: Windows workers re-import this file
    main()
