"""
Background RFC jobs run in the worker process of core/rfc_proc.py.

No import-time side effects; must not import dashboard.app. Results cross
back to the caller as plain dicts / lists.
"""
from __future__ import annotations


def _sync() -> None:
    try:
        from core import live_settings
        live_settings.sync()          # Settings page changes reach this worker too
    except Exception:  # noqa: BLE001
        pass


def collect_checks_job(system: str, cfg: dict, snapshot_changes: bool = True) -> dict:
    """Run (or reuse) the extra checks for one system and take a change
    snapshot when due. {"ok", "metrics": [dict], "error", "changes"}."""
    _sync()
    from collectors import extra_checks
    from collectors.rfc_collector import SapSession
    out = {"ok": False, "metrics": [], "error": "", "changes": 0}
    with SapSession(system, cfg) as s:
        out["ok"] = bool(s.ok)
        if not s.ok:
            out["error"] = str(getattr(s, "error", "") or getattr(s, "last_error", "") or "")[:300]
        metrics, _age = extra_checks.cached(s, system, cfg)
        out["metrics"] = [extra_checks._to_json(m) for m in metrics]
        if s.ok and snapshot_changes:
            try:
                from core import change_tracking
                out["changes"] = len(change_tracking.maybe_snapshot(s, system))
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"change snapshot failed: {exc}"[:300]
    return out


def change_snapshot_job(system: str, cfg: dict) -> list:
    """A change snapshot now (the Changes tab's button). [[title, detail], ...]."""
    _sync()
    from collectors.rfc_collector import SapSession
    from core import change_tracking
    with SapSession(system, cfg) as s:
        if not s.ok:
            raise RuntimeError(f"RFC logon failed: {getattr(s, 'error', '') or s.last_error}")
        return [list(c) for c in change_tracking.maybe_snapshot(s, system, force=True)]


def synthetic_job(system: str, cfg: dict) -> dict:
    _sync()
    from core import synthetic
    return synthetic.measure(system, cfg)


def collect_custom_job(system: str, cfg: dict) -> dict:
    """Raw readings for the custom alerts (core/custom_alerts.py). {"ok", "data", "error"}."""
    _sync()
    from collectors import custom_reads
    from collectors.rfc_collector import SapSession
    out = {"ok": False, "data": {}, "error": ""}
    with SapSession(system, cfg) as s:
        out["ok"] = bool(s.ok)
        if not s.ok:
            out["error"] = str(getattr(s, "error", "") or getattr(s, "last_error", "") or "")[:300]
            return out
        out["data"] = custom_reads.read_all(s, system, cfg)
    return out
