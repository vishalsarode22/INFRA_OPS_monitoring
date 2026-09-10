"""
Fills the client-provided MetroBrands Excel template ("System Monitoring"
sheet) with results from the GUI T-code collector. Does not touch
screenshots -- Excel gets text values only, per requirement.
"""

import shutil
from datetime import datetime
import openpyxl
import re

from datetime import datetime

from core.models import MetricResult
from utils.logger import get_logger

log = get_logger(__name__, "application")

NO_DATA_TEXT = "See attached screenshot for details."

# Maps template row number -> our internal tcode identifier
TCODE_ROW_MAP = {
    4: "SM21",
    5: "ST22",
    6: "SM13",
    7: "SM12",
    8: "SP01",
    9: "SM37",
    10: "SM37_CANCELLED",
    11: "AL08",
    12: "SM51",
    13: "SM66",
    14: "SCOT",
    15: "ST03N",
    16: "SMLG",
    17: "SOST",
    18: "DB12",
    19: "DB01",
    20: "DB02",
    21: "SMQ1",
    22: "SMQ2",
    23: "SM58",
}


def _build_check_text(metric: MetricResult) -> str:
    """Builds the 'Checks' column text, phrased to match the client's
    reference style per T-code, using real extracted data where available."""
    data = metric.extra_data
    tcode = metric.tcode

    if not data:
        return NO_DATA_TEXT

    if tcode == "ST22":
        count = data.get("dump_count")
        if count is not None:
            today_str = datetime.now().strftime("%d.%m.%Y")
            return f"{count} Abap Dumps - {today_str}"

    if tcode == "SM13":
        summary = data.get("update_summary")
        if summary:
            # e.g. "0 Update records found" -> "00 Update record / 00 Error"
            match = re.match(r"(\d+)\s*Update records?\s*found", summary, re.IGNORECASE)
            if match:
                n = int(match.group(1))
                return f"{n:02d} Update record / 00 Error"
        return NO_DATA_TEXT

    if tcode == "SM12":
        count = data.get("lock_count")
        if count is not None:
            return f"{count} selected Lock Entries found"

    if tcode == "SP01":
        count = data.get("spool_count_visible")
        if count is not None:
            return f"{count} Spool requests displayed (visible page)"

    if tcode == "SM37":
        count = data.get("active_jobs")
        if count is not None:
            return f"{count} active job found" if count != 1 else "1 active job found"

    if tcode == "SM37_CANCELLED":
        count = data.get("cancelled_jobs")
        if count is not None:
            return f"{count} Cancelled Jobs"

    if tcode == "AL08":
        summary = data.get("session_summary")
        if summary:
            match = re.match(r"(\d+)\s*user logons with\s*(\d+)\s*back-end sessions", summary, re.IGNORECASE)
            if match:
                sessions = match.group(2)
                return f"{sessions} ABAP sessions"
        return NO_DATA_TEXT

    if tcode in ("SMQ1", "SMQ2"):
        entries = data.get("entries_displayed")
        queues = data.get("queues_displayed")
        if entries is not None and queues is not None:
            return (
                "               Queue Information\n"
                f"Number of Entries Displayed                  {entries}\n"
                f"Number of Queues Displayed                   {queues}"
            )
    if tcode == "SM51":
        count = data.get("instances_started")
        if count is not None:
            return f"{count} Application server{'s' if count != 1 else ''} are active"

    if tcode == "SM58":
        status = data.get("trfc_status")
        if status:
            return status

    if tcode == "SM66":
        running = data.get("running_processes")
        if running is not None:
            return f"{running} work process{'es' if running != 1 else ''} in use (Running)"

    if tcode == "SOST":
        send_requests = data.get("send_requests")
        waiting = data.get("waiting")
        sent = data.get("sent")
        errors = data.get("errors")
        if send_requests is not None:
            return (
                f"{send_requests} Send Requests {waiting} Waiting , "
                f"{sent} sent, {errors} Error"
            )

    if tcode == "ST03N":
        rt = data.get("dialog_avg_response_time_ms")
        if rt is not None:
            return f"Avg. dialog response time: {rt} ms"

    # Fallback: generic key/value formatting for anything not specially handled
    parts = []
    for key, value in data.items():
        if value is None:
            continue
        label = key.replace("_", " ").title()
        parts.append(f"{label}: {value}")
    return "; ".join(parts) if parts else NO_DATA_TEXT


