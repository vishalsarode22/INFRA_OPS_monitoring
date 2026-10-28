"""Shared bootstrap for the three services: env, heartbeat, clean shutdown."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

# Make `python -m services.x` work from any directory and keep BASE_DIR
# (utils/paths.py uses os.getcwd()) pointing at the project root.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from utils.logger import get_logger  # noqa: E402

# Real-time exception handling for every service process (core/errors.py).
try:
    from core import errors as _errors  # noqa: E402
    _errors.install()
except Exception:  # noqa: BLE001 -- never stop a service from starting over this
    pass

HEARTBEAT_SECONDS = 30
_stop = threading.Event()


def stopping() -> bool:
    return _stop.is_set()


def install_signal_handlers(log) -> None:
    def _handler(signum, _frame):
        log.info(f"signal {signum}: stopping")
        _stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass          # not the main thread, or unsupported on this platform


def start_heartbeat(service: str, detail_fn=None) -> threading.Thread:
    """Touch the heartbeats table every 30 s until stop. Never raises."""
    from core import store
    log = get_logger(f"services.{service}")

    def _loop():
        while not _stop.is_set():
            try:
                store.beat(service, (detail_fn() if detail_fn else "") or "")
            except Exception as exc:
                log.warning(f"heartbeat failed: {exc}")
            _stop.wait(HEARTBEAT_SECONDS)

    t = threading.Thread(target=_loop, name=f"heartbeat-{service}", daemon=True)
    t.start()
    return t


def wait_forever() -> None:
    while not _stop.is_set():
        time.sleep(1)
