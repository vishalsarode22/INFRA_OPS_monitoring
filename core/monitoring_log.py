"""
Monitoring log: which monitoring ran, when it started and ended, which mails
went out for it (to whom, or why not), and when the next one is due.

No storage of its own. It joins what is already recorded:

    core/run_history.py     every run and scheduling decision
    alert_sends (ops_db)    every mail / WhatsApp decision: sent, suppressed
                            (with the reason) or failed -- alerts, SYSTEM
                            DOWN, RCA, job / dump owner mails and, through
                            notifications/mailer.send_message, the report
    core/outbox.py          report / WhatsApp deliveries carrying the run_id

A mail belongs to a run when its outbox delivery carries the run's run_id,
otherwise when it is an alert_sends row of the same system written between
the run's start and its end + IBO_DELIVERY_TIMEOUT_SECONDS (default 300)
+ 120 s (deliveries are sent after the watched part of the run ends).

Everything here is pure (rows in, dicts out) so the dashboard route stays
thin and the joins are testable. Never raises on odd rows.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

TRIGGER_LABELS = {
    "schedule": "Scheduled", "catch-up": "Catch-up (missed slot)",
    "manual-system": "Manual (one system)", "manual-profile": "Manual (profile)",
    "manual-all": "Manual (all systems)", "new-system": "New-system check", "retry": "Retry",
    "rca": "RCA run", "cli": "Command line", "legacy": "Imported (old feed)",
}

KIND_LABELS = {
    "report": "Report", "final_report": "Report", "diagnostic": "RCA (diagnostic)",
    "rca": "RCA", "system_down": "SYSTEM DOWN", "vpn_down": "VPN down",
    "not_reachable": "Not reachable", "credentials": "Credentials", "collector_down": "Collector down",
    "recovered": "Recovered", "critical": "Critical alert", "tcode_failure": "T-code failed",
    "sweep_failure": "Pipeline failed", "report_held": "Report held", "report_not_sent": "Report NOT sent", "report_sent": "Report sent notice",
    "monitor_down": "Monitor down", "ocr_parser": "Parser check", "job_owner": "Job owner",
    "dump_owner": "Dump owner", "whatsapp": "WhatsApp screenshots", "user_mail": "Job user mail",
    "alert": "Alert", "recovery": "Recovery", "repeat": "Alert (repeat)", "test": "Test",
    "escalation": "Escalation",
}

# alert_sends.kind values written by the per-value engine (core/alert_engine.py);
# every other value is a gate decision written by core/alert_ledger.history()
# (sent / send failed / suppressed-...), with the mail kind in `metric`.
ENGINE_KINDS = frozenset({"alert", "recovery", "repeat", "test", "user_mail"})
REPORT_KINDS = frozenset({"report", "final_report"})
CHANNEL_LABELS = {"email": "Email", "whatsapp": "WhatsApp", "sms": "SMS", "teams": "Teams"}


def _parse(value) -> datetime | None:
    from core.run_history import _parse as p
    try:
        return p(value)
    except Exception:  # noqa: BLE001
        return None


def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def split_recipients(text) -> tuple[list[str], list[str]]:
    """'a, b | cc: c' (send_message rows) or 'a,b' (ledger rows) -> (to, cc)."""
    text = str(text or "")
    to_part, cc_part = text, ""
    low = text.lower()
    if "| cc:" in low:
        i = low.index("| cc:")
        to_part, cc_part = text[:i], text[i + 5:]
    to = [a.strip() for a in to_part.split(",") if a.strip()]
    cc = [a.strip() for a in cc_part.split(",") if a.strip()]
    return to, cc


def _kind_label(kind: str, fallback: str = "") -> str:
    k = str(kind or "")
    if k in KIND_LABELS:
        return KIND_LABELS[k]
    for suffix, word in (("_skipped", " (skipped)"), ("_failed", " (failed)")):
        if k.endswith(suffix) and k[: -len(suffix)] in KIND_LABELS:
            return KIND_LABELS[k[: -len(suffix)]] + word
    return fallback or k.replace("_", " ") or "Mail"


def email_from_send(row: dict) -> dict:
    """One alert_sends row -> the Monitoring log's email entry."""
    row = row or {}
    decision = str(row.get("kind") or "")
    note = str(row.get("note") or "")
    ok = bool(row.get("ok"))
    engine = decision in ENGINE_KINDS
    if engine:
        kind = decision
        subject = None
        label = str(row.get("label") or row.get("metric") or "")
        detail = " ".join(x for x in (str(row.get("level") or ""), label) if x)
        if row.get("value") not in (None, ""):
            detail += f" = {row.get('value')}"
        if row.get("instance"):
            detail += f" ({row.get('instance')})"
    else:
        kind = str(row.get("metric") or "")
        detail = None
        if ok:
            subject = note or None
        elif kind in REPORT_KINDS or kind in ("diagnostic", "rca"):
            subject = str(row.get("value") or "") or None       # the key is the subject
        else:
            subject = None
    if ok:
        status, status_text = "sent", "sent"
    else:
        low = note.lower()
        if decision.startswith("suppressed") or low.startswith("held") or low.startswith("suppressed"):
            reason = note.split(":", 1)[1].strip() if ":" in note else (note or decision)
            status, status_text = "suppressed", f"suppressed: {reason}"
        else:
            reason = note[len("send failed:"):].strip() if low.startswith("send failed:") else note
            status, status_text = "failed", f"failed: {reason or 'see logs/email.log'}"
    to, cc = split_recipients(row.get("recipients"))
    channel = str(row.get("channel") or "email")
    return {"id": row.get("id"), "at": row.get("at"), "system": row.get("system"), "kind": kind,
            "kind_label": _kind_label(kind, str(row.get("label") or "")), "subject": subject,
            "detail": detail, "to": to, "cc": cc, "channel": channel,
            "channel_label": CHANNEL_LABELS.get(channel, channel), "status": status,
            "status_text": status_text, "source": "alert_sends", "run_id": None}


