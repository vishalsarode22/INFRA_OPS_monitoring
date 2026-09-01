"""
Fills the InfraBeatOps Advanced SAP Monitoring workbook from a
MonitoringResult.

WHY THE TEMPLATE IS THE AUTHORITY
---------------------------------
The workbook's Configuration sheet is a threshold registry keyed by the same
canonical metric names the collectors emit (cpu, memory, sap.sm12.lock_count,
sap.smlg.response_time ...). That makes it the natural place for an operator
to tune thresholds without touching Python.

So thresholds are READ FROM the workbook, not written to it. Before this,
thresholds lived in three places -- config/thresholds.yaml, the _THRESHOLDS
dict in collectors/rfc_collector.py, and this sheet -- and nothing kept them
in agreement. `load_thresholds()` lets the rest of the pipeline defer to the
sheet, with the Python defaults as a fallback for metrics it does not list.

WHAT IS NEVER WRITTEN
---------------------
A check with no collected value is left BLANK and marked NOT COLLECTED. It is
never written as 0 and never marked HEALTHY. Roughly half the checklist rows
(SICK, SE06, SLICENSE, OAC0, SPAD, SMQR/SMQS) have no collector at all, and a
workbook that shows them green would be lying in a format people archive and
send to management.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.styles import Alignment, Font, PatternFill

from core.models import MonitoringResult, Status

TEMPLATE_NAME = "InfraBeatOps_Advanced_SAP_Monitoring_Template.xlsx"


# ---------------------------------------------------------------------------
# Checklist row -> metric name(s)
# ---------------------------------------------------------------------------
#
# Several candidates per check: the first one present wins, so a row fills
# from whichever collector reached the system (RFC, SSH or SAP GUI).
# A check with no candidates present stays blank -- see NOT COLLECTED above.

CHECK_METRICS: dict[str, list[str]] = {
    "DB-01":     ["sap.db02.free_pct", "sap.db02.used_pct"],
    "DB-03":     ["sap.db12.last_backup"],
    "SYS-01":    ["sap.sm21.errors"],
    "SYS-02":    ["sap.sm12.lock_count"],
    "SYS-03":    ["sap.al08.user_logons", "sap.al08.backend_sessions"],
    "SYS-04":    ["sap.sm51.instances_started"],
    "SYS-05":    ["sap.sm66.wp_saturation_pct", "sap.sm66.running_processes",
                  "sap.sm50.free_dia_wp"],
    "SMLG-01":   ["sap.smlg.response_time"],
    "PERF-01":   ["sap.st03n.dialog_response_time"],
    "JOB-01":    ["sap.sm37.active_jobs"],
    "JOB-02":    ["sap.sm37.cancelled_jobs"],
    "UPD-01":    ["sap.sm13.failed_updates"],
    "RFC-01":    ["sap.sm58.stuck_entries"],
    "QRF-01":    ["sap.smq1.entries"],
    "QRF-02":    ["sap.smq2.entries"],
    "CONN-01":   ["sap_process_icman"],
    "CONN-02":   ["sap_process_gwrd"],
    "MAIL-02":   ["sap.sost.errors", "sap.sost.send_requests"],
    "SPOOL-01":  ["sap.sp01.spool_count"],
    "DUMP-01":   ["sap.st22.dumps", "sap.st22.dump_count"],
    "OS-01":     ["disk_/", "disk_/usr/sap", "disk_/sapmnt"],
    "OS-02":     ["cpu", "memory", "load_1m"],
}

# Column letters on the Monitoring Checklist sheet (header row 2).
COL = {
    "check_id": "A", "category": "B", "task": "C", "tcode": "D",
    "objective": "E", "value": "F", "unit": "G", "threshold": "H",
    "status": "I", "reason": "J", "owner": "K", "timestamp": "L",
    "host": "M", "evidence_id": "N", "screenshot": "O",
    "ai_finding": "P", "action": "Q",
}

FILL = {
    "HEALTHY":       PatternFill("solid", fgColor="D1FADF"),
    "WARNING":       PatternFill("solid", fgColor="FEF0C7"),
    "CRITICAL":      PatternFill("solid", fgColor="FEE4E2"),
    "NOT COLLECTED": PatternFill("solid", fgColor="EAECF0"),
}
FONT = {
    "HEALTHY":       Font(color="027A48", bold=True, size=10),
    "WARNING":       Font(color="B54708", bold=True, size=10),
    "CRITICAL":      Font(color="B42318", bold=True, size=10),
    "NOT COLLECTED": Font(color="667085", italic=True, size=10),
}

_STATUS_LABEL = {
    Status.NORMAL: "HEALTHY",
    Status.WARNING: "WARNING",
    Status.CRITICAL: "CRITICAL",
    Status.UNKNOWN: "NOT COLLECTED",
}


def _header_row(ws, first_header: str) -> int:
    """
    Finds the row holding the column headers, and raises if it cannot.

    Every sheet has a merged title banner, so headers do not start at row 1.
    An earlier draft defaulted to row 2 when the search failed, which made
    the writer treat the header row as data -- the first output had
    "Check ID / Collected Value / Status" written as a checklist entry and
    counted in the status totals. Failing loudly is better: a silently
    corrupted report is worse than no report.
    """
    target = first_header.strip().lower()
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 20), max_col=4):
        for cell in row:
            if cell.value and str(cell.value).strip().lower() == target:
                return cell.row
    raise ValueError(
        f"Sheet '{ws.title}': header '{first_header}' not found in the first "
        f"20 rows. The template layout has changed; update CHECK_METRICS/COL."
    )


def template_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "config", "templates", TEMPLATE_NAME)


# ---------------------------------------------------------------------------
# Thresholds: the workbook is the source of truth
# ---------------------------------------------------------------------------

def load_thresholds(path: str | None = None) -> dict[str, dict]:
    """
    Reads the Configuration sheet into {metric: {warning, critical, unit,
    direction}}.

    `direction` matters: LOWER means a smaller number is worse (availability
    percentages). Grading every metric as higher-is-worse is how an
    availability of 100% once graded as RED_ALERT in the previous codebase.
    """
    path = path or template_path()
    if not os.path.exists(path):
        return {}

    wb = load_workbook(path, data_only=True)
    if "Configuration" not in wb.sheetnames:
        return {}

    out = {}
    for row in wb["Configuration"].iter_rows(min_row=3, values_only=True):
        name = row[0]
        if not name or str(name).strip().lower().startswith("metric"):
            continue
        try:
            out[str(name).strip()] = {
                "warning": float(row[1]) if row[1] is not None else None,
                "critical": float(row[2]) if row[2] is not None else None,
                "unit": (row[3] or "").strip(),
                "direction": (row[4] or "HIGHER").strip().upper(),
            }
        except (TypeError, ValueError):
            continue
    wb.close()
    return out


def grade(metric: str, value, thresholds: dict) -> Status:
    """Grades a value against the workbook's thresholds."""
    if value is None:
        return Status.UNKNOWN
    rule = thresholds.get(metric)
    if not rule:
        return Status.UNKNOWN
    try:
        v = float(value)
    except (TypeError, ValueError):
        return Status.UNKNOWN

    warn, crit = rule.get("warning"), rule.get("critical")
    if rule.get("direction") == "LOWER":
        if crit is not None and v <= crit:
            return Status.CRITICAL
        if warn is not None and v <= warn:
            return Status.WARNING
    else:
        if crit is not None and v >= crit:
            return Status.CRITICAL
        if warn is not None and v >= warn:
            return Status.WARNING
    return Status.NORMAL


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _metric_index(result: MonitoringResult) -> dict:
    return {m.name: m for m in result.metrics}


