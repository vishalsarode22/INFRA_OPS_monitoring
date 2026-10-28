"""
Delivery outbox: report mails, alert mails and WhatsApp messages leave the
sweep through here, on a separate delivery worker thread.

WHY
The final report used to be mailed INSIDE the pipeline thread the watchdog
watches: SMTP with a 60 s timeout x 2 tries plus sleeps, and no heartbeat
meanwhile. A slow mail server therefore looked exactly like a hung SAP GUI
-- the watchdog force-killed SAP and the whole system was swept again, for
a problem that had nothing to do with SAP.

Now the sweep's watched section ends once the data, the snapshot and the
reports are saved. Each delivery is put in the outbox and the pipeline
returns. The delivery worker sends them one at a time, each with its own
time budget (IBO_DELIVERY_TIMEOUT_SECONDS, default 300). A delivery that
fails or times out is logged and recorded -- it never kills SAP and never
retries the sweep. (Retry policy inside a mail send belongs to
notifications/.)

FILES
    dashboard/outbox/<id>.json   (IBO_OUTBOX_DIR overrides)
One small JSON file per delivery: kind, system, run_id, status
(queued | sending | sent | failed | timeout | lost), attempts, error, times.
The work itself (the bound send call, holding the in-memory report) lives
in this process; an entry still "queued"/"sending" when the process starts
again is marked "lost" and logged, so nothing disappears silently. Files
older than IBO_OUTBOX_KEEP_DAYS (default 7) are deleted.

    outbox.enqueue("final_report", "PRD", lambda: send_final_report(...), run_id=...)
    outbox.drain(timeout=600)     # python main.py waits before it exits

LATE FINISH
A delivery marked "timeout" keeps running in the background; when it
finishes later its entry is updated to "sent" / "failed" (late=True).

REPORT DURING A MAIL OUTAGE
A report (kind final_report / report) that fails while the sending account
is suspended after a quota answer (notifications/mailer.py) is not dropped:
it is re-queued for one minute after the suspension ends, at most
IBO_REPORT_OUTAGE_RETRIES times (default 3). Its entry shows status
"retry-scheduled", next_attempt_at and retries. The retry lives in this
process: if the process stops first, the next start marks it "lost".
"""
from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTBOX_DIR: str | None = None        # tests may set this

_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
_lock = threading.Lock()
_worker: dict = {"thread": None, "recovered": set()}
_pending = {"n": 0}
_timers: dict = {}                 # entry id -> threading.Timer of a scheduled retry

# Deliveries re-queued when they fail during a mail outage.
OUTAGE_RETRY_KINDS = frozenset({"final_report", "report"})
_idle = threading.Condition(_lock)
_pytest_dir: dict = {}


def outbox_dir() -> str:
    if OUTBOX_DIR:
        return OUTBOX_DIR
    env = (os.getenv("IBO_OUTBOX_DIR") or "").strip()
    if env:
        return env
    if "PYTEST_CURRENT_TEST" in os.environ:
        d = _pytest_dir.get("dir")
        if d is None:
            d = _pytest_dir["dir"] = tempfile.mkdtemp(prefix="ibo_outbox_")
        return d
    return os.path.join(_BASE, "dashboard", "outbox")


def timeout_seconds() -> float:
    try:
        return max(5.0, float(os.getenv("IBO_DELIVERY_TIMEOUT_SECONDS", "300") or 300))
    except ValueError:
        return 300.0


def _path(entry_id: str) -> str:
    return os.path.join(outbox_dir(), f"{entry_id}.json")


def _write(entry: dict) -> None:
    try:
        from utils.atomic import atomic_write_json_file
        atomic_write_json_file(_path(entry["id"]), entry)
    except Exception as exc:  # noqa: BLE001 -- bookkeeping must not stop a delivery
        log.debug(f"outbox: entry {entry.get('id')} not written: {exc}")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _recover_once() -> None:
    """Entries left queued/sending by a previous process are marked lost."""
    d = outbox_dir()
    if d in _worker["recovered"]:
        return
    _worker["recovered"].add(d)
    if not os.path.isdir(d):
        return
    keep = timedelta(days=_keep_days())
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(d, fn)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        if entry.get("status") in ("queued", "sending", "retry-scheduled"):
            entry["status"] = "lost"
            entry["error"] = "the process that queued it stopped before it was delivered"
            entry["finished_at"] = _now()
            log.warning(f"outbox: {entry.get('kind')} for {entry.get('system')} (run "
                        f"{entry.get('run_id')}) was never delivered -- the process stopped first.")
            _write(entry)
        else:
            try:
                if time.time() - os.path.getmtime(p) > keep.total_seconds():
                    os.unlink(p)
            except OSError:
                pass


def _keep_days() -> int:
    try:
        return max(1, int(os.getenv("IBO_OUTBOX_KEEP_DAYS", "7") or 7))
    except ValueError:
        return 7


def enqueue(kind: str, system: str, send, run_id: str | None = None,
            description: str = "", meta: dict | None = None) -> dict:
    """
    Queue one delivery. `send` is a no-argument callable that performs it
    and returns truthy on success (a falsy return counts as failed).
    Returns the outbox entry. Never raises.
    """
    entry = {"id": f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}",
             "kind": kind, "system": system, "run_id": run_id, "description": description,
             "status": "queued", "attempts": 0, "error": None, "queued_at": _now(),
             "finished_at": None, "meta": meta or {}}
    try:
        with _lock:
            _recover_once()
            _pending["n"] += 1
        _write(entry)
        _queue.put((entry, send))
        _ensure_worker()
    except Exception as exc:  # noqa: BLE001
        log.error(f"outbox: could not queue {kind} for {system}: {exc}")
    return entry


def _ensure_worker() -> None:
    with _lock:
        t = _worker["thread"]
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_run_worker, name="delivery-worker", daemon=True)
        _worker["thread"] = t
        t.start()


def outage_retries() -> int:
    try:
        return max(0, int(os.getenv("IBO_REPORT_OUTAGE_RETRIES", "3") or 3))
    except ValueError:
        return 3


def _outage_delay(reason: str) -> float | None:
    """Seconds until a report should be retried, or None when it is not a mail outage."""
    try:
        from notifications import mailer
        rec = mailer.suspension(None)
        if rec and rec.get("active"):
            return max(60.0, (rec["until_dt"] - datetime.now()).total_seconds() + 60.0)
        low = str(reason or "").lower()
        if "suppressed-mail-outage" in low or mailer._QUOTA.search(low):
            return mailer.suspend_minutes() * 60.0 + 60.0
    except Exception:  # noqa: BLE001
        return None
    return None


def _schedule_outage_retry(entry: dict, send, reason: str) -> bool:
    """Re-queue a report that failed in a mail outage. True when scheduled."""
    if entry.get("kind") not in OUTAGE_RETRY_KINDS:
        return False
    if int(entry.get("retries") or 0) >= outage_retries():
        return False
    delay = _outage_delay(reason)
    if delay is None:
        return False
    entry["retries"] = int(entry.get("retries") or 0) + 1
    entry["status"] = "retry-scheduled"
    entry["next_attempt_at"] = (datetime.now().astimezone()
                                + timedelta(seconds=delay)).isoformat(timespec="seconds")
    log.warning(f"outbox: {entry['kind']} for {entry['system']} not sent during a mail outage "
                f"({str(reason)[:160]}); retry {entry['retries']}/{outage_retries()} at "
                f"{entry['next_attempt_at']}.")

    def _requeue():
        _timers.pop(entry["id"], None)
        with _lock:
            _pending["n"] += 1
        entry["status"] = "queued"
        _write(entry)
        _queue.put((entry, send))
        _ensure_worker()

    t = threading.Timer(delay, _requeue)
    t.daemon = True
    _timers[entry["id"]] = t
    t.start()
    return True