_OUTBOX_STATUS = {"queued": "queued", "sending": "sending", "sent": "sent", "failed": "failed",
                  "timeout": "failed", "lost": "failed", "retry-scheduled": "queued"}


def email_from_outbox(entry: dict) -> dict:
    """An outbox delivery that has no alert_sends row (queued, timed out, lost, WhatsApp)."""
    st = str(entry.get("status") or "")
    status = _OUTBOX_STATUS.get(st, st or "unknown")
    text = st or "unknown"
    if st == "timeout":
        text = f"failed: timeout ({entry.get('error') or 'no answer'})"
    elif st in ("failed", "lost"):
        text = f"failed: {entry.get('error') or st}"
    elif st == "retry-scheduled":
        text = f"retry scheduled for {entry.get('next_attempt_at') or 'later'}"
    kind = str(entry.get("kind") or "")
    channel = "whatsapp" if kind == "whatsapp" else "email"
    when = _parse(entry.get("finished_at") or entry.get("started_at") or entry.get("queued_at"))
    return {"id": f"outbox:{entry.get('id')}", "at": _fmt(when), "system": entry.get("system"),
            "kind": kind, "kind_label": _kind_label(kind), "subject": entry.get("description") or None,
            "detail": None, "to": [], "cc": [], "channel": channel,
            "channel_label": CHANNEL_LABELS.get(channel, channel), "status": status,
            "status_text": text, "source": "outbox", "run_id": entry.get("run_id")}


def run_window(run: dict, timeout_s: float) -> tuple[datetime, datetime] | None:
    start = _parse(run.get("started_at"))
    if start is None:
        return None
    end = _parse(run.get("ended_at"))
    if end is None and run.get("duration_s") is not None:
        try:
            end = start + timedelta(seconds=float(run["duration_s"]))
        except (TypeError, ValueError):
            end = None
    if end is None:
        end = _parse(run.get("recorded_at")) or start
    return start, end + timedelta(seconds=float(timeout_s) + 120)


