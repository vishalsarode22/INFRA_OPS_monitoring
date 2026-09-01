"""
Generates a professional PDF report from a MonitoringResult.
Sections: Executive Summary, System Info, Metrics (grouped), AI Analysis,
T-code Evidence (screenshots). Status color coding: NORMAL=green,
WARNING=orange, CRITICAL=red.
"""

import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
)

from core.models import MonitoringResult, Status
from utils.logger import get_logger
from utils.paths import BASE_DIR

log = get_logger(__name__, "application")

STATUS_COLORS = {
    Status.NORMAL: colors.HexColor("#2e7d32"),
    Status.WARNING: colors.HexColor("#e65100"),
    Status.CRITICAL: colors.HexColor("#c62828"),
    Status.UNKNOWN: colors.HexColor("#616161"),
}


def _reports_dir_for_today() -> str:
    base = os.path.join(BASE_DIR, "reports")
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(base, today)
    os.makedirs(path, exist_ok=True)
    return path


def _pdf_path_for_today() -> str:
    return os.path.join(_reports_dir_for_today(), "SAP_BASIS_Report.pdf")


def generate_pdf_report(result: MonitoringResult, gui_results: list = None, path: str = None) -> str:
    if path is None:
        path = _pdf_path_for_today()

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        topMargin=2 * cm, bottomMargin=2 * cm,
        leftMargin=2 * cm, rightMargin=2 * cm
    )
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle("TitleCustom", parent=styles["Title"], fontSize=18)
    heading_style = styles["Heading2"]
    normal_style = styles["Normal"]

    # --- Title ---
    story.append(Paragraph("SAP BASIS Monitoring Report", title_style))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        f"System: {result.system} &nbsp;&nbsp; Client: {result.client} &nbsp;&nbsp; "
        f"Generated: {result.cycle_timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
        normal_style
    ))
    story.append(Spacer(1, 0.5 * cm))

    # --- 1. Executive Summary ---
    story.append(Paragraph("1. Executive Summary", heading_style))
    status_color = STATUS_COLORS.get(result.overall_status, colors.black)
    summary_style = ParagraphStyle(
        "SummaryStatus", parent=normal_style, textColor=status_color,
        fontSize=14, spaceAfter=10
    )
    story.append(Paragraph(f"Overall Status: {result.overall_status.value}", summary_style))

    critical_count = len(result.critical_metrics())
    warning_count = len([m for m in result.metrics if m.status == Status.WARNING])
    story.append(Paragraph(
        f"{len(result.metrics)} metrics evaluated -- "
        f"{critical_count} critical, {warning_count} warning.",
        normal_style
    ))
    if result.errors:
        story.append(Paragraph(f"Collector errors encountered: {len(result.errors)}", normal_style))
    story.append(Spacer(1, 0.5 * cm))

    # --- 2. Metrics Table ---
    story.append(Paragraph("2. Monitoring Metrics", heading_style))
    table_data = [["Metric", "Value", "Status", "Detail"]]
    row_colors = []
    for m in result.metrics:
        table_data.append([m.name, m.display_value, m.status.value, m.detail[:60]])
        row_colors.append(STATUS_COLORS.get(m.status, colors.black))

    metrics_table = Table(table_data, colWidths=[4 * cm, 2.5 * cm, 2.5 * cm, 6 * cm], repeatRows=1)
    table_style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    for i, color in enumerate(row_colors, start=1):
        table_style_cmds.append(("TEXTCOLOR", (2, i), (2, i), color))
    metrics_table.setStyle(TableStyle(table_style_cmds))
    story.append(metrics_table)
    story.append(Spacer(1, 0.7 * cm))

    # --- 3. AI Analysis ---
    story.append(Paragraph("3. AI Analysis", heading_style))
    ai = result.ai_analysis
    if ai and ai.raw_response:
        story.append(Paragraph(f"<b>Severity:</b> {ai.severity}", normal_style))
        story.append(Paragraph(f"<b>Likely Root Cause:</b> {ai.likely_root_cause}", normal_style))
        story.append(Spacer(1, 0.2 * cm))

        if ai.evidence:
            story.append(Paragraph("<b>Evidence:</b>", normal_style))
            for ev in ai.evidence:
                story.append(Paragraph(f"&bull; {ev}", normal_style))
        story.append(Spacer(1, 0.2 * cm))

        if ai.recommended_actions:
            story.append(Paragraph("<b>Recommended Actions:</b>", normal_style))
            for idx, action in enumerate(ai.recommended_actions, start=1):
                story.append(Paragraph(f"{idx}. {action}", normal_style))
        story.append(Spacer(1, 0.2 * cm))

        story.append(Paragraph(f"<b>Confidence:</b> {ai.confidence}", normal_style))
    else:
        story.append(Paragraph("No AI analysis available for this cycle.", normal_style))

    story.append(Spacer(1, 0.5 * cm))

    # --- 4. T-code Evidence (screenshots) ---
    if gui_results:
        story.append(Paragraph("4. T-code Evidence", heading_style))
        for m in gui_results:
            if m.extra_data:
                data_str = ", ".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in m.extra_data.items())
                label = f"<b>{m.tcode}</b> -- {data_str}"
            else:
                label = f"<b>{m.tcode}</b>"
            story.append(Paragraph(label, normal_style))

            for shot_path in (m.screenshot_paths or []):
                if not shot_path or not os.path.exists(shot_path):
                    continue
                try:
                    img = Image(shot_path, width=15 * cm, height=9 * cm)
                    story.append(img)
                    story.append(Spacer(1, 0.3 * cm))
                except Exception as e:
                    log.warning(f"Could not embed screenshot {shot_path}: {e}")

            story.append(Spacer(1, 0.4 * cm))

    # --- Footer note ---
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(
        "This report was generated automatically by the SAP BASIS AI Monitoring Agent.",
        ParagraphStyle("Footer", parent=normal_style, fontSize=7, textColor=colors.grey)
    ))

    doc.build(story)
    log.info(f"PDF report generated: {path}")
    return path