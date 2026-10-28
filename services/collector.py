"""
Collector service: the scheduler and the sweeps, nothing else.

No HTTP server. It imports the scheduler loop that dashboard/app.py already
has (so the Profiles page and schedules keep their meaning) and runs it in
the foreground with IBO_ROLE=collector. The web service reads whatever
this writes through the shared database; it never sweeps.

On Windows this process drives SAP GUI, so it must run inside a logged-on
desktop session -- a Scheduled Task "at log on", not a Windows service
(services have no desktop). On Linux it is RFC-only and runs under systemd.
"""
from __future__ import annotations

import os
import threading

os.environ["IBO_ROLE"] = "collector"

from services import common  # noqa: E402  (sets cwd, loads .env)

log = common.get_logger("services.collector")


def _detail() -> str:
    try:
        from dashboard.app import _run_state, _scheduler_state
        if _run_state.get("running"):
            return f"sweeping {_run_state.get('current_system')}"
        return f"idle, next {_scheduler_state.get('next_run_at') or '-'}"
    except Exception:
        return ""


def main() -> None:
    common.install_signal_handlers(log)
    from core import store
    store.conn()                                   # create schema early, fail early
    from dashboard.app import _scheduler_loop, start_live_refresher
    common.start_heartbeat("collector", _detail)
    start_live_refresher()                         # on-demand unless IBO_LIVE_AUTO_REFRESH=1
    log.info("collector service started (IBO_ROLE=collector)")
    from core import supervisor
    supervisor.start("scheduler", _scheduler_loop)
    common.wait_forever()
    log.info("collector service stopped")


if __name__ == "__main__":
    main()