def fill_metrobrands_template(template_path: str, output_path: str,
                               gui_results: list[MetricResult],
                               report_date: str = None,
                               result=None) -> str:
    """
    Copies the template and fills the System Monitoring sheet.

    What changed and why: the sheet used to carry the template's placeholder
    header ("System - TST / Test Monitering") on every system's report, and
    the Checks column fell back to dumping every extra_data key -- which is
    where "Evidence Id: EV-..." and SMLG's "Instance Count: 0" came from.
    Every row now goes through reporting.check_narratives, which reads the
    parsed figures AND the OCR text of the captured screen and produces a
    specific observation, a status, a count and a recommendation. Columns
    D-H, previously empty, carry those. A Summary sheet is added.
    """
    from openpyxl.styles import Alignment, Font, PatternFill
    from reporting.check_narratives import (narrate_all, summary_counts,
                                            deterministic_analysis)

    shutil.copy(template_path, output_path)
    wb = openpyxl.load_workbook(output_path)
    ws = wb["System Monitoring"]

    if report_date is None:
        report_date = datetime.now().strftime("%d.%m.%Y")

    narratives = narrate_all(gui_results)
    by_tcode = {n.tcode: n for n in narratives}
    counts = summary_counts(narratives)

    # ---- header: the system this report is actually about ----------------
    sysname = getattr(result, "system", None) or next(
        (m.extra_data.get("system") for m in gui_results if m.extra_data.get("system")), "")
    client = getattr(result, "client", None) or next(
        (m.extra_data.get("client") for m in gui_results if m.extra_data.get("client")), "")
    overall = getattr(getattr(result, "overall_status", None), "value", "") if result else ""
    ws["A1"] = f"System - {sysname}" + (f"  (client {client})" if client else "")
    ws["A2"] = "InfraBeatOps SAP Basis Monitoring"
    ws["C2"] = report_date
    ws["D2"] = f"Overall: {overall}" if overall else ""
    ws["E2"] = (f"{counts['captured']}/{counts['total']} checks captured · "
                f"{counts['attention']} need attention · {counts['failed']} failed")

    # ---- column headers (row 3) ------------------------------------------
    headers = {"C": "Checks (observation)", "D": "Status", "E": "Count / Value",
               "F": "Recommendation", "G": "Captured at", "H": "Evidence ID"}
    bold = Font(bold=True)
    for col, text in headers.items():
        ws[f"{col}3"] = text
        ws[f"{col}3"].font = bold
    widths = {"C": 70, "D": 13, "E": 20, "F": 60, "G": 19, "H": 44}
    for col, w in widths.items():
        ws.column_dimensions[col].width = max(ws.column_dimensions[col].width or 0, w)

    fills = {"OK": "C8E6C9", "ATTENTION": "FFE0B2", "FAILED": "FFCDD2",
             "NOT COLLECTED": "ECEFF1"}
    wrap = Alignment(wrap_text=True, vertical="top")

    for row, tcode in TCODE_ROW_MAP.items():
        n = by_tcode.get(tcode)
        if n is None:
            ws[f"C{row}"] = "Not collected this cycle"
            ws[f"D{row}"] = "NOT COLLECTED"
        else:
            ws[f"C{row}"] = n.observation
            ws[f"D{row}"] = n.status
            ws[f"E{row}"] = n.value
            ws[f"F{row}"] = n.recommendation
            ws[f"G{row}"] = n.captured_at.replace("T", " ")
            ws[f"H{row}"] = n.evidence_id
        st = ws[f"D{row}"].value or "NOT COLLECTED"
        ws[f"D{row}"].fill = PatternFill("solid", fgColor=fills.get(st, "FFFFFF"))
        for col in "CDEFGH":
            ws[f"{col}{row}"].alignment = wrap

    # ---- Summary sheet ------------------------------------------------------
    if "Summary" in wb.sheetnames:
        del wb["Summary"]
    sm = wb.create_sheet("Summary", 0)
    sm.column_dimensions["A"].width = 26
    sm.column_dimensions["B"].width = 110
    r = 1
    def put(k, v, b=False):
        nonlocal r
        sm.cell(row=r, column=1, value=k).font = Font(bold=True)
        c = sm.cell(row=r, column=2, value=v)
        c.alignment = wrap
        if b:
            c.font = Font(bold=True)
        r += 1
    put("System", f"{sysname}" + (f" (client {client})" if client else ""), True)
    put("Report date", report_date)
    put("Overall status", overall or "n/a", True)
    put("Checks", f"{counts['captured']} captured, {counts['ok']} OK, "
                  f"{counts['attention']} need attention, {counts['failed']} failed")
    r += 1
    ai = getattr(result, "ai_analysis", None) if result else None
    da = deterministic_analysis(result, narratives) if result else None
    put("Analysis", "AI analysis" if ai else "Rules-based analysis (model unavailable this cycle)", True)
    if ai:
        put("Severity", getattr(ai, "severity", ""))
        put("Likely root cause", getattr(ai, "likely_root_cause", ""))
        for i, a in enumerate(getattr(ai, "recommended_actions", []) or [], 1):
            put(f"Action {i}", a)
        put("Confidence", str(getattr(ai, "confidence", "")))
    if da:
        put("Headline", da["headline"])
        for i, f_ in enumerate(da["findings"], 1):
            put(f"Finding {i}", f_)
        for i, a in enumerate(da["actions"], 1):
            put(f"Recommended {i}", a)
    r += 1
    put("Checks needing attention", "", True)
    for n in narratives:
        if n.status in ("ATTENTION", "FAILED"):
            put(f"{n.tcode} — {n.task}", f"{n.observation}  →  {n.recommendation}")

    wb.save(output_path)
    log.info(f"MetroBrands Excel template filled: {output_path}")
    return output_path