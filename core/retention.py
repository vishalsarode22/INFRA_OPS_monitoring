"""
Delete what we should not still be holding.

reports/<YYYY-MM-DD>/<SYSTEM>/ fills with screenshots of SM12, SM66, SM37
and the rest -- production usernames, lock owners, job names -- plus the
PDF and Excel sent to the customer. Logs rotate and metric history prunes
after twelve months; this directory grew 93 MB in three weeks with no
limit at all, on a VM that also holds every client's credentials.

Two windows, both in days, both in .env:

    IBO_EVIDENCE_RETENTION_DAYS   default 14   screenshots / OCR evidence
    IBO_REPORT_RETENTION_DAYS     default 60   PDF, Excel, everything else
                                               under a dated folder

Evidence is removed first because it is the sensitive part; the PDF the
customer already received can stay longer for reference. The Excel
HISTORY workbook (one per system, appended every sweep) is not under a
dated folder and is never touched. sm50_history/ follows the report
window.

Runs from the scheduler once a day (dashboard/app.py) and on demand:
    python -m core.retention            # report what would go, delete it
    python -m core.retention --dry-run  # report only
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime, timedelta

from utils.logger import get_logger
from utils.paths import BASE_DIR

log = get_logger(__name__)

REPORTS_DIR = os.path.join(BASE_DIR, "reports")
_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _days(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def plan(now: datetime | None = None) -> dict:
    """What would be deleted, without deleting anything."""
    now = now or datetime.now()
    evidence_cutoff = now - timedelta(days=_days("IBO_EVIDENCE_RETENTION_DAYS", 14))
    report_cutoff = now - timedelta(days=_days("IBO_REPORT_RETENTION_DAYS", 60))
    out = {"evidence_dirs": [], "report_days": [], "sm50_files": [], "bytes": 0}
    if not os.path.isdir(REPORTS_DIR):
        return out
    for day in sorted(os.listdir(REPORTS_DIR)):
        day_path = os.path.join(REPORTS_DIR, day)
        if not os.path.isdir(day_path):
            continue
        if day == "sm50_history":
            for root, _d, files in os.walk(day_path):
                for f in files:
                    p = os.path.join(root, f)
                    try:
                        if datetime.fromtimestamp(os.path.getmtime(p)) < report_cutoff:
                            out["sm50_files"].append(p)
                            out["bytes"] += os.path.getsize(p)
                    except OSError:
                        pass
            continue
        if not _DATED.match(day):
            continue
        try:
            stamp = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            continue
        if stamp < report_cutoff:
            out["report_days"].append(day_path)
            out["bytes"] += _dir_size(day_path)
        elif stamp < evidence_cutoff:
            for system in os.listdir(day_path):
                ev = os.path.join(day_path, system, "evidence")
                if os.path.isdir(ev):
                    out["evidence_dirs"].append(ev)
                    out["bytes"] += _dir_size(ev)
    return out


def run(dry_run: bool = False) -> dict:
    p = plan()
    if not dry_run:
        for path in p["evidence_dirs"] + p["report_days"]:
            shutil.rmtree(path, ignore_errors=True)
        for path in p["sm50_files"]:
            try:
                os.remove(path)
            except OSError:
                pass
        _prune_databases()
    n = len(p["evidence_dirs"]) + len(p["report_days"]) + len(p["sm50_files"])
    if n:
        log.info(f"retention{' (dry run)' if dry_run else ''}: {len(p['evidence_dirs'])} evidence "
                 f"folder(s), {len(p['report_days'])} report day(s), {len(p['sm50_files'])} "
                 f"sm50 file(s), {p['bytes'] / 1e6:.1f} MB")
    return p


def _prune_databases() -> None:
    """
    Database tables that only ever grew on the single-VM setup (review
    28.10.2026): stored history lines, the alert ledger, finished jobs.
        IBO_HISTORY_LINES_DAYS  default 400   (metric history keeps 12 months)
        IBO_ALERT_LEDGER_DAYS   default 180
        IBO_JOBS_DAYS           default 14
    """
    from core import store
    for label, fn in (
            ("history lines", lambda: store.prune_lines(_days("IBO_HISTORY_LINES_DAYS", 400))),
            ("finished jobs", lambda: store.prune_jobs(_days("IBO_JOBS_DAYS", 14))),
            ("alert ledger", lambda: __import__("core.alert_ledger", fromlist=["prune"]).prune(
                _days("IBO_ALERT_LEDGER_DAYS", 180)))):
        try:
            n = fn()
            if n:
                log.info(f"retention: {n} old {label} row(s) deleted")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"retention: {label} not pruned: {exc}")


if __name__ == "__main__":
    result = run(dry_run="--dry-run" in sys.argv)
    for path in result["evidence_dirs"] + result["report_days"] + result["sm50_files"]:
        print(path)
    print(f"{result['bytes'] / 1e6:.1f} MB")
