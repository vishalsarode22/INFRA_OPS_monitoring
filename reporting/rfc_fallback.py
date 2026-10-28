"""
Report rows for checks that have no SAP GUI capture this run.

When the SAP GUI part of a sweep could not run (other people's SAP sessions
open on the monitoring VM, the SAP Logon entry missing, a logon refused),
the RFC collector has usually still read the same figures -- ST22 dumps,
SM37 cancelled jobs, SM12 locks, SM58 / SMQ1 / SMQ2 queues, AL08 users,
SM66 load. The Excel sheet and the PDF used to print "See attached
screenshot" / NOT MEASURED for every row anyway, and never said why.

Now each such row gets the RFC figures (marked "read over RFC") and, where
there is nothing, the reason the screen was not captured.
"""
from __future__ import annotations

_LABELS = {
    "sap.st22.dumps": "ABAP dumps today",
    "sap.sm37.cancelled_jobs": "Cancelled jobs today",
    "sap.sm37.active_jobs": "Running jobs",
    "sap.sm12.lock_count": "Lock entries",
    "sap.sm12.oldest_lock_minutes": "Oldest lock (min)",
    "sap.sm12.locks_per_user_max": "Most locks per user",
    "sap.sm13.failed_updates": "Failed updates",
    "sap.sm58.stuck_entries": "Failed tRFC today",
    "sap.sm58.sysfail_backlog": "Older failed tRFC",
    "sap.smq1.entries": "Outbound queue entries",
    "sap.smq2.entries": "Inbound queue entries",
    "sap.al08.user_logons": "Users logged on",
    "sap.sm66.wp_saturation_pct": "Dialog WP busy",
    "sap.sm66.max_instance_saturation_pct": "Busiest instance",
    "sap.sm50.long_running_wp": "Long-running WPs",
    "sap.sm50.priv_mode_wp": "WPs in PRIV mode",
    "sap.st03.dialog_resp_ms": "Dialog response (ms)",
    "sap.st03.max_instance_resp_ms": "Slowest instance (ms)",
    "sap.sm21.errors": "System log errors",
    "sap.we02.failed_idocs": "Failed IDocs",
    "sap.db12.last_backup": "Last backup",
}

# Template row -> which RFC metrics belong to it (besides metrics whose own
# tcode matches the row).
_ROW_METRICS = {
    "SM37_CANCELLED": {"sap.sm37.cancelled_jobs"},
    "SM37": {"sap.sm37.active_jobs", "sap.sm37.cancelled_jobs"},
    "SMLG": {"sap.st03.dialog_resp_ms", "sap.st03.max_instance_resp_ms"},
    "SM50": {"sap.sm50.long_running_wp", "sap.sm50.priv_mode_wp"},
}
_RANK = {"CRITICAL": 3, "WARNING": 2, "OK": 1, "UNKNOWN": 0}


def _status(m) -> str:
    s = getattr(getattr(m, "status", None), "value", getattr(m, "status", "")) or ""
    s = str(s).upper()
    return "OK" if s == "NORMAL" else (s if s in _RANK else "UNKNOWN")


def _is_gui(m) -> bool:
    return str(getattr(m, "source", "") or "").startswith("sap_gui")


def gui_skip_reason(result) -> str:
    """Why the SAP GUI screens were not captured this run ('' when they were, or unknown)."""
    for e in getattr(result, "errors", None) or []:
        text = str(e)
        if text.startswith("sap_gui:"):
            return text.split(":", 1)[1].strip()
    return ""


def desktop_note(result) -> str:
    """A locked / disconnected monitoring desktop during the sweep ('' when fine)."""
    for e in getattr(result, "errors", None) or []:
        text = str(e)
        if text.startswith("desktop:"):
            return text.split(":", 1)[1].strip()
    return ""


def metrics_for_row(result, tcode: str) -> list:
    tcode = (tcode or "").upper()
    names = _ROW_METRICS.get(tcode)
    out = []
    for m in getattr(result, "metrics", None) or []:
        if _is_gui(m) or getattr(m, "value", None) is None and _status(m) == "UNKNOWN":
            continue
        own = str(getattr(m, "tcode", "") or "").upper()
        if names is not None:
            if getattr(m, "name", "") in names:
                out.append(m)
        elif own == tcode:
            out.append(m)
    return out


def row(result, tcode: str) -> tuple[str, str, str] | None:
    """(actual result, status, observation) from RFC for a template row; None when nothing was read."""
    ms = metrics_for_row(result, tcode)
    reason = gui_skip_reason(result)
    why = f" No SAP GUI screenshot this run: {reason}" if reason else ""
    if not ms:
        if reason:
            return (f"Not captured this run -- {reason}", "UNKNOWN",
                    "Neither the screen nor an RFC read is available for this check this run.")
        return None
    parts, details = [], []
    for m in ms:
        label = _LABELS.get(m.name, m.name.split(".")[-1].replace("_", " "))
        parts.append(f"{label}: {m.display_value}")
        d = (getattr(m, "detail", "") or "").strip()
        if d and _status(m) in ("WARNING", "CRITICAL"):
            details.append(d)
    status = max((_status(m) for m in ms), key=lambda s: _RANK[s])
    observation = ("; ".join(details)[:400] + ". " if details else "") + "Read over RFC." + why
    return "; ".join(parts), status, observation.strip()
