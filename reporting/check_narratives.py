"""
Per-T-code capture narratives for reports.

Turns what the GUI sweep actually captured for each T-code into:

    observation     what was seen, in one or two specific sentences
    status          OK / WARNING / CRITICAL / UNKNOWN / FAILED -- the same
                    words the Excel sheet uses, so the two cannot disagree
    value           the headline count or figure, if there is one
    recommendation  what to do about it, only when it needs attention

Inputs are the MetricResult.extra_data written by the GUI collector: the
pattern-extracted fields (lock_count, dump_count, ...) plus the OCR excerpt
of the final screen (ocr_excerpt), which this module mines with per-T-code
regexes so a screen with no dedicated parser still yields a real sentence
instead of "See attached screenshot".

Why this exists: the Excel "Checks" column and the PDF used to fall back to
dumping every extra_data key -- which is where "Evidence Id: EV-..." rows
came from (metadata leaking into the observation) and where SMLG read
"Instance Count: 0" (an analysis dict leaking through). Both reports now
go through here, so they say the same thing and say something useful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# An RFC metric name's "sap.<tcode>.<field>" shape usually names the T-code
# capture that covers the same ground -- but not always. sap.sm37.cancelled_
# jobs, for instance, splits to tcode "SM37", which is the ACTIVE-jobs
# capture; the comparable capture is the separate "SM37_CANCELLED" check.
# Without this override, the conflict-detector below compared the cancelled-
# jobs RFC count against the active-jobs screen and reported the two as
# disagreeing over unrelated numbers.
_METRIC_TCODE_OVERRIDES = {
    "sap.sm37.cancelled_jobs": "SM37_CANCELLED",
}

# Keys that are bookkeeping, never observations.
_META_KEYS = {
    "evidence_id", "attempts", "system", "client", "recovery_actions",
    "started_at", "finished_at", "ocr_excerpt", "ocr_lines", "analysis",
    "historical_analysis", "screenshot", "screenshots",
}


@dataclass
class CheckNarrative:
    tcode: str
    task: str
    observation: str
    status: str                  # OK | WARNING | CRITICAL | UNKNOWN | FAILED
    value: str = ""
    result: str = ""             # the Excel "Actual Result" wording
    is_evidence: bool = False    # the capture record, not a metric derived from it
    recommendation: str = ""
    captured_at: str = ""
    evidence_id: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)


# Human task names per T-code (used where the template has none).
TASKS = {
    "SM21": "SAP System Log", "ST22": "ABAP Dumps", "SM13": "Update Requests",
    "SM12": "Lock Entries", "SP01": "Spool Requests", "SM37": "Active Background Jobs",
    "SM37_CANCELLED": "Cancelled Background Jobs", "AL08": "User Logons",
    "SM51": "Application Servers", "SM66": "Work Processes (global)",
    "SM50": "Work Processes (instance)", "SCOT": "SAPconnect Configuration",
    "ST03N": "Dialog Response Time", "SMLG": "Logon Groups / Load",
    "SOST": "SAPconnect Send Requests", "DB12": "Backup Status",
    "DB01": "Exclusive Lock Waits", "DB02": "DB Space Overview",
    "SMQ1": "qRFC Outbound Queue", "SMQ2": "qRFC Inbound Queue",
    "SM58": "tRFC Log", "ST06": "OS Monitor (CPU / Memory)",
}


def _num(s) -> int | None:
    try:
        return int(str(s).replace(",", "").replace(".", "").strip())
    except (TypeError, ValueError):
        return None


def _grab(pattern: str, text: str, flags=re.I):
    m = re.search(pattern, text, flags)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Reconciliation with the Excel sheet
#
# Excel reads the structured extra_data fields; the handlers further down
# mine the OCR excerpt with regexes. Two readings of one capture is how a
# single run reported DB01 as "1 blocked" in the PDF and "0 locks" in the
# sheet, SCOT as "0 nodes" against "Mail Port 25", and SMLG as
# "1 ms, 216 users" against "no data captured".
#
# The regexes lose that argument on the evidence: _db01 counted the status
# bar's clock as a blocked row, _scot needed a token before "SMTP" that the
# screen does not have, and _smlg's fallback split "1.216" on the decimal
# point. Structured fields do not have those failure modes, and where no
# structured field exists the sheet says UNKNOWN rather than guessing.
#
# So for any T-code the sheet carries, the sheet is authoritative for status
# and figures. The handlers stay in charge of T-codes the sheet does not
# carry (SM21, ST03N, ST06, SM50) and of the recommendation text, which is
# advice rather than data.
# --------------------------------------------------------------------------

# One vocabulary for both reports. This used to translate the sheet's
# WARNING/CRITICAL into "ATTENTION" and UNKNOWN into "NOT COLLECTED", so the
# PDF and the Excel sheet sent in the same email used different words for
# the same row -- and the PDF could not say which findings were critical.
_EXCEL_STATUS = {
    "OK": "OK",
    "WARNING": "WARNING",
    "CRITICAL": "CRITICAL",
    "UNKNOWN": "UNKNOWN",
}

# The PDF's Count/Value column is narrow; anything longer than a short
# phrase belongs in Observation instead of being truncated mid-word.
_VALUE_MAX_CHARS = 30


def _excel_view(metric):
    """
    (status, value, observation) as the Excel sheet will render this metric,
    or None when the sheet does not cover it and the handler should stand.

    Imported lazily: reporting.excel_template_writer imports core.models, and
    a module-level import here would make check_narratives unusable in the
    lighter contexts that only want narrate().
    """
    try:
        from reporting.excel_template_writer import (
            TCODE_ROW_MAP, NO_DATA_TEXT,
            _status_for_metric, _build_actual_result, _build_observation,
        )
    except Exception:
        return None

    tcode = (getattr(metric, "tcode", "") or "").upper()
    if tcode not in set(TCODE_ROW_MAP.values()):
        return None

    try:
        raw_status = _status_for_metric(metric)
        status = _EXCEL_STATUS.get(raw_status, "UNKNOWN")
        result = str(_build_actual_result(metric) or "").strip()
        observation = str(_build_observation(metric, raw_status) or "").strip()
    except Exception:
        return None

    # Nothing structured to show: leave the handler's narrative in place
    # rather than replacing a real sentence with a placeholder.
    if not result or result == NO_DATA_TEXT:
        return None

    value = result if len(result) <= _VALUE_MAX_CHARS else ""
    return status, value, result, observation


# --------------------------------------------------------------------------
# OCR legibility
#
# Tesseract on a SAP GUI screen with icon toolbars returns long runs of
# glyph noise ("(c) gRFC Edit Goto ... @Ge@ishs(R) AALS e8"). Printing that
# as an Observation in a client report is worse than printing nothing: it
# looks like the tool is broken. These two helpers decide whether an OCR
# excerpt is readable enough to quote, and only then is it quoted.
# --------------------------------------------------------------------------

_WORDLIKE = re.compile(r"^[A-Za-z][A-Za-z./-]{2,}$|^\d{1,7}$|^[A-Za-z]{1,4}\d{1,4}$")


# Characters that essentially never appear in SAP screen text but are the
# signature of Tesseract reading toolbar icons: box glyphs, currency and
# quotation marks, guillemets, copyright/registered symbols.
_GARBAGE_CHARS = set("«»©®£¥€“”‘’|\\^~`§¶†‡•…=[]{}<>*")


def _is_legible(text: str, min_tokens: int = 6) -> bool:
    """
    True when an OCR excerpt reads like words rather than glyph noise.

    Two signals, because neither alone is enough. Word density catches short
    fragments ("& CAQ (1) 800 * vhrrncaqci INS [al"). Glyph density catches
    the harder case where a toolbar row sits between real menu words and the
    line scores well on words alone -- the SOST capture read as legible on
    word density at 0.65 while containing five icon-noise tokens.
    """
    tokens = [t for t in re.split(r"\s+", str(text or "").strip()) if t]
    if len(tokens) < min_tokens:
        return False
    wordlike = sum(1 for t in tokens if _WORDLIKE.match(t))
    garbage = sum(1 for t in tokens if _GARBAGE_CHARS & set(t))
    return (wordlike / len(tokens)) >= 0.60 and (garbage / len(tokens)) <= 0.08


NO_READABLE_TEXT = ("Screen captured; the on-screen text could not be read "
                    "reliably. See the attached screenshot.")


def _plausible_instance(name: str) -> bool:
    """
    SAP instance names are <host>_<SID>_<NN> with an uppercase 3-char SID.
    OCR routinely turns q into g and uppercase into lowercase, producing
    lookalikes such as vhrrncagei_cao_00 for vhrrncaqci_CAQ_00. Printing a
    wrong hostname in a client report is worse than printing none, so a name
    that fails this check is dropped rather than guessed at.
    """
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9]*_[A-Z0-9]{3}_\d{2}", str(name or "")))


def narrate(metric) -> CheckNarrative:
    """Build the narrative for one GUI-capture MetricResult."""
    tcode = (getattr(metric, "tcode", "") or "").upper()
    data: dict = dict(getattr(metric, "extra_data", {}) or {})
    ocr: str = str(data.get("ocr_excerpt", "") or "")
    task = TASKS.get(tcode, tcode)
    n = CheckNarrative(tcode=tcode, task=task, observation="", status="OK",
                       captured_at=str(data.get("finished_at", "") or ""),
                       evidence_id=str(data.get("evidence_id", "") or ""))
    facts = {k: v for k, v in data.items() if k not in _META_KEYS and v is not None}
    n.facts = facts
    shots = list(getattr(metric, "screenshot_paths", None) or [])
    if not shots and getattr(metric, "screenshot_path", None):
        shots = [metric.screenshot_path]
    n.screenshots = [s_ for s_ in shots if s_]
    n.is_evidence = str(getattr(metric, "name", "") or "").startswith("screenshot_")

    display = getattr(metric, "display_value", "") or ""
    if display == "failed":
        n.status = "FAILED"
        n.observation = f"Capture failed: {getattr(metric, 'detail', '') or 'unknown error'}"
        n.recommendation = "Re-run the sweep; if it fails again check the SAP GUI session and screen layout for this T-code."
        return n

    handler = _HANDLERS.get(tcode, _generic)
    handler(n, facts, ocr)
    if not n.observation:
        _generic(n, facts, ocr)

    # The sheet wins on status and figures; the handler keeps its advice.
    # Run this last so it overrides whatever the handler concluded -- the
    # point is that section 2 of the PDF and the Excel row cannot disagree.
    view = _excel_view(metric)
    if view is not None:
        n.status, n.value, n.result, n.observation = view
    elif not n.result:
        n.result = n.value

    return n


# --------------------------------------------------------------------------
# Per-T-code handlers. Each sets observation/status/value/recommendation.
# They read parsed fields first, then mine the OCR excerpt.
#
# For T-codes the Excel sheet carries, _excel_view() overrides the status,
# value and observation set here; the recommendation survives. For the rest
# (SM21, ST03N, ST06, SM50) these handlers remain the only source.
# --------------------------------------------------------------------------

def _st22(n, f, ocr):
    c = _num(f.get("dump_count"))
    if c is None and re.search(r"No short dumps match", ocr, re.I):
        c = 0
    if c is None:
        c = _num(_grab(r"(\d+)\s+Runtime Errors", ocr))
    if c is None:
        return
    n.value = f"{c} dumps"
    n.observation = (f"{c} ABAP runtime error(s) today (all clients)."
                     if c else "No ABAP runtime errors today or yesterday in any client.")
    if c:
        n.status = "WARNING"
        n.recommendation = "Open ST22, group by runtime error and program, and assign the top offenders to the responsible team."


def _sm12(n, f, ocr):
    c = _num(f.get("lock_count"))
    if c is None:
        c = _num(_grab(r"Lock Table\s*\((\d+)\)", ocr))
    if c is None:
        return
    users = sorted(set(re.findall(r"\d{2}:\d{2}:\d{2}\s+\d{3}\s+([A-Z][A-Z0-9_]+)", ocr)))[:5]
    n.value = f"{c} locks"
    n.observation = f"{c} lock entr{'y' if c == 1 else 'ies'} in the enqueue table"
    n.observation += (f", held by {', '.join(users)}." if users else ".")
    if c > 20:
        n.status = "WARNING"
        n.recommendation = "Check for long-held locks (SM12 → age) and terminated sessions still holding enqueue entries."


def _sm13(n, f, ocr):
    s = f.get("update_summary")
    c = _num(_grab(r"(\d+)\s*Update records?", str(s or "")))
    if c is None and "Update Requests" in ocr and not re.search(r"\bErr\b|Error", ocr):
        c = 0
    if c is None:
        return
    n.value = f"{c} update requests"
    n.observation = "No pending or failed update requests." if c == 0 else f"{c} update request(s) pending."
    if c:
        n.status = "WARNING"
        n.recommendation = "Review SM13 for update errors (status Err) and repeat or delete them after root-cause."


def _sp01(n, f, ocr):
    c = _num(f.get("spool_count_visible"))
    m = re.search(r"(\d+)\s*Spool requests displayed", ocr, re.I)
    if m:
        c = int(m.group(1))
    noout = _num(_grab(r"(\d+)\s*Spool requests without output request", ocr))
    if c is None:
        return
    n.value = f"{c} spool requests"
    n.observation = f"{c} spool request(s) listed for the selection"
    n.observation += (f"; {noout} without an output request." if noout is not None else ".")
    if c > 50:
        n.status = "WARNING"
        n.recommendation = "Schedule spool reorganisation (RSPO0041/RSPO1041) and check for stuck output requests."


def _sm37(n, f, ocr):
    c = _num(f.get("active_jobs"))
    if c is None:
        return
    n.value = f"{c} active"
    n.observation = f"{c} background job(s) active at capture time."


def _sm37_cancelled(n, f, ocr):
    # action_sm37_cancelled's authoritative figure is "records_passed" /
    # "cancelled_job_count", read from SM37's Shift+F7 "List Status"
    # popup -- "cancelled_jobs" is a leftover key that action always sets
    # to [] ("No individual job extraction is performed", per its own
    # docstring), so reading it as a count here made _num() fail on every
    # run and the real figure never reached the report.
    c = _num(f.get("records_passed"))
    if c is None:
        c = _num(f.get("cancelled_job_count"))
    jobs = re.findall(r"\b([A-Z][A-Z0-9_]{6,})\s+\S*\s*\S*\s*[A-Z_]+\s+Canceled", ocr)
    if c is None and "Canceled" in ocr:
        c = len(jobs) or 1
    if c is None:
        return
    n.value = f"{c} cancelled"
    if c == 0:
        n.observation = "No cancelled background jobs in the selection period."
        return
    n.observation = f"{c} cancelled job(s) today/yesterday"
    n.observation += (f": {', '.join(sorted(set(jobs))[:5])}." if jobs else ".")
    n.status = "WARNING"
    n.recommendation = "Open the job log (SM37 → Job log) for each cancelled job; check for dumps at the cancel time in ST22."


def _al08(n, f, ocr):
    users = _num(_grab(r"(\d+)\s*user sessions? with", ocr))
    sess = _num(_grab(r"with\s*(\d+)\s*ABAP sessions", ocr))
    if users is None:
        s = str(f.get("session_summary") or "")
        users = _num(_grab(r"(\d+)\s*user logons", s)); sess = _num(_grab(r"(\d+)\s*back-end", s))
    if users is None:
        return
    n.value = f"{users} users / {sess or '?'} sessions"
    n.observation = f"{users} logged-on user session(s) with {sess or '?'} ABAP session(s) across all instances."


def _sm51(n, f, ocr):
    c = _num(f.get("instances_started"))
    if c is None:
        c = _num(_grab(r"(\d+)\s*AS instance\(s\) started", ocr))
    if c is None:
        return
    inst = re.findall(r"\b(\w+_[A-Z0-9]{3}_\d{2})\b", ocr)
    n.value = f"{c} active"
    n.observation = f"{c} application server instance(s) started"
    n.observation += (f": {', '.join(sorted(set(inst)))}." if inst else ".")


def _sm66(n, f, ocr):
    running = _num(f.get("running_processes"))
    rows = _num(f.get("visible_process_rows"))
    if running is None:
        return
    n.value = f"{running} running"
    n.observation = f"{running} work process(es) running system-wide"
    n.observation += (f" ({rows} rows visible)." if rows is not None else ".")


def _sm50(n, f, ocr):
    total = _num(_grab(r"Total Number of Work Processes\s*(\d+)", ocr))
    dia = re.search(r"Dialog\s*(\d+)\s*/\s*(\d+)", ocr)
    bgd = re.search(r"Background\s*(\d+)\s*/\s*(\d+)", ocr)
    if total is None and not dia:
        return
    parts = []
    if total is not None:
        parts.append(f"{total} work processes total")
    if dia:
        parts.append(f"Dialog {dia.group(2)} of {dia.group(1)} free")
    if bgd:
        parts.append(f"Background {bgd.group(2)} of {bgd.group(1)} free")
    n.value = f"{total} WPs" if total is not None else ""
    n.observation = "; ".join(parts) + "."
    if dia and int(dia.group(2)) == 0:
        n.status = "WARNING"
        n.recommendation = "No free dialog work processes -- users will queue. Check long-running dialog steps in SM50."


def _scot(n, f, ocr):
    # A listed mail port -- any number -- means the SMTP node is configured,
    # and it is the figure this check reports. Previously the narrative only
    # counted node names out of the OCR text, so a port read cleanly by
    # scripting could still be reported as "the SMTP node list appeared
    # empty" and flagged ATTENTION.
    if f.get("extraction_failed"):
        # Nothing was read, so say nothing about what SCOT contains -- the
        # OCR node count below would otherwise report an empty node list it
        # never actually observed. The sheet supplies status and wording.
        n.recommendation = ("Re-run the sweep; if SCOT still cannot be read, open "
                            "SCOT > Settings > SMTP Nodes and check the mail port by hand.")
        return
    port = _num(f.get("mail_port"))
    if port is not None:
        n.value = f"Port {int(port)}"
        n.observation = f"SAPconnect SMTP node is configured; mail port {int(port)} is listed."
        return
    if "mail_port_raw" in f and not f.get("extraction_failed"):
        raw = str(f.get("mail_port_raw") or "").strip()
        n.value = "No port"
        if _num(f.get("smtp_nodes_configured")) == 0:
            n.value = "No SMTP node"
            n.observation = ("The SMTP Nodes list in SCOT is empty: no SMTP node is "
                             "configured, so outbound mail cannot leave the system.")
            n.status = "WARNING"
            n.recommendation = ("Create and activate an SMTP node in SCOT with the mail host "
                                "and port. Until one exists, SOST send requests stay queued "
                                "and are never delivered.")
            return
        else:
            n.observation = (f"The SMTP mail port field shows {raw!r}, which is not a port number."
                             if raw else "No SMTP mail port is listed in SCOT.")
        n.status = "WARNING"
        n.recommendation = ("Check the SMTP node in SCOT; without a mail port configured, "
                            "outbound mail (SOST) cannot leave the system.")
        return

    if "SMTP" not in ocr and not f:
        return
    nodes = re.findall(r"\b([A-Z0-9_]{3,})\s+(?:SMTP|smtp)", ocr)
    n.observation = ("SAPconnect SMTP node configuration captured"
                     + (f"; {len(nodes)} node(s) listed." if nodes else "; the SMTP node list appeared empty on the captured screen."))
    n.value = f"{len(nodes)} nodes" if nodes else "0 nodes"
    if not nodes:
        n.status = "WARNING"
        n.recommendation = "Confirm an SMTP node exists and is active in SCOT; outbound mail (SOST) cannot leave the system without one."


def _st03n(n, f, ocr):
    rt = f.get("dialog_avg_response_time_ms")
    m = re.search(r"DIALOG\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)", ocr)
    steps = None
    if m:
        steps = _num(m.group(1))
        rt = rt or m.group(2).replace(",", ".")
    if rt is None:
        return
    try:
        rtf = float(str(rt).replace(",", "."))
    except ValueError:
        rtf = None
    n.value = f"{rt} ms"
    n.observation = f"Average dialog response time {rt} ms"
    n.observation += (f" over {steps} dialog steps today." if steps else " (today's workload).")
    if rtf is not None and rtf > 1000:
        n.status = "WARNING"
        n.recommendation = "Dialog response above 1 s. Break down by DB time vs CPU time in ST03N → Workload overview and check top transactions."


def _smlg(n, f, ocr):
    # The State column is an icon (often OCR'd as nothing or a stray glyph),
    # so allow an optional NON-digit token there -- a greedy \S* here ate
    # the leading "6" of "68 ms".
    m = re.search(r"(\w+_[A-Z0-9]{3}_\d{2})(?:\s+[^\d\s]\S*)?\s+(\d+)\s+(\d+)\s+(\d{2}:\d{2}:\d{2})\s+(\d+)\s+(\d+)", ocr)
    if not m:
        m2 = re.search(r"Resp\.?\s*time.*?(\w+_[A-Z0-9]{3}_\d{2})\D+(\d+)\D+(\d+)", ocr, re.I)
        if not m2:
            return
        inst, resp, users = m2.group(1), m2.group(2), m2.group(3)
        n.value = f"{resp} ms"
        where = f"Instance {inst}" if _plausible_instance(inst) else "Instance (name unreadable)"
        n.observation = f"{where}: response {resp} ms, {users} users (SMLG load view)."
        return
    inst, resp, users, tm, qual, steps = m.groups()
    groups = re.findall(r"\b(PUBLIC|SPACE|[A-Z0-9_]{4,})\s+\w+_[A-Z0-9]{3}_\d{2}\s+\d+\.\d+", ocr)
    n.value = f"{resp} ms"
    where = f"Instance {inst}" if _plausible_instance(inst) else "Instance (name unreadable)"
    n.observation = (f"{where}: response {resp} ms, {users} users, quality {qual}, "
                     f"{steps} dialog steps (sample {tm}).")
    if groups:
        n.observation += f" Logon groups: {', '.join(sorted(set(groups)))}."
    if int(resp) > 1000:
        n.status = "WARNING"
        n.recommendation = "Instance response above 1 s in the load balancer view; correlate with ST03N and SM50."


def _sost(n, f, ocr):
    total = _num(f.get("send_requests"))
    sent = _num(f.get("sent"))
    errors = _num(f.get("errors"))
    m = re.search(r"(\d+)\s*Send\b.*?(\d+)\s*Sent.*?(\d+)\s*Errors?", ocr, re.I)
    if m:
        total, sent, errors = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if total is None:
        return
    waiting = None if sent is None else max(total - sent - (errors or 0), 0)
    n.value = f"{total} requests"
    n.observation = (f"{total} send request(s) in the period: {sent if sent is not None else '?'} sent, "
                     f"{waiting if waiting is not None else '?'} waiting, {errors if errors is not None else '?'} in error.")
    if (waiting or 0) > 50 or (errors or 0) > 0:
        n.status = "WARNING"
        n.recommendation = ("Large waiting queue / errors in SOST: check that the SAPconnect send job (RSCONN01) is scheduled "
                            "and that SCOT has an active SMTP node.")


def _db12(n, f, ocr):
    ok = len(re.findall(r"successful", ocr, re.I))
    failed = len(re.findall(r"\bfailed\b|\berror\b", ocr, re.I))
    last = _grab(r"(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2})", ocr)
    status = f.get("backup_status")
    if not ok and not failed and not status:
        return
    n.value = f"{ok} successful"
    n.observation = f"Backup catalog: {ok} successful backup entr{'y' if ok == 1 else 'ies'} visible"
    n.observation += (f", most recent {last}." if last else ".")
    if failed:
        n.observation += f" {failed} entr{'y' if failed == 1 else 'ies'} flagged failed/error."
        n.status = "WARNING"
        n.recommendation = "Investigate failed backups in DB12 → Backup Logs before the retention window closes."


def _db01(n, f, ocr):
    if "Blocked Transactions" not in ocr and not f:
        return
    rows = len(re.findall(r"\d{2}:\d{2}:\d{2}", ocr))
    n.value = f"{rows} blocked" if rows else "0 blocked"
    n.observation = ("No blocked transactions / exclusive lock waits at capture time."
                     if rows == 0 else f"{rows} blocked transaction row(s) visible.")
    if rows:
        n.status = "WARNING"
        n.recommendation = "Identify the blocking session in DB01 and the ABAP program behind it (SM50/SM66)."


def _db02(n, f, ocr):
    state = _grab(r"Operational State\s*([A-Za-z]+)", ocr)
    version = _grab(r"Version\s*([\d.]+)", ocr)
    started = _grab(r"Start Time Of First Started Service\s*([\d.]+\s+[\d:]+)", ocr)
    if not (state or version or started):
        return
    parts = ["Database overview captured"]
    if state:
        parts.append(f"operational state {state}")
    if version:
        parts.append(f"version {version}")
    if started:
        parts.append(f"first service started {started}")
    n.observation = ", ".join(parts) + "."
    n.value = state or ""


def _smq(n, f, ocr):
    e = _num(f.get("entries_displayed"))
    q = _num(f.get("queues_displayed"))
    if e is None:
        e = _num(_grab(r"Number of Entries Displayed\s*(\d+)", ocr))
        q = _num(_grab(r"Number of Queues Displayed\s*(\d+)", ocr))
    if e is None:
        return
    n.value = f"{e} entries / {q or 0} queues"
    n.observation = ("Queue empty -- no stuck LUWs." if e == 0
                     else f"{e} entr{'y' if e == 1 else 'ies'} in {q or '?'} queue(s).")
    if e:
        n.status = "WARNING"
        n.recommendation = "Check queue status (SYSFAIL/CPICERR) and restart or delete blocked LUWs after review."


def _sm58(n, f, ocr):
    s = f.get("trfc_status")
    if not s and "Nothing was selected" in ocr:
        s = "Nothing was selected"
    if not s:
        return
    n.value = "0 entries" if "Nothing" in str(s) else str(s)
    n.observation = ("No tRFC entries pending or in error for the selection." if "Nothing" in str(s)
                     else f"tRFC monitor: {s}.")


def _st06(n, f, ocr):
    idle = _num(_grab(r"Idle\s*(\d+)\s*%", ocr))
    cpus = _num(_grab(r"Number of CPUs\s*(\d+)", ocr))
    load = _grab(r"Average processes waiting.*?(\d+[.,]\d+)", ocr)
    host = _grab(r"Hostname\s*(\S+)", ocr)
    if idle is None and cpus is None:
        return
    parts = []
    if idle is not None:
        parts.append(f"CPU {100 - idle}% busy ({idle}% idle)")
    if cpus is not None:
        parts.append(f"{cpus} CPUs")
    if load:
        parts.append(f"load {load}")
    if host:
        parts.append(f"host {host}")
    n.value = f"{100 - idle}% CPU" if idle is not None else ""
    n.observation = ", ".join(parts) + "."
    if idle is not None and idle < 20:
        n.status = "WARNING"
        n.recommendation = "CPU over 80% busy at capture; check top processes (ST06 → Top 40) and SM66 for runaway work processes."


def _sm21(n, f, ocr):
    ids = re.findall(r"\b([A-Z]\d{2}|[A-Z]{2}\d)\b\s+(?=[A-Z][a-z])", ocr)
    total = len(re.findall(r"\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}", ocr))
    softcancel = len(re.findall(r"SOFTCANCEL|Softcancel", ocr))
    dumps_like = len(re.findall(r"\bR68\b|\bAB0\b|\bAB1\b|runtime error", ocr, re.I))
    if not total and not ids:
        return
    n.value = f"{total} messages"
    n.observation = f"{total} syslog message(s) visible on the captured page"
    extras = []
    if softcancel:
        extras.append(f"{softcancel} GUI soft-cancel(s)")
    if dumps_like:
        extras.append(f"{dumps_like} runtime-error message(s)")
    n.observation += (" including " + ", ".join(extras) + "." if extras else ".")
    if dumps_like:
        n.status = "WARNING"
        n.recommendation = "Cross-check runtime-error syslog entries with ST22 for the same timestamps."


def _generic(n, f, ocr):
    parts = []
    for k, v in f.items():
        if isinstance(v, (str, int, float)) and str(v).strip():
            parts.append(f"{k.replace('_', ' ').title()}: {v}")
    if parts:
        n.observation = "; ".join(parts[:6]) + "."
    elif ocr and _is_legible(ocr):
        n.observation = "Screen captured. Recognised text: " + ocr[:220].rstrip() + ("…" if len(ocr) > 220 else "")
    else:
        n.observation = NO_READABLE_TEXT


_HANDLERS = {
    "ST22": _st22, "SM12": _sm12, "SM13": _sm13, "SP01": _sp01, "SM37": _sm37,
    "SM37_CANCELLED": _sm37_cancelled, "AL08": _al08, "SM51": _sm51, "SM66": _sm66,
    "SM50": _sm50, "SCOT": _scot, "ST03N": _st03n, "SMLG": _smlg, "SOST": _sost,
    "DB12": _db12, "DB01": _db01, "DB02": _db02, "SMQ1": _smq, "SMQ2": _smq,
    "SM58": _sm58, "ST06": _st06, "SM21": _sm21,
}


def _richness(n: CheckNarrative) -> tuple:
    """
    How much a narrative actually says, for choosing between duplicates.
    A parsed figure beats a bare screen capture; a real sentence beats the
    'could not be read' fallback.
    """
    unreadable = n.observation in ("", NO_READABLE_TEXT,
                                   "Screen captured; no figures could be read from it.")
    if n.status == "UNKNOWN" and not n.result:
        unreadable = True
    # The capture record always wins over a metric derived from it. SMLG
    # sends both; the derived one carries no instance list, read "not
    # measured", and won on text length -- so the PDF said NOT MEASURED
    # beside an Excel row reading 893 ms / 519 ms.
    return (1 if n.is_evidence else 0,
            0 if unreadable else 1,
            1 if n.value else 0,
            1 if n.recommendation else 0,
            len(n.observation))


def narrate_all(gui_results) -> list[CheckNarrative]:
    """
    One narrative per T-code, in capture order.

    A T-code can produce more than one MetricResult in a cycle -- SMLG, for
    instance, emits both a structured read and an OCR read, which is how a
    client report ended up with two SMLG rows, one of them saying only
    "Ocr Source: True". Reports show one row per check, so duplicates are
    collapsed here: the narrative that says the most wins, and screenshots
    from every capture of that T-code are kept so no evidence is lost.
    """
    best: dict[str, CheckNarrative] = {}
    order: list[str] = []
    for m in (gui_results or []):
        if not getattr(m, "tcode", None):
            continue
        n = narrate(m)
        prev = best.get(n.tcode)
        if prev is None:
            best[n.tcode] = n
            order.append(n.tcode)
            continue
        keep, drop = ((n, prev) if _richness(n) > _richness(prev) else (prev, n))
        for shot in drop.screenshots:
            if shot not in keep.screenshots:
                keep.screenshots.append(shot)
        if not keep.evidence_id:
            keep.evidence_id = drop.evidence_id
        best[n.tcode] = keep
    return [best[t] for t in order]


def summary_counts(narratives: list[CheckNarrative]) -> dict:
    return {
        "captured": sum(1 for n in narratives if n.status != "FAILED"),
        "failed": sum(1 for n in narratives if n.status == "FAILED"),
        "ok": sum(1 for n in narratives if n.status == "OK"),
        "warning": sum(1 for n in narratives if n.status == "WARNING"),
        "critical": sum(1 for n in narratives if n.status == "CRITICAL"),
        "unknown": sum(1 for n in narratives if n.status == "UNKNOWN"),
        "attention": sum(1 for n in narratives if n.status in ("WARNING", "CRITICAL")),
        "total": len(narratives),
    }


# --------------------------------------------------------------------------
# Cross-checks between the RFC metric stream and the captured screens.
#
# Only metrics that measure the SAME thing as a screen figure are compared.
# The old rule flagged any RFC breach whose T-code screen read OK -- so
# sap.sm12.oldest_lock_minutes (a lock AGE) was declared to "disagree" with
# SM12's lock COUNT, even though the screenshot's first row showed the very
# lock the metric named. Different dimensions are not a conflict; they are
# simply separate findings.
#
# mode "equal": the two figures should match.
# mode "within": the RFC figure is a subset of the screen total (per-user
#                maximum vs all locks) and cannot legitimately exceed it.
# --------------------------------------------------------------------------
_COMPARABLE = {
    "sap.st22.dump_count": ("ST22", "dump_count", "equal", "ABAP dumps"),
    "sap.sm58.stuck_entries": ("SM58", "failed_entries", "equal", "failed tRFC entries"),
    "sap.sm37.cancelled_jobs": ("SM37_CANCELLED", "cancelled_job_count", "equal", "cancelled jobs"),
    "sap.sm13.update_errors": ("SM13", "update_count", "equal", "update requests"),
    "sap.sm12.lock_count": ("SM12", "lock_count", "equal", "lock entries"),
    "sap.sm12.locks_per_user_max": ("SM12", "lock_count", "within", "lock entries"),
}


def _metric_number(metric):
    value = getattr(metric, "value", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _cross_checks(result, gui_by_tcode: dict) -> list[str]:
    notes = []
    for m in getattr(result, "metrics", []) or []:
        spec = _COMPARABLE.get(str(getattr(m, "name", "")))
        if not spec:
            continue
        tcode, field_name, mode, noun = spec
        gui = gui_by_tcode.get(tcode)
        if gui is None:
            continue
        data = dict(getattr(gui, "extra_data", {}) or {})
        screen = _num(data.get(field_name))
        if screen is None and isinstance(data.get("information"), dict):
            screen = _num(data["information"].get(field_name))
        rfc = _metric_number(m)
        if screen is None or rfc is None:
            continue
        if (spec[0] == "SM37_CANCELLED"
                and (getattr(m, "extra_data", {}) or {}).get("scope") == "today"
                and rfc <= screen):
            # The RFC read covers jobs scheduled today; the screen covers
            # since yesterday, so the screen seeing more is expected.
            continue
        if spec[0] == "SM58" and (getattr(m, "extra_data", {}) or {}).get("scope") == "today":
            # Same scope as the screen now. A difference of one or two is the
            # few seconds between the two reads, not a disagreement.
            if abs(rfc - screen) > 2:
                notes.append(
                    f"[SM58] RFC reads {rfc:,.0f} failed tRFC entries dated today; the SM58 "
                    f"screen lists {screen:,}. Confirm in SM58.")
            continue
        if spec[0] == "SM58" and rfc != screen:
            limit_hit = rfc >= 500
            rfc_text = "at least 500" if limit_hit else f"{rfc:,.0f}"
            capped = " (the read stops at 500 rows)" if limit_hit else ""
            notes.append(
                f"[SM58] RFC counts {rfc_text} SYSFAIL tRFC entries across all dates{capped}; "
                f"the SM58 screen, with its default date selection, lists {screen:,}. "
                f"Review the older backlog in SM58 with a wider date range.")
            continue
        if mode == "equal" and rfc != screen:
            notes.append(
                f"[{tcode}] RFC reads {rfc:,.0f} {noun}; the {tcode} screen shows "
                f"{screen:,}. The two reads differ in scope or timing -- confirm "
                f"in {tcode} with a wider selection before acting on either.")
        elif mode == "within" and rfc > screen + max(2, screen * 0.05):
            # A lock or two apart is the seconds between the two reads
            # (PS4 23.09: "13 locks, more than the 12 on the screen").
            notes.append(
                f"[{tcode}] RFC reports one user holding {rfc:,.0f} locks, more than "
                f"the {screen:,} {noun} on the {tcode} screen. The reads were taken "
                f"at different moments or cover different clients -- confirm in {tcode}.")
    return notes


def _correlated_incidents(gui_by_tcode: dict) -> list[str]:
    """
    Failed tRFC entries (SM58) and ABAP dumps (ST22) at the same second are
    one incident. On PS4 all four SM58 failures -- "Syntax error in program
    ZFI_WF_APP_DOA_CL" -- matched four SYNTAX_ERROR dumps by SAP_WFRT to
    the second, and the report listed them as two unrelated findings.
    """
    from datetime import datetime

    sm58, st22 = gui_by_tcode.get("SM58"), gui_by_tcode.get("ST22")
    if sm58 is None or st22 is None:
        return []
    entries = [e for e in (getattr(sm58, "extra_data", {}) or {}).get("trfc_rows") or []
               if isinstance(e, dict)]
    dumps = [d for d in (getattr(st22, "extra_data", {}) or {}).get("dumps") or []
             if isinstance(d, dict)]

    def stamp(item):
        try:
            return datetime.strptime(f"{item.get('date', '')} {item.get('time', '')}",
                                     "%d.%m.%Y %H:%M:%S")
        except ValueError:
            return None

    used, pairs = set(), []
    for entry in entries:
        when = stamp(entry)
        if when is None:
            continue
        for index, dump in enumerate(dumps):
            other = stamp(dump)
            if index not in used and other and abs((when - other).total_seconds()) <= 5:
                used.add(index)
                pairs.append((entry, dump, other))
                break
    if not pairs:
        return []

    groups = {}
    for entry, dump, when in pairs:
        key = (entry.get("status_text") or "tRFC error", dump.get("runtime_error") or "dump")
        groups.setdefault(key, []).append((entry, dump, when))

    notes = []
    for (status_text, runtime_error), items in groups.items():
        times = ", ".join(sorted(w.strftime("%H:%M:%S") for _, _, w in items))
        modules = sorted({e.get("function_module") for e, _, _ in items if e.get("function_module")})
        users = sorted({d.get("user") for _, d, _ in items if d.get("user")})
        notes.append(
            f"[SM58 + ST22] {len(items)} of {len(entries)} failed tRFC entries match "
            f"{runtime_error} dumps to the second ({times}): one incident, "
            f"\"{status_text}\""
            + (f" in {', '.join(modules)}" if modules else "")
            + (f", user {', '.join(users)}" if users else "")
            + ". Fix the program, then re-process the SM58 entries.")
    return notes


# What SM66 / SM51 run to build their own lists. Under the IBOPS logon user
# this is the monitoring session itself, and it holds no locks.
_MONITOR_PROGRAMS = ("CL_SERVER_INFO",)


def _monitoring_users(result) -> set:
    """The logon users IBOPS itself uses on this system (systems.yaml)."""
    try:
        from reporting.excel_template_writer import _system_config

        cfg = _system_config(getattr(result, "system", "")) or {}
    except Exception:
        cfg = {}
    rfc = cfg.get("rfc") if isinstance(cfg.get("rfc"), dict) else {}
    users = {str(cfg.get("username") or ""), str(rfc.get("user") or rfc.get("username") or "")}
    return {u.strip().upper() for u in users if u and u.strip()}


def _lock_holder_notes(result, gui_by_tcode: dict) -> list[str]:
    """
    The user holding the most SM12 locks, and what that user is running in
    SM66.

    IBOPS's own processes are left out. On PS4 the top holder was PS4_ADMIN
    -- the account IBOPS logs on with -- and the report named its
    CL_SERVER_INFO process (SM66 building its own list) as what the user was
    running, while the same account had two background jobs
    (/UI5/APP_INDEX_CALCULATE, SAPLSENA) that were far likelier holders.
    Background jobs are listed first.
    """
    metric = next((m for m in getattr(result, "metrics", []) or []
                   if str(getattr(m, "name", "")) == "sap.sm12.locks_per_user_max"), None)
    status = getattr(getattr(metric, "status", None), "value", str(getattr(metric, "status", "")))
    if metric is None or status not in ("WARNING", "CRITICAL"):
        return []
    match = re.match(r"\s*([^\s,]+)\s+(\d[\d.,]*)", str(getattr(metric, "detail", "") or ""))
    if not match:
        return []
    user, held = match.group(1), _num(match.group(2))
    if not held:
        return []

    sm12 = gui_by_tcode.get("SM12")
    total = _num((getattr(sm12, "extra_data", {}) or {}).get("lock_count")) if sm12 else None
    if total is not None and held > total:
        # The RFC read and the screen were taken at different moments or
        # cover different clients (PS4 15:09: "CPIUSER holds 358 of 166").
        # Linking them would state something impossible; the SM12
        # cross-check already reports the mismatch.
        return []

    sm66 = gui_by_tcode.get("SM66")
    rows = [r for r in ((getattr(sm66, "extra_data", {}) or {}).get("process_rows") or [])
            if isinstance(r, dict)] if sm66 else []

    def cell(row, *names):
        lowered = {str(k).lower(): v for k, v in row.items()}
        for name in names:
            value = lowered.get(name.lower())
            if value not in (None, ""):
                return str(value).strip()
        return ""

    def describe(row):
        program = cell(row, "WP_PROGRAM", "program") or "an unnamed program"
        where = " ".join(x for x in (cell(row, "WP_TYPE_DISP", "wp_type"), "work process",
                                     cell(row, "WP_INDEX", "wp_index")) if x)
        server = cell(row, "SERVER_NAME", "server_name")
        state = cell(row, "STATE_DISP", "state")
        return (f"{program} in {where}" + (f" on {server}" if server else "")
                + (f" ({state})" if state else ""))

    mine = [r for r in rows if cell(r, "USER_NAME", "user").upper() == user.upper()]
    own = [r for r in mine if cell(r, "WP_PROGRAM", "program").upper().startswith(_MONITOR_PROGRAMS)]
    work = [r for r in mine if r not in own]
    work.sort(key=lambda r: 0 if cell(r, "WP_TYPE_DISP", "wp_type").upper().startswith("BTC") else 1)
    monitor_account = user.upper() in _monitoring_users(result)

    text = (f"[SM12 + SM66] User {user}"
            + (" (the account IBOPS logs on with)" if monitor_account else "")
            + f" holds {held:,} lock entries" + (f" of {total:,}" if total else ""))
    if work:
        text += " and is running " + "; ".join(describe(r) for r in work[:2])
        if len(work) > 2:
            text += f", plus {len(work) - 2} more work process(es)"
        text += ". Find out what these are doing before deleting any of the locks."
    elif own:
        text += ("; its only active work processes are IBOPS's own monitoring session, "
                 "which holds no locks. Check the lock age and owner session in SM12 "
                 "before deleting.")
    elif sm66 is not None:
        text += ("; SM66 shows no active work process for this user at capture time. Check the "
                 "lock age in SM12 and the user's session before deleting.")
    else:
        text += "."
    if monitor_account:
        text += (" A dedicated monitoring user would keep IBOPS's own activity apart from "
                 "this account's jobs.")
    return [text]


_RANK = {"OK": 0, "NORMAL": 0, "UNKNOWN": 1, "WARNING": 2, "CRITICAL": 3}


def report_status(result, narratives: list[CheckNarrative]) -> str:
    """
    The one status the report states, in the header and in the analysis.

    Worst of the pipeline's overall status and the worst check. The PDF
    used to print "Overall Status: CRITICAL" at the top and "Severity:
    WARNING" (the model's opinion) in the analysis two pages later.
    """
    overall = getattr(getattr(result, "overall_status", None), "value",
                      str(getattr(result, "overall_status", "") or "UNKNOWN")).upper()
    worst = overall if overall in _RANK else "UNKNOWN"
    for n in narratives:
        if _RANK.get(n.status, 0) > _RANK.get(worst, 0):
            worst = n.status
    return "OK" if worst == "NORMAL" else worst


def deterministic_analysis(result, narratives: list[CheckNarrative], gui_results=None) -> dict:
    """
    A rules-based analysis for the report when the model is unavailable --
    and the structured evidence the model is given when it is available.
    Never invents: every line traces to a metric status or a capture fact.
    """
    gui_by_tcode = {}
    for g in gui_results or []:
        code = (getattr(g, "tcode", "") or "").upper()
        if code and code not in gui_by_tcode:
            gui_by_tcode[code] = g

    metric_findings, metric_rows, check_findings, actions = [], [], [], []

    for m in getattr(result, "metrics", []) or []:
        st = getattr(getattr(m, "status", None), "value", str(getattr(m, "status", "")))
        if st not in ("CRITICAL", "WARNING"):
            continue
        detail = str(getattr(m, "detail", "") or "")
        source = ("screen" if str(getattr(m, "source", "") or "") == "sap_gui_collector"
                  else "RFC")
        metric_findings.append(f"[{st}] {m.name} = {m.display_value}"
                               + (f" — {detail}" if detail else "") + f" ({source} metric)")
        metric_rows.append({"status": st, "name": str(m.name), "source": source,
                            "tcode": str(getattr(m, "tcode", "") or "").upper(),
                            "value": str(m.display_value or ""), "detail": detail})

    conflicts = _cross_checks(result, gui_by_tcode)
    incidents = _correlated_incidents(gui_by_tcode) + _lock_holder_notes(result, gui_by_tcode)

    for n in narratives:
        if n.status in ("WARNING", "CRITICAL", "FAILED"):
            check_findings.append(f"[{n.tcode}] {n.result or n.observation}")
            if n.recommendation:
                actions.append(f"{n.tcode}: {n.recommendation}")

    findings = incidents + metric_findings + conflicts + check_findings
    counts = summary_counts(narratives)
    # Screen-sourced metrics repeat a check row; count RFC breaches only.
    rfc_rows = [r for r in metric_rows if r["source"] == "RFC"]
    counts["metric_findings"] = len(rfc_rows)
    counts["conflicts"] = len(conflicts)
    counts["incidents"] = len(incidents)
    crit = sum(1 for r in rfc_rows if r["status"] == "CRITICAL")
    counts["metric_critical"] = crit

    severity = report_status(result, narratives)
    if not findings:
        headline = (f"All {counts['captured']} captured checks are within normal ranges; "
                    f"no metric is critical or warning this cycle.")
    else:
        bits = [f"{counts['attention'] + counts['failed']} of {counts['total']} "
                f"checks need attention"]
        if incidents:
            bits.insert(0, f"{len(incidents)} correlated incident(s)")
        if rfc_rows:
            bits.append(f"{len(rfc_rows)} RFC metric(s) over threshold ({crit} critical)")
        if counts["unknown"]:
            bits.append(f"{counts['unknown']} check(s) not measured")
        if conflicts:
            bits.append(f"{len(conflicts)} cross-check(s) to confirm")
        headline = "; ".join(bits) + "."
    return {"severity": severity, "headline": headline,
            "findings": findings, "actions": actions, "counts": counts,
            "conflicts": conflicts, "metric_rows": metric_rows, "incidents": incidents,
            "check_findings": check_findings}
