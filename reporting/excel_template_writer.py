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

    Every row goes through reporting.check_narratives, which reads the
    parsed figures AND the OCR text of the captured screen and produces a
    specific observation, a status, a count and a recommendation. A Summary
    sheet is added.

    Formatting is applied here too (navy header bands, colour-coded status,
    zebra striping, borders, frozen header, autofilter, a KPI strip) so every
    generated report is presentation-ready without a manual pass.
    """
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from reporting.check_narratives import (narrate_all, summary_counts,
                                            deterministic_analysis)
    import textwrap as _tw

    shutil.copy(template_path, output_path)
    wb = openpyxl.load_workbook(output_path)
    ws = wb["System Monitoring"]

    if report_date is None:
        report_date = datetime.now().strftime("%d.%m.%Y")

    narratives = narrate_all(gui_results)
    by_tcode = {n.tcode: n for n in narratives}
    counts = summary_counts(narratives)

    # ------------------------------------------------------------- palette
    NAVY, NAVY_TXT = "1F3864", "FFFFFF"
    SLATE = "44546A"
    LIGHT_BG = "F2F5FA"
    WHITE = "FFFFFF"
    GREY_LINE = "B7C0CC"
    FONT_NAME = "Calibri"
    thin = Side(style="thin", color=GREY_LINE)
    BORDER_ALL = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    wrap_center = Alignment(wrap_text=True, vertical="center", horizontal="center")

    STATUS_STYLE = {
        "OK":            ("C6EFCE", "1E7B34"),
        "ATTENTION":     ("FFE699", "9C6500"),
        "FAILED":        ("FFC7CE", "9C0006"),
        "NOT COLLECTED": ("ECEFF1", "667085"),
    }
    # The SYSTEM-level overall status (result.overall_status.value) uses a
    # different vocabulary -- NORMAL/WARNING/CRITICAL/UNKNOWN from
    # core.models.Status -- than the per-check narrative status above
    # (OK/ATTENTION/FAILED/NOT COLLECTED). Looking "WARNING" up in
    # STATUS_STYLE silently found nothing and left the KPI cell uncoloured;
    # this is the separate mapping for that value specifically.
    OVERALL_STATUS_STYLE = {
        "NORMAL":   ("C6EFCE", "1E7B34"),
        "WARNING":  ("FFE699", "9C6500"),
        "CRITICAL": ("FFC7CE", "9C0006"),
        "UNKNOWN":  ("ECEFF1", "667085"),
    }

    def _est_lines(text, width_chars):
        wrapped = _tw.wrap(str(text), width=max(int(width_chars) - 2, 5))
        return max(1, len(wrapped))

    # ---- header: the system this report is actually about ----------------
    sysname = getattr(result, "system", None) or next(
        (m.extra_data.get("system") for m in gui_results if m.extra_data.get("system")), "")
    client = getattr(result, "client", None) or next(
        (m.extra_data.get("client") for m in gui_results if m.extra_data.get("client")), "")
    overall = getattr(getattr(result, "overall_status", None), "value", "") if result else ""

    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:D1")
    ws["A1"] = f"System - {sysname}" + (f"  (client {client})" if client else "")
    ws["A1"].font = Font(name=FONT_NAME, size=14, bold=True, color=NAVY_TXT)
    ws["A1"].fill = PatternFill("solid", fgColor=NAVY)
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    ws.merge_cells("E1:H1")
    ws["E1"] = f"Report date: {report_date}"
    ws["E1"].font = Font(name=FONT_NAME, size=11, bold=True, color=NAVY_TXT)
    ws["E1"].fill = PatternFill("solid", fgColor=NAVY)
    ws["E1"].alignment = Alignment(horizontal="right", vertical="center")
    ws.row_dimensions[1].height = 26

    ws.merge_cells("A2:H2")
    ws["A2"] = "InfraBeatOps SAP Basis Monitoring"
    ws["A2"].font = Font(name=FONT_NAME, size=10, italic=True, color=WHITE)
    ws["A2"].fill = PatternFill("solid", fgColor=SLATE)
    ws["A2"].alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[2].height = 18

    # ---- KPI strip: overall status + counts, inserted as a new row 3 -----
    ws.insert_rows(3)
    kpi_bg, kpi_txt = OVERALL_STATUS_STYLE.get(overall, (LIGHT_BG, "000000"))
    kpis = [("A3", "Overall status", "B3", overall or "n/a", kpi_bg, kpi_txt),
            ("C3", "Checks run", "D3", str(counts["total"]), LIGHT_BG, "000000"),
            ("E3", "OK", "F3", str(counts["ok"]), *STATUS_STYLE["OK"]),
            ("G3", "Attention", "H3", str(counts["attention"]), *STATUS_STYLE["ATTENTION"])]
    for label_cell, label, val_cell, val, bg, txt in kpis:
        lc = ws[label_cell]
        lc.value = label
        lc.font = Font(name=FONT_NAME, size=9, bold=True, color=SLATE)
        lc.fill = PatternFill("solid", fgColor=LIGHT_BG)
        lc.alignment = wrap_center
        lc.border = BORDER_ALL
        vc = ws[val_cell]
        vc.value = val
        vc.font = Font(name=FONT_NAME, size=12, bold=True, color=txt)
        vc.fill = PatternFill("solid", fgColor=bg)
        vc.alignment = wrap_center
        vc.border = BORDER_ALL
    ws.row_dimensions[3].height = 20

    # ---- column headers (now row 4, after the KPI strip insert) ----------
    HEADER_ROW = 4
    ws[f"A{HEADER_ROW}"] = "Monitoring Task"
    ws[f"B{HEADER_ROW}"] = "Transaction"
    headers = {"C": "Checks (observation)", "D": "Status", "E": "Count / Value",
               "F": "Recommendation", "G": "Captured at", "H": "Evidence ID"}
    for col, text in headers.items():
        ws[f"{col}{HEADER_ROW}"] = text
    for col in "ABCDEFGH":
        c = ws[f"{col}{HEADER_ROW}"]
        c.font = Font(name=FONT_NAME, size=10, bold=True, color=NAVY_TXT)
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = wrap_center
        c.border = BORDER_ALL
    ws.row_dimensions[HEADER_ROW].height = 28

    widths = {"A": 24, "B": 13, "C": 55, "D": 13, "E": 16, "F": 45, "G": 18, "H": 30}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w

    # ---- data rows (shifted down by 1 from the KPI-strip insert) ---------
    LINE_HEIGHT = 14
    MIN_ROW_HEIGHT = 30

    DATA_START = HEADER_ROW + 1
    for i, (orig_row, tcode) in enumerate(TCODE_ROW_MAP.items()):
        row = DATA_START + i
        n = by_tcode.get(tcode)
        task_label = ws[f"A{orig_row + 1}"].value or tcode  # +1: rows shifted by insert_rows(3)
        ws[f"A{row}"] = task_label
        ws[f"B{row}"] = tcode
        if n is None:
            ws[f"C{row}"] = "Not collected this cycle"
            ws[f"D{row}"] = "NOT COLLECTED"
            ws[f"E{row}"] = ""
            ws[f"F{row}"] = ""
            ws[f"G{row}"] = ""
            ws[f"H{row}"] = ""
        else:
            ws[f"C{row}"] = n.observation
            ws[f"D{row}"] = n.status
            ws[f"E{row}"] = n.value
            ws[f"F{row}"] = n.recommendation
            ws[f"G{row}"] = n.captured_at.replace("T", " ")
            ws[f"H{row}"] = n.evidence_id

        st = ws[f"D{row}"].value or "NOT COLLECTED"
        bg, txt = STATUS_STYLE.get(st, (WHITE, "000000"))
        stripe = LIGHT_BG if i % 2 else WHITE

        max_lines = 1
        for col in "ABCDEFGH":
            cell = ws[f"{col}{row}"]
            cell.border = BORDER_ALL
            cell.alignment = wrap
            cell.font = Font(name=FONT_NAME, size=10,
                             bold=(col == "A"),
                             color=(txt if col == "D" else "000000"))
            cell.fill = PatternFill("solid", fgColor=(bg if col == "D" else stripe))
            val = cell.value
            if val:
                w = widths.get(col, 20)
                max_lines = max(max_lines, _est_lines(val, w))
        ws.row_dimensions[row].height = max(MIN_ROW_HEIGHT, max_lines * LINE_HEIGHT + 6)

    LAST_ROW = DATA_START + len(TCODE_ROW_MAP) - 1
    ws.freeze_panes = f"A{DATA_START}"
    ws.auto_filter.ref = f"A{HEADER_ROW}:H{LAST_ROW}"
    ws.page_setup.orientation = "landscape"
    ws.print_title_rows = f"{HEADER_ROW}:{HEADER_ROW}"

    # ---- Summary sheet ------------------------------------------------------
    if "Summary" in wb.sheetnames:
        del wb["Summary"]
    sm = wb.create_sheet("Summary", 0)
    sm.sheet_view.showGridLines = False
    sm.column_dimensions["A"].width = 28
    sm.column_dimensions["B"].width = 100

    sm.merge_cells("A1:B1")
    sm["A1"] = f"Monitoring Summary \u2014 {sysname}" + (f" (client {client})" if client else "")
    sm["A1"].font = Font(name=FONT_NAME, size=14, bold=True, color=NAVY_TXT)
    sm["A1"].fill = PatternFill("solid", fgColor=NAVY)
    sm["A1"].alignment = Alignment(horizontal="left", vertical="center")
    sm.row_dimensions[1].height = 26

    r = 3

    def put_meta(k, v, status_colour=False):
        nonlocal r
        lc = sm.cell(row=r, column=1, value=k)
        lc.font = Font(name=FONT_NAME, size=10, bold=True, color=SLATE)
        lc.fill = PatternFill("solid", fgColor=LIGHT_BG)
        lc.border = BORDER_ALL
        lc.alignment = Alignment(vertical="center")
        vc = sm.cell(row=r, column=2, value=v)
        vc.border = BORDER_ALL
        vc.alignment = Alignment(vertical="center", wrap_text=True)
        if status_colour:
            bg, txt = OVERALL_STATUS_STYLE.get(str(v), (WHITE, "000000"))
            vc.font = Font(name=FONT_NAME, size=10, bold=True, color=txt)
            vc.fill = PatternFill("solid", fgColor=bg)
        else:
            vc.font = Font(name=FONT_NAME, size=10)
            vc.fill = PatternFill("solid", fgColor=WHITE)
        sm.row_dimensions[r].height = 18
        r += 1

    def put_section(title):
        nonlocal r
        sm.merge_cells(f"A{r}:B{r}")
        c = sm.cell(row=r, column=1, value=title)
        c.font = Font(name=FONT_NAME, size=12, bold=True, color=NAVY_TXT)
        c.fill = PatternFill("solid", fgColor=SLATE)
        c.alignment = Alignment(vertical="center")
        sm.row_dimensions[r].height = 20
        r += 1

    def put_line(text, bullet="\u2022", bold=False, bg=None):
        nonlocal r
        sm.merge_cells(f"A{r}:B{r}")
        c = sm.cell(row=r, column=1, value=f"{bullet} {text}" if bullet else text)
        c.font = Font(name=FONT_NAME, size=10, bold=bold,
                     color=("1F3864" if bullet == "\u2192" else "000000"))
        c.fill = PatternFill("solid", fgColor=bg or (LIGHT_BG if (r % 2) else WHITE))
        c.border = BORDER_ALL
        c.alignment = Alignment(vertical="top", wrap_text=True)
        lines = _est_lines(text, 100)
        sm.row_dimensions[r].height = max(18, lines * LINE_HEIGHT + 4)
        r += 1

    ov_status_line = overall or "n/a"
    put_meta("Report date", report_date)
    put_meta("Overall status", ov_status_line, status_colour=True)
    put_meta("Checks", f"{counts['captured']} captured, {counts['ok']} OK, "
                       f"{counts['attention']} need attention, {counts['failed']} failed")
    r += 1

    ai = getattr(result, "ai_analysis", None) if result else None
    da = deterministic_analysis(result, narratives) if result else None

    put_section("Analysis")
    put_line("AI analysis" if ai else "Rules-based analysis (model unavailable this cycle)",
             bullet="", bold=True, bg=WHITE)
    if ai:
        put_line(f"Severity: {getattr(ai, 'severity', '')}", bullet="")
        put_line(f"Likely root cause: {getattr(ai, 'likely_root_cause', '')}", bullet="")
        for a in (getattr(ai, "recommended_actions", []) or []):
            put_line(a, bullet="\u2192")
        put_line(f"Confidence: {getattr(ai, 'confidence', '')}", bullet="")
    if da:
        bg = "C6EFCE" if "0 critical" in da["headline"] and counts["attention"] == 0 else "FFE699"
        put_line(da["headline"], bullet="", bold=True, bg=bg)
        if da["findings"]:
            put_section("Findings")
            for f_ in da["findings"]:
                put_line(f_)
        if da["actions"]:
            put_section("Recommended Actions")
            for a in da["actions"]:
                put_line(a, bullet="\u2192")

    attention_rows = [n for n in narratives if n.status in ("ATTENTION", "FAILED")]
    if attention_rows:
        put_section("Checks needing attention")
        for n in attention_rows:
            sm.merge_cells(f"A{r}:B{r}")
            lc = sm.cell(row=r, column=1, value=f"{n.tcode} \u2014 {n.task}")
            lc.font = Font(name=FONT_NAME, size=10, bold=True)
            lc.fill = PatternFill("solid", fgColor="FFE699")
            lc.border = BORDER_ALL
            lc.alignment = Alignment(vertical="top", wrap_text=True)
            r += 1
            sm.merge_cells(f"A{r}:B{r}")
            vc = sm.cell(row=r, column=1, value=f"{n.observation}  \u2192  {n.recommendation}")
            vc.font = Font(name=FONT_NAME, size=10)
            vc.fill = PatternFill("solid", fgColor=WHITE)
            vc.border = BORDER_ALL
            vc.alignment = Alignment(vertical="top", wrap_text=True)
            lines = _est_lines(f"{n.observation} {n.recommendation}", 100)
            sm.row_dimensions[r].height = max(18, lines * LINE_HEIGHT + 4)
            r += 1

    sm.freeze_panes = "A4"
    sm.page_setup.orientation = "landscape"
    sm.page_setup.fitToWidth = 1
    sm.page_setup.fitToHeight = 0
    sm.sheet_properties.pageSetUpPr.fitToPage = True

    wb.save(output_path)
    log.info(f"MetroBrands Excel template filled: {output_path}")
    return output_path
