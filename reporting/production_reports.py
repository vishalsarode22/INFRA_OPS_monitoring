from pathlib import Path
import os
import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from core.models import MonitoringResult
from reporting.system_paths import system_report_dir, system_excel_path, system_pdf_path, system_evidence_root
from utils.paths import BASE_DIR
from utils.logger import get_logger

log = get_logger(__name__, "application")

def _text(v):
    return str(getattr(v, "value", v) if v is not None else "")

def _real_path(value):
    p = Path(value)
    return str(p if p.is_absolute() else Path(BASE_DIR) / p)

def _incidents(result):
    rows = []
    for item in result.incidents or []:
        rows.append(dict(item) if isinstance(item, dict) else getattr(item, "__dict__", {"incident": str(item)}))
    return rows

def generate_system_excel(result: MonitoringResult, gui_results=None, output_path=None):
    path = output_path or system_excel_path(result.system, result.cycle_timestamp)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "System Summary"
    summary = [
        ["InfraBeatOps Monitoring Report", ""],
        ["System", result.system],
        ["Client", result.client],
        ["Execution Time", result.cycle_timestamp.strftime("%Y-%m-%d %H:%M:%S")],
        ["Overall Status", _text(result.overall_status)],
        ["Metrics Evaluated", len(result.metrics)],
        ["Critical", len(result.critical_metrics())],
        ["Warning", len(result.warning_metrics())],
        ["Unknown", len(result.unknown_metrics())],
        ["T-Codes Executed", len(gui_results or [])],
        ["T-Codes Failed", sum(1 for m in (gui_results or []) if getattr(m, "display_value", "") == "failed")],
    ]
    for row in summary:
        ws.append(row)
    ws["A1"].font = Font(bold=True, size=16)
    ws.merge_cells("A1:B1")
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 60

    def sheet(name, headers, rows):
        sh = wb.create_sheet(name)
        sh.append(headers)
        for c in sh[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="37474F")
        for row in rows:
            sh.append(row)
        sh.freeze_panes = "A2"
        for col in range(1, len(headers) + 1):
            vals = [len(str(sh.cell(r, col).value or "")) for r in range(1, sh.max_row + 1)]
            sh.column_dimensions[get_column_letter(col)].width = min(60, max(14, max(vals, default=14) + 2))
        return sh

    sheet("Metrics",
        ["Timestamp","System","Client","Metric","Value","Warning","Critical","Status","TCode","Detail","Screenshot"],
        [[result.cycle_timestamp.strftime("%Y-%m-%d %H:%M:%S"), result.system, result.client,
          m.name, m.value if m.value is not None else m.display_value, m.threshold_warning,
          m.threshold_critical, _text(m.status), m.tcode or "", m.detail, m.screenshot_path or ""]
         for m in result.metrics])

    sheet("TCode Results",
        ["Evidence ID","TCode","Status","Display Value","Attempts","Recovery","Detail","Screenshots"],
        [[(getattr(m,"extra_data",{}) or {}).get("evidence_id",""), m.tcode or "",
          _text(m.status), m.display_value, (getattr(m,"extra_data",{}) or {}).get("attempts",0),
          "; ".join((getattr(m,"extra_data",{}) or {}).get("recovery_actions",[]) or []),
          m.detail, "; ".join(m.screenshot_paths or [])] for m in (gui_results or [])])

    sheet("Evidence Index",
        ["Evidence ID","System","Client","TCode","Attempts","Status","Recovery","Screenshots"],
        [[(getattr(m,"extra_data",{}) or {}).get("evidence_id",""),
          (getattr(m,"extra_data",{}) or {}).get("system",result.system),
          (getattr(m,"extra_data",{}) or {}).get("client",result.client),
          m.tcode or "", (getattr(m,"extra_data",{}) or {}).get("attempts",0), _text(m.status),
          "; ".join((getattr(m,"extra_data",{}) or {}).get("recovery_actions",[]) or []),
          "; ".join(m.screenshot_paths or [])] for m in (gui_results or [])])

    incidents = _incidents(result)
    sheet("Incidents",
        ["Incident ID","Severity","Title","Description","Root Cause","Recommended Action"],
        [[i.get("incident_id", i.get("id","")), i.get("severity", i.get("status","")),
          i.get("title", i.get("name","")), i.get("description", i.get("detail","")),
          i.get("root_cause", i.get("likely_root_cause","")),
          i.get("recommended_action", i.get("recommendation",""))] for i in incidents])

    recovery = []
    for m in gui_results or []:
        d = getattr(m, "extra_data", {}) or {}
        if d.get("attempts", 1) > 1 or d.get("recovery_actions"):
            recovery.append([d.get("evidence_id",""), m.tcode or "", d.get("attempts",0),
                             "; ".join(d.get("recovery_actions",[]) or []), m.display_value, m.detail])
    sheet("Recovery History", ["Evidence ID","TCode","Attempts","Recovery Actions","Final Result","Detail"], recovery)

    ai = result.ai_analysis
    sheet("AI Analysis", ["Severity","Likely Root Cause","Evidence","Recommended Actions","Confidence"],
          [[ai.severity, ai.likely_root_cause, "; ".join(ai.evidence or []),
            "; ".join(ai.recommended_actions or []), ai.confidence]] if ai else [])
    wb.save(path)
    return str(path)

def generate_system_pdf(result: MonitoringResult, gui_results=None, output_path=None):
    path = output_path or system_pdf_path(result.system, result.cycle_timestamp)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(path, pagesize=A4, rightMargin=1.5*cm, leftMargin=1.5*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = getSampleStyleSheet()
    h2 = styles["Heading2"]
    body = styles["BodyText"]
    story = [
        Paragraph("InfraBeatOps — SAP BASIS Monitoring Report", styles["Title"]),
        Paragraph(f"<b>System:</b> {result.system} &nbsp;&nbsp; <b>Client:</b> {result.client}<br/>"
                  f"<b>Execution:</b> {result.cycle_timestamp:%Y-%m-%d %H:%M:%S}", body),
        Spacer(1, .4*cm),
        Paragraph(f"<b>Overall Status: {_text(result.overall_status)}</b>", h2),
        Paragraph(f"Metrics: {len(result.metrics)} | Critical: {len(result.critical_metrics())} | "
                  f"Warnings: {len(result.warning_metrics())} | Unknown: {len(result.unknown_metrics())}", body),
    ]
    story += [Spacer(1,.3*cm), Paragraph("1. Monitoring Metrics", h2)]
    rows = [["Metric","Value","Status","TCode","Detail"]]
    rows += [[m.name,m.display_value,_text(m.status),m.tcode or "",m.detail[:90]] for m in result.metrics]
    t = Table(rows, colWidths=[4*cm,3*cm,2.2*cm,2*cm,6*cm], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#37474F")),
                           ("TEXTCOLOR",(0,0),(-1,0),colors.white),
                           ("FONTSIZE",(0,0),(-1,-1),7),
                           ("GRID",(0,0),(-1,-1),.3,colors.grey),
                           ("VALIGN",(0,0),(-1,-1),"TOP")]))
    # ---- 2. What each T-code capture found -------------------------------
    # Screenshots are no longer embedded (they made the PDF a 25-page image
    # dump that nobody reads). Each capture is instead described: what was
    # seen, its status, the headline count, and what to do about it. The
    # screenshots remain on disk under the evidence ID for anyone who wants
    # the raw page.
    from reporting.check_narratives import narrate_all, summary_counts, deterministic_analysis
    narratives = narrate_all(gui_results)
    counts = summary_counts(narratives)
    story += [t, Spacer(1,.4*cm), Paragraph("2. T-Code Checks — what was captured", h2)]
    story.append(Paragraph(
        f"{counts['captured']} of {counts['total']} checks captured · "
        f"<b>{counts['attention']}</b> need attention · {counts['failed']} failed. "
        f"Screenshots are retained on the collector under each Evidence ID.", body))
    small = ParagraphStyle("small", parent=body, fontSize=7, leading=9)
    crow = [["T-code", "Check", "Status", "Count / Value", "Observation", "Recommendation"]]
    for n in narratives:
        crow.append([Paragraph(n.tcode, small), Paragraph(n.task, small),
                     Paragraph(n.status, small), Paragraph(n.value or "", small),
                     Paragraph(n.observation, small), Paragraph(n.recommendation or "", small)])
    ct = Table(crow, colWidths=[1.6*cm, 2.6*cm, 1.9*cm, 2.2*cm, 5.4*cm, 4.3*cm], repeatRows=1)
    ct_style = [("BACKGROUND",(0,0),(-1,0),colors.HexColor("#37474F")),
                ("TEXTCOLOR",(0,0),(-1,0),colors.white),
                ("FONTSIZE",(0,0),(-1,-1),7),
                ("GRID",(0,0),(-1,-1),.3,colors.grey),
                ("VALIGN",(0,0),(-1,-1),"TOP")]
    for i, n in enumerate(narratives, start=1):
        colour = {"ATTENTION": "#FFE0B2", "FAILED": "#FFCDD2", "OK": "#E8F5E9"}.get(n.status)
        if colour:
            ct_style.append(("BACKGROUND", (2, i), (2, i), colors.HexColor(colour)))
    ct.setStyle(TableStyle(ct_style))
    story += [ct, Spacer(1,.4*cm)]

    # ---- 3. Analysis --------------------------------------------------------
    # The model narrative when available, always backed by the rules-based
    # analysis so the section is never "No AI analysis available" -- the
    # findings and actions below trace to metric statuses and captured facts.
    story.append(Paragraph("3. Analysis", h2))
    da = deterministic_analysis(result, narratives)
    ai = result.ai_analysis
    story.append(Paragraph(f"<b>Severity:</b> {(getattr(ai, 'severity', None) or da['severity'])}", body))
    story.append(Paragraph(f"<b>Summary:</b> {da['headline']}", body))
    if ai:
        story.append(Paragraph(f"<b>Likely root cause (model):</b> {ai.likely_root_cause}", body))
        for a in (ai.recommended_actions or []):
            story.append(Paragraph(f"• {a}", body))
        story.append(Paragraph(f"<b>Confidence:</b> {ai.confidence}", body))
    else:
        story.append(Paragraph("<i>Model narrative unavailable this cycle; the findings below are rules-based "
                               "and trace directly to metric thresholds and captured screens.</i>", body))
    if da["findings"]:
        story.append(Paragraph("<b>Findings</b>", body))
        for f_ in da["findings"]:
            story.append(Paragraph(f"• {f_}", body))
    if da["actions"]:
        story.append(Paragraph("<b>Recommended actions</b>", body))
        for a in da["actions"]:
            story.append(Paragraph(f"• {a}", body))
    if not da["findings"]:
        story.append(Paragraph("No metric breached a threshold and every captured check read normal.", body))
    doc.build(story)
    return str(path)

def generate_system_reports(result: MonitoringResult, gui_results=None):
    return {
        "excel": generate_system_excel(result, gui_results),
        "pdf": generate_system_pdf(result, gui_results),
        "report_dir": str(system_report_dir(result.system, result.cycle_timestamp)),
        "evidence_root": str(system_evidence_root(result.system, result.cycle_timestamp)),
    }
