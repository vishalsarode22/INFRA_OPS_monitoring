"""
Run one live RFC read in a WORKER PROCESS.

Why a process and not a thread: the timing middleware showed the whole
server freezing for 30-60s at a time -- a static JPEG took 30546 ms and
/api/live (which only copies a dict) took 62396 ms, exactly spanning RFC
read bursts. That pattern means the interpreter itself was stopped: on this
pyrfc/NW RFC SDK build, RFC calls hold the GIL for their full duration, so
while one thread waits on SAP no Python runs anywhere in the process --
web server included. No amount of threading fixes that; a separate process
with its own GIL does.

This module is imported by the worker processes. It must therefore have NO
import-time side effects and must NOT import dashboard.app (which starts
schedulers). Workers are long-lived, so rfc_live's module-level caches
(perf TTL, deep TTL, SQLM-off, cooldowns) persist inside them exactly as
they did in the server process.
"""

from __future__ import annotations


def read_live_job(system_name: str, cfg: dict) -> dict:
    """Top-level (picklable) entry point for a ProcessPoolExecutor."""
    from collectors.rfc_live import read_live, attach_snapshot_extras
    from core.status_snapshot import load_snapshot

    payload = read_live(system_name, cfg, use_cache=False)
    try:
        payload = attach_snapshot_extras(payload, load_snapshot(system_name))
    except Exception:  # noqa: BLE001 -- extras are enrichment only
        pass
    return payload