def _deliver(entry: dict, send) -> None:
    entry["status"] = "sending"
    entry["attempts"] += 1
    entry["started_at"] = _now()
    _write(entry)
    box: dict = {}
    state = {"timed_out": False}
    state_lock = threading.Lock()

    def _reason() -> str:
        try:
            from notifications import mailer
            return mailer.last_reason()
        except Exception:  # noqa: BLE001
            return ""

    def _go():
        try:
            box["ok"] = send()
            box["reason"] = _reason()
        except BaseException as exc:  # noqa: BLE001 -- reported below
            box["error"] = f"{type(exc).__name__}: {exc}"
        with state_lock:
            state["done"] = True
            if not state["timed_out"]:
                return
            # The worker gave up waiting and marked it "timeout": record how
            # it really ended (under the lock, so "timeout" cannot overwrite it).
            entry["late"] = True
            entry["finished_at"] = _now()
            if "error" in box:
                entry["status"], entry["error"] = "failed", box["error"][:500]
            elif box.get("ok") is False:
                entry["status"] = "failed"
                entry["error"] = (box.get("reason") or
                                  "the sender reported failure (see the mail log)")[:500]
            else:
                entry["status"], entry["error"] = "sent", None
            log.info(f"outbox: {entry['kind']} for {entry['system']} finished late: "
                     f"{entry['status']}.")
            _write(entry)

    t = threading.Thread(target=_go, name=f"deliver-{entry['kind']}", daemon=True)
    t.start()
    t.join(timeout_seconds())
    with state_lock:
        if not state.get("done"):
            state["timed_out"] = True
            entry["status"] = "timeout"
            entry["error"] = f"no answer within {timeout_seconds():.0f}s"
            entry["finished_at"] = _now()
            log.warning(f"outbox: {entry['kind']} for {entry['system']} did not finish within "
                        f"{timeout_seconds():.0f}s -- left to finish in the background; the "
                        f"sweep is not affected (the entry is updated when it ends).")
            _write(entry)
            return
    if "error" in box:
        entry["status"] = "failed"
        entry["error"] = box["error"][:500]
        log.warning(f"outbox: {entry['kind']} for {entry['system']} failed: {entry['error']}")
    elif box.get("ok") is False:
        entry["status"] = "failed"
        entry["error"] = (box.get("reason") or "the sender reported failure (see the mail log)")[:500]
        log.warning(f"outbox: {entry['kind']} for {entry['system']} was not delivered.")
        if _schedule_outage_retry(entry, send, box.get("reason") or ""):
            _write(entry)
            return
    else:
        entry["status"] = "sent"
        entry["error"] = None
        entry.pop("next_attempt_at", None)
        log.info(f"outbox: {entry['kind']} for {entry['system']} delivered.")
    entry["finished_at"] = _now()
    _write(entry)


def _run_worker() -> None:
    while True:
        try:
            entry, send = _queue.get(timeout=60)
        except queue.Empty:
            with _lock:
                if _queue.empty():
                    _worker["thread"] = None
                    return
            continue
        try:
            _deliver(entry, send)
        except Exception as exc:  # noqa: BLE001 -- the worker must never die
            log.error(f"outbox: delivery crashed: {exc}")
        finally:
            with _lock:
                _pending["n"] = max(0, _pending["n"] - 1)
                _idle.notify_all()


def pending() -> int:
    with _lock:
        return _pending["n"]


def drain(timeout: float = 600.0) -> bool:
    """Wait until every queued delivery is done (or `timeout`). True when empty."""
    deadline = time.monotonic() + max(0.0, timeout)
    with _lock:
        while _pending["n"] > 0:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _idle.wait(min(left, 1.0))
    return True


def recent(limit: int = 50) -> list[dict]:
    d = outbox_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d), reverse=True)[: max(1, limit)]:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                out.append(json.load(fh))
        except Exception:  # noqa: BLE001
            continue
    return out
