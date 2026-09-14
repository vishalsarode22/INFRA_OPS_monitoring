"""
Per-T-code capture narratives for reports.

Turns what the GUI sweep actually captured for each T-code into:

    observation     what was seen, in one or two specific sentences
    status          OK / ATTENTION / FAILED / NOT COLLECTED
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
    status: str                  # OK | ATTENTION | FAILED | NOT COLLECTED
    value: str = ""
    recommendation: str = ""
    captured_at: str = ""
    evidence_id: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)


# Human task names per T-code (used where the template has none).
TASKS = {
    "SM21": "SAP System Log", "ST22": "ABAP Dumps", "SM13": "Update Requests",
    "SM12": "Lock Entries", "SP01": "Spool Requests", "SM37": "Active Background Jobs",
    "SM37_CANCELLED": "Cancelled Background Jobs", "AL08": "User Activities",
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
    return n


# --------------------------------------------------------------------------
# Per-T-code handlers. Each sets observation/status/value/recommendation.
# They read parsed fields first, then mine the OCR excerpt.
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
        n.status = "ATTENTION"
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
        n.status = "ATTENTION"
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
        n.status = "ATTENTION"
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
        n.status = "ATTENTION"
        n.recommendation = "Schedule spool reorganisation (RSPO0041/RSPO1041) and check for stuck output requests."


def _sm37(n, f, ocr):
    c = _num(f.get("active_jobs"))
    if c is None:
        return
    n.value = f"{c} active"
    n.observation = f"{c} background job(s) active at capture time."


def _sm37_cancelled(n, f, ocr):
    c = _num(f.get("cancelled_jobs"))
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
    n.status = "ATTENTION"
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
        n.status = "ATTENTION"
        n.recommendation = "No free dialog work processes -- users will queue. Check long-running dialog steps in SM50."


def _scot(n, f, ocr):
    if "SMTP" not in ocr and not f:
        return
    nodes = re.findall(r"\b([A-Z0-9_]{3,})\s+(?:SMTP|smtp)", ocr)
    n.observation = ("SAPconnect SMTP node configuration captured"
                     + (f"; {len(nodes)} node(s) listed." if nodes else "; the SMTP node list appeared empty on the captured screen."))
    n.value = f"{len(nodes)} nodes" if nodes else "0 nodes"
    if not nodes:
        n.status = "ATTENTION"
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
        n.status = "ATTENTION"
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
        n.status = "ATTENTION"
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
        n.status = "ATTENTION"
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
        n.status = "ATTENTION"
        n.recommendation = "Investigate failed backups in DB12 → Backup Logs before the retention window closes."


def _db01(n, f, ocr):
    if "Blocked Transactions" not in ocr and not f:
        return
    rows = len(re.findall(r"\d{2}:\d{2}:\d{2}", ocr))
    n.value = f"{rows} blocked" if rows else "0 blocked"
    n.observation = ("No blocked transactions / exclusive lock waits at capture time."
                     if rows == 0 else f"{rows} blocked transaction row(s) visible.")
    if rows:
        n.status = "ATTENTION"
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
        n.status = "ATTENTION"
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
        n.status = "ATTENTION"
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
        n.status = "ATTENTION"
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
    return (0 if unreadable else 1,
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
        "attention": sum(1 for n in narratives if n.status == "ATTENTION"),
        "ok": sum(1 for n in narratives if n.status == "OK"),
        "total": len(narratives),
    }


def deterministic_analysis(result, narratives: list[CheckNarrative]) -> dict:
    """
    A rules-based analysis for the report when the model is unavailable --
    and the structured evidence the model is given when it is available.
    Never invents: every line traces to a metric status or a capture fact.
    """
    by_tcode = {n.tcode: n for n in narratives}

    # Threshold findings come from the RFC metric stream; check findings come
    # from the GUI captures. They are separate evidence and are counted
    # separately -- conflating them is how a report said "3 need attention"
    # in the header and "4 item(s) need attention" two sections later.
    metric_findings, check_findings, actions, conflicts = [], [], [], []

    for m in getattr(result, "metrics", []) or []:
        st = getattr(getattr(m, "status", None), "value", str(getattr(m, "status", "")))
        if st not in ("CRITICAL", "WARNING"):
            continue
        line = (f"[{st}] {m.name} = {m.display_value}"
                + (f" — {m.detail}" if getattr(m, "detail", "") else "")
                + " (RFC metric)")
        metric_findings.append(line)

        # An RFC metric named sap.<tcode>.<field> covers the same ground as
        # the GUI capture of that T-code. When the two disagree -- SM58 read
        # 3 stuck entries over RFC while the captured screen showed none --
        # saying so is the report's job. Printing both without comment leaves
        # the reader to notice the contradiction, which is how trust is lost.
        parts = str(getattr(m, "name", "")).split(".")
        tcode = parts[1].upper() if len(parts) > 2 and parts[0] == "sap" else ""
        n = by_tcode.get(tcode)
        if n is not None and n.status == "OK":
            conflicts.append(
                f"[{tcode}] RFC metric {m.name} reads {m.display_value} while the "
                f"captured screen read normal ({n.value or 'no figure'}). The two "
                f"sources disagree; treat this check as unverified until one is "
                f"confirmed at the system.")

    for n in narratives:
        if n.status == "ATTENTION":
            check_findings.append(f"[{n.tcode}] {n.observation}")
            if n.recommendation:
                actions.append(f"{n.tcode}: {n.recommendation}")
        elif n.status == "FAILED":
            check_findings.append(f"[{n.tcode}] {n.observation}")

    findings = metric_findings + conflicts + check_findings
    counts = summary_counts(narratives)
    counts["metric_findings"] = len(metric_findings)
    counts["conflicts"] = len(conflicts)

    if not findings:
        headline = (f"All {counts['captured']} captured checks are within normal ranges; "
                    f"no metric is critical or warning this cycle.")
        severity = "NORMAL"
    else:
        crit = sum(1 for f_ in metric_findings if f_.startswith("[CRITICAL]"))
        severity = "CRITICAL" if crit else "WARNING"
        bits = [f"{counts['attention'] + counts['failed']} of {counts['total']} "
                f"captured checks need attention"]
        if metric_findings:
            bits.append(f"{len(metric_findings)} RFC metric(s) over threshold "
                        f"({crit} critical)")
        if conflicts:
            bits.append(f"{len(conflicts)} source conflict(s)")
        headline = "; ".join(bits) + "."
    return {"severity": severity, "headline": headline,
            "findings": findings, "actions": actions, "counts": counts,
            "conflicts": conflicts}