def _pick(metrics: dict, candidates: list[str]):
    for name in candidates:
        if name in metrics:
            return metrics[name]
    # Prefix match covers per-mount names like "disk_/usr/sap".
    for name in candidates:
        for key, metric in metrics.items():
            if key.startswith(name):
                return metric
    return None


def _evidence_id(result: MonitoringResult, metric) -> str:
    existing = (metric.extra_data or {}).get("evidence_id")
    if existing:
        return str(existing)
    stamp = result.cycle_timestamp.strftime("%Y%m%d-%H%M%S")
    tcode = (metric.tcode or "OS").upper()
    # The metric name is part of the ID: without it, every metric sharing a
    # T-code in one cycle got the SAME evidence ID, so the Evidence Register
    # had colliding keys and AI findings attached to the wrong row.
    slug = re.sub(r"[^A-Za-z0-9]+", "", metric.name.replace("sap.", ""))[:14].upper()
    return f"EV-{result.system}-{tcode}-{stamp}-{slug}"


def write_workbook(result: MonitoringResult, output_path: str,
                   company: str = "", analyst: str = "",
                   window: str = "", environment: str = "") -> str:
    """
    Fills a copy of the template and returns the written path.

    The template is copied, never modified in place -- overwriting it would
    destroy the Dashboard sheet's COUNTIF formulas on the first run.
    """
    src = template_path()
    if not os.path.exists(src):
        raise FileNotFoundError(f"Template not found: {src}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    shutil.copyfile(src, output_path)

    wb = load_workbook(output_path)
    thresholds = load_thresholds(src)
    metrics = _metric_index(result)
    ts = result.cycle_timestamp

    # ---- Dashboard -------------------------------------------------------
    #
    # Cells are located by their LABEL rather than hardcoded coordinates.
    # The sheet has a merged A1:H2 title banner, so the metadata block starts
    # at row 4, not row 2 -- and writing to a merged cell raises. Looking the
    # label up means the writer survives someone inserting a row.
    dash = wb["Dashboard"]

    def set_by_label(label: str, value):
        target = str(label).strip().lower()
        for row in dash.iter_rows(min_row=1, max_row=dash.max_row, max_col=2):
            cell = row[0]
            if cell.value and str(cell.value).strip().lower() == target:
                value_cell = dash.cell(row=cell.row, column=2)
                if not isinstance(value_cell, MergedCell):
                    value_cell.value = value
                return True
        return False

    set_by_label("Company Name", company or "—")
    set_by_label("System Name", result.system)
    set_by_label("SAP SID", (result.metrics[0].extra_data or {}).get("sid", result.system)
                 if result.metrics else result.system)
    set_by_label("Client", result.client)
    set_by_label("Environment", environment or "—")
    set_by_label("Monitoring Date", ts.strftime("%Y-%m-%d"))
    set_by_label("Monitoring Window", window or "—")
    set_by_label("Analyst", analyst or "InfraBeatOps (automated)")
    # Total/Healthy/Warning/Critical are COUNTIF formulas over the checklist,
    # so they recalculate themselves. Nothing to write.

    # ---- Monitoring Checklist -------------------------------------------
    ws = wb["Monitoring Checklist"]
    filled = skipped = 0
    first_data_row = _header_row(ws, "Check ID") + 1

    for row in range(first_data_row, ws.max_row + 1):
        check_id = ws[f"{COL['check_id']}{row}"].value
        if not check_id:
            continue
        check_id = str(check_id).strip()

        metric = _pick(metrics, CHECK_METRICS.get(check_id, []))
        if metric is None:
            # No collector produced this check. Leave the value blank so the
            # sheet cannot be read as "measured and fine".
            ws[f"{COL['status']}{row}"] = "NOT COLLECTED"
            ws[f"{COL['status']}{row}"].fill = FILL["NOT COLLECTED"]
            ws[f"{COL['status']}{row}"].font = FONT["NOT COLLECTED"]
            ws[f"{COL['reason']}{row}"] = "No collector configured for this check"
            skipped += 1
            continue

        # Prefer the workbook's threshold over the collector's own grade, so
        # an operator retuning the Configuration sheet actually changes the
        # outcome. Fall back to the collector where the sheet is silent.
        status = grade(metric.name, metric.value, thresholds)
        if status is Status.UNKNOWN:
            status = metric.status
        label = _STATUS_LABEL.get(status, "NOT COLLECTED")

        ws[f"{COL['value']}{row}"] = metric.display_value if metric.value is None else metric.value
        if metric.unit:
            ws[f"{COL['unit']}{row}"] = metric.unit

        rule = thresholds.get(metric.name)
        if rule:
            arrow = "<=" if rule.get("direction") == "LOWER" else ">="
            ws[f"{COL['threshold']}{row}"] = (
                f"warn {arrow} {rule['warning']:g} · crit {arrow} {rule['critical']:g}"
                if rule.get("warning") is not None and rule.get("critical") is not None else ""
            )

        cell = ws[f"{COL['status']}{row}"]
        cell.value = label
        cell.fill = FILL[label]
        cell.font = FONT[label]
        cell.alignment = Alignment(horizontal="center")

        if label != "HEALTHY":
            ws[f"{COL['reason']}{row}"] = (
                metric.detail or f"{metric.name} = {metric.display_value}"
            )[:300]

        ws[f"{COL['timestamp']}{row}"] = ts.strftime("%Y-%m-%d %H:%M:%S")
        ws[f"{COL['host']}{row}"] = (metric.extra_data or {}).get("host", "")
        ws[f"{COL['evidence_id']}{row}"] = _evidence_id(result, metric)
        if metric.screenshot_path:
            ws[f"{COL['screenshot']}{row}"] = metric.screenshot_path
        # Column P (AI Finding) and Q (Recommended Action) are filled by
        # write_ai_analysis(); left blank when no analysis ran.
        filled += 1

    # ---- Evidence Register ----------------------------------------------
    ev = wb["Evidence Register"]
    r = _header_row(ev, "Evidence ID") + 1
    for metric in result.metrics:
        if not (metric.screenshot_path or metric.screenshot_paths or metric.tcode):
            continue
        ev[f"A{r}"] = _evidence_id(result, metric)
        ev[f"B{r}"] = result.system
        ev[f"C{r}"] = result.client
        ev[f"D{r}"] = metric.tcode or ""
        ev[f"E{r}"] = "screenshot" if metric.screenshot_path else "metric"
        ev[f"F{r}"] = ts.strftime("%Y-%m-%d %H:%M:%S")
        ev[f"G{r}"] = metric.screenshot_path or ""
        ev[f"H{r}"] = metric.source or ""
        ev[f"I{r}"] = (metric.detail or metric.display_value)[:400]
        ev[f"K{r}"] = (metric.extra_data or {}).get("host", "")
        # "Integrity / Quality" records whether the reading is trustworthy.
        # An UNKNOWN metric is present in the register but explicitly marked
        # unusable, rather than being silently omitted.
        ev[f"L{r}"] = "unreadable" if metric.value is None and metric.status is Status.UNKNOWN else "ok"
        r += 1

    # ---- Daily History ---------------------------------------------------
    hist = wb["Daily History"]
    r = _header_row(hist, "Date") + 1
    for metric in result.metrics:
        hist[f"A{r}"] = ts.strftime("%Y-%m-%d")
        hist[f"B{r}"] = company or ""
        hist[f"C{r}"] = result.system
        hist[f"D{r}"] = result.client
        hist[f"E{r}"] = window or ""
        hist[f"F{r}"] = metric.tcode or ""
        hist[f"G{r}"] = metric.name
        hist[f"H{r}"] = metric.value if metric.value is not None else metric.display_value
        hist[f"I{r}"] = _STATUS_LABEL.get(metric.status, "NOT COLLECTED")
        hist[f"K{r}"] = _evidence_id(result, metric)
        r += 1

    wb.save(output_path)
    wb.close()
    return output_path


def write_ai_analysis(workbook_path: str, result: MonitoringResult,
                      analysis=None) -> str:
    """
    Writes the AI Analysis sheet and back-fills the checklist's AI Finding /
    Recommended Action columns.

    Deliberate constraints:
      * Only rows that are WARNING or CRITICAL get an analysis. Asking a model
        to explain a healthy reading invites invention.
      * `finding_status` is written verbatim (HYPOTHESIS / CONFIRMED /
        UNKNOWN). A hypothesis must never be presented as a confirmed cause.
      * "Analyst Validation" is left blank for a human to sign off.
    """
    analysis = analysis or result.ai_analysis
    wb = load_workbook(workbook_path)
    ws = wb["AI Analysis"]
    checklist = wb["Monitoring Checklist"]

    if analysis is None:
        r0 = _header_row(ws, "Analysis ID") + 1
        ws.cell(row=r0, column=1, value="—")
        ws.cell(row=r0, column=5, value="AI analysis unavailable for this cycle")
        ws.cell(row=r0, column=9, value=(
            "Monitoring completed normally; only the AI step was skipped or "
            "failed. This says nothing about SAP health."))
        ws.cell(row=r0, column=10, value="N/A")
        wb.save(workbook_path)
        wb.close()
        return workbook_path

    actionable = [m for m in result.metrics
                  if m.status in (Status.WARNING, Status.CRITICAL)]

    # AIAnalysis is produced ONCE PER CYCLE, not per metric. Writing the same
    # root cause against every actionable metric attributes it to findings it
    # never examined -- the first run pasted an SMLG response-time hypothesis
    # onto an unrelated disk-space warning.
    #
    # So the cycle analysis is written as ONE row covering the cycle, listing
    # the findings it was derived from. Per-metric attribution requires
    # per-metric analysis, which the AI layer does not currently produce.
    ts = result.cycle_timestamp.strftime("%Y%m%d-%H%M%S")
    r0 = _header_row(ws, "Analysis ID") + 1

    if not actionable:
        ws.cell(row=r0, column=1, value="—")
        ws.cell(row=r0, column=5,
                value="No WARNING or CRITICAL findings in this cycle")
        ws.cell(row=r0, column=10, value="N/A")
        wb.save(workbook_path)
        wb.close()
        return workbook_path

    finding = getattr(analysis, "finding_status", "UNKNOWN") or "UNKNOWN"
    confidence = getattr(analysis, "confidence", "") or "UNKNOWN"
    score = getattr(analysis, "confidence_score", None)
    actions = list(getattr(analysis, "recommended_actions", []) or [])

    ws.cell(row=r0, column=1, value=f"AI-{result.system}-{ts}")
    ws.cell(row=r0, column=2, value="; ".join(_evidence_id(result, m) for m in actionable)[:400])
    ws.cell(row=r0, column=3, value=", ".join(sorted({m.tcode for m in actionable if m.tcode})))
    ws.cell(row=r0, column=4, value=result.overall_status.value)
    ws.cell(row=r0, column=5, value=(getattr(analysis, "likely_root_cause", "") or "")[:600])
    ws.cell(row=r0, column=8, value=", ".join(m.name for m in actionable)[:300])
    ws.cell(row=r0, column=9, value=(getattr(analysis, "root_cause_category", "") or "")[:200])
    ws.cell(row=r0, column=10,
            value=f"{finding} · {confidence}" +
                  (f" · {score:.0%}" if isinstance(score, (int, float)) else ""))
    ws.cell(row=r0, column=12, value="; ".join(actions[:2])[:300])
    ws.cell(row=r0, column=13, value="; ".join(actions[2:4])[:300])
    # Column 14 (Analyst Validation) stays blank -- a human signs this off.

    # Back-fill the checklist. Every actionable row gets the SAME cycle-level
    # note, explicitly labelled as such so nobody reads it as a per-check
    # diagnosis.
    checklist_start = _header_row(checklist, "Check ID") + 1
    actionable_names = {m.name for m in actionable}
    for c_row in range(checklist_start, checklist.max_row + 1):
        check_id = checklist[f"{COL['check_id']}{c_row}"].value
        if not check_id:
            continue
        candidates = CHECK_METRICS.get(str(check_id).strip(), [])
        if not any(n in actionable_names for n in candidates):
            continue
        checklist[f"{COL['ai_finding']}{c_row}"] = (
            f"[{finding} · cycle-level] "
            f"{(getattr(analysis, 'likely_root_cause', '') or '')[:220]}"
        )
        checklist[f"{COL['action']}{c_row}"] = "; ".join(actions[:2])[:300]

    wb.save(workbook_path)
    wb.close()
    return workbook_path