def link(runs: list[dict], emails: list[dict], outbox_entries: list[dict],
         timeout_s: float) -> list[dict]:
    """Attach `emails` (newest first) to each run. Returns the runs (new dicts)."""
    by_run: dict = {}
    for e in outbox_entries or []:
        if e.get("run_id"):
            by_run.setdefault(str(e["run_id"]), []).append(e)
    # Per system, sorted by time: each run looks up its window with a binary
    # search instead of scanning every mail (200 runs x 5000 mails was 80 % of
    # the Monitoring log's time -- perf test 28.10.2026).
    import bisect
    by_sys: dict = {}
    for e in emails:
        at = _parse(e.get("at"))
        if at is not None:
            by_sys.setdefault(e.get("system"), []).append((at, e))
    keys_by_sys = {}
    for sysname, lst in by_sys.items():
        lst.sort(key=lambda t: t[0])
        keys_by_sys[sysname] = [t[0] for t in lst]
    out = []
    from core import run_names
    _names = run_names.maps()
    for run in runs:
        r = dict(run)
        r["trigger_label"] = TRIGGER_LABELS.get(str(r.get("trigger") or ""), r.get("trigger") or "")
        r["run_name"] = run_names.name_for(r, _names)
        mine: list[dict] = []
        win = run_window(r, timeout_s)
        if win is not None:
            lo, hi = win
            lst = by_sys.get(r.get("system")) or []
            keys = keys_by_sys.get(r.get("system")) or []
            i, j = bisect.bisect_left(keys, lo), bisect.bisect_right(keys, hi)
            for _at, e in reversed(lst[i:j]):          # newest first, as before
                mine.append(dict(e, run_id=r.get("run_id")))
        for entry in by_run.get(str(r.get("run_id") or ""), []):
            kind = str(entry.get("kind") or "")
            channel = "whatsapp" if kind == "whatsapp" else "email"
            twin_kinds = REPORT_KINDS if kind in REPORT_KINDS else {kind}
            twin = next((m for m in mine if m["kind"] in twin_kinds and m["channel"] == channel
                         and not m.get("outbox_status")), None)
            if twin is not None and entry.get("status") in ("sent", "failed"):
                twin["outbox_status"] = entry.get("status")
                continue
            mine.append(email_from_outbox(entry))
        mine.sort(key=lambda m: str(m.get("at") or ""), reverse=True)
        r["emails"] = mine
        r["emails_sent"] = sum(1 for m in mine if m["status"] == "sent")
        out.append(r)
    return out


def emails_summary(emails: list[dict]) -> str:
    parts = []
    for m in emails or []:
        who = ", ".join(m.get("to") or [])
        if m.get("cc"):
            who += (" " if who else "") + "cc " + ", ".join(m["cc"])
        parts.append(f"{m.get('kind_label')} → {who or '—'} ({m.get('status_text')})")
    return "; ".join(parts)


CSV_COLUMNS = ("started_at", "ended_at", "duration_s", "system", "profile_id", "trigger", "result",
               "failed_stage", "failure_reason", "screenshots_captured", "next_run_at",
               "next_run_reason", "unexpected", "run_id", "emails")


def to_csv(runs: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in runs:
        line = []
        for c in CSV_COLUMNS:
            if c == "emails":
                v = emails_summary(r.get("emails") or [])
            elif c == "unexpected":
                v = 1 if r.get(c) else 0
            elif c == "trigger":
                v = r.get("trigger_label") or r.get("trigger")
            else:
                v = r.get(c)
            text = "" if v is None else str(v)
            if text[:1] in ("=", "+", "-", "@") and c != "duration_s":
                text = "'" + text                        # no formula injection in Excel
            line.append(text)
        w.writerow(line)
    return buf.getvalue()
