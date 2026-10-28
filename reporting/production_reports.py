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
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, KeepTogether
)
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

# ---------------------------------------------------------------------------
# PDF report
#
# Layout: cover summary (status, counts, key findings) -> check results ->
# analysis -> screen-evidence appendix, one screenshot per check, two per
# page, with a small caption instead of a bold "<TCode> — Screenshot N"
# heading. Status words and colours are the Excel sheet's own.
# ---------------------------------------------------------------------------
import re as _re
from datetime import datetime as _dt
from xml.sax.saxutils import escape as _esc

from reportlab.pdfgen import canvas as _rl_canvas

# ---- palette: soft pastels, dark slate text ------------------------------------
_NAVY = colors.HexColor("#2E4057")      # headings and brand text (dark slate)
_INK = colors.HexColor("#28323C")
_MUTED = colors.HexColor("#6B7785")
_RULE = colors.HexColor("#DDE3EA")
_ZEBRA = colors.HexColor("#F7F9FC")
_HEAD_BG = colors.HexColor("#DCE8F5")   # powder blue: table headers, title band
_GROUP_BG = colors.HexColor("#EEEAF7")  # lavender: area rows in the check table
_BAND_BG = colors.HexColor("#EAF2FB")

# status -> (text colour, pastel fill)
_STATUS_COLOURS = {
    "OK": ("#2E7D4F", "#DFF3E4"),
    "WARNING": ("#8A5A00", "#FFF3CD"),
    "CRITICAL": ("#A23B3B", "#FBE0E0"),
    "UNKNOWN": ("#5B6472", "#ECEEF4"),
    "FAILED": ("#A23B3B", "#FBE0E0"),
    "CONFIRM": ("#3B5B8C", "#E3ECF7"),
    "INCIDENT": ("#A23B3B", "#FBE0E0"),
}
_STATUS_LABEL = {"UNKNOWN": "NOT MEASURED", "CONFIRM": "CROSS-CHECK"}
_RANK = {"INCIDENT": -1, "CRITICAL": 0, "FAILED": 0, "WARNING": 1, "UNKNOWN": 2, "CONFIRM": 3, "OK": 4}

# Checks grouped by what they tell a Basis reader; anything else -> "Other checks".
_AREAS = (
    ("Users, servers & performance", ("AL08", "SM51", "SM66", "SM50", "SMLG", "ST03N", "ST06", "ST02")),
    ("Jobs, updates & locks", ("SM37", "SM37_CANCELLED", "SM13", "SM12")),
    ("Interfaces & mail", ("SM58", "SMQ1", "SMQ2", "SCOT", "SOST", "SMICM", "STMS")),
    ("Errors & logs", ("ST22", "SM21", "SP01")),
    ("Database", ("DB01", "DB02", "DB12", "ST04")),
    ("Security & tracing", ("STRUST", "ST05")),
)

# Generic sheet wording that adds nothing next to its own row.
_BOILERPLATE = ("Compare with the screenshot.",)


def _t(value) -> str:
    """Escape text for a reportlab Paragraph ("<ok>" in SAP text broke it)."""
    return _esc(str(value if value is not None else ""))


def _short(text, limit=150) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip(" ,;") + "…"


def _known_tcodes() -> set:
    """Every T-code with a capture task; a narrative for anything else (SM50, ST06) is kept as it is."""
    try:
        from core.config_loader import get_monitoring_tasks
        return {str(t.get("tcode") or "").strip().upper() for t in get_monitoring_tasks()}
    except Exception:  # noqa: BLE001
        return set()


def _metric_label(name: str) -> str:
    """'sap.sm12.oldest_lock_minutes' -> 'Oldest lock' (catalogue wording)."""
    try:
        from core.metric_catalogue import CATALOGUE
        label = (CATALOGUE.get(name) or {}).get("label")
        if label:
            return str(label)
    except Exception:  # noqa: BLE001
        pass
    try:
        from reporting.rfc_fallback import _LABELS
        if name in _LABELS:
            return _LABELS[name]
    except Exception:  # noqa: BLE001
        pass
    parts = str(name).split(".")
    if len(parts) >= 3 and parts[0] == "sap":
        return f"{parts[1].upper()} {' '.join(parts[2:]).replace('_', ' ')}"
    return str(name)


def _shot_part(tcode: str, path: str) -> str:
    """'ST04_large_tables_by_memory_142233_123456.png' -> 'large tables by memory'."""
    m = _re.match(rf"{_re.escape(tcode)}_(.+?)_\d{{6}}_\d+\.png$", Path(path).name, _re.I)
    return m.group(1).replace("_", " ") if m else ""


def _primary_screenshot(paths):
    """
    The one screen per check that shows the result. Selection screens and
    popup captures are skipped: SM37's "records passed" capture was taken
    with the popup open, but the capture grabs the main window, so it was
    the job list a second time.
    """
    existing = [p for p in (paths or []) if p and os.path.exists(p)]
    if not existing:
        return None
    preferred = [p for p in existing
                 if not _re.search(r"_(selection|records_passed)_", Path(p).name, _re.I)]
    return (preferred or existing)[-1]


def _styles():
    base = getSampleStyleSheet()["BodyText"]
    s = {}
    s["title"] = ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=18,
                                leading=22, textColor=_NAVY, spaceAfter=0)
    s["subtitle"] = ParagraphStyle("st", parent=base, fontSize=9.5, leading=12.5, textColor=_MUTED)
    s["h1"] = ParagraphStyle("h1", parent=base, fontName="Helvetica-Bold", fontSize=12.5,
                             leading=15, textColor=_NAVY, spaceBefore=10, spaceAfter=5)
    s["h2"] = ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=9.5,
                             leading=12, textColor=_NAVY, spaceBefore=9, spaceAfter=4)
    s["body"] = ParagraphStyle("b", parent=base, fontSize=8.8, leading=12, textColor=_INK)
    s["muted"] = ParagraphStyle("m", parent=s["body"], fontSize=7.6, leading=10, textColor=_MUTED)
    s["cell"] = ParagraphStyle("c", parent=base, fontSize=7.6, leading=9.6, textColor=_INK)
    s["cellb"] = ParagraphStyle("cb", parent=s["cell"], fontName="Helvetica-Bold")
    s["head"] = ParagraphStyle("hd", parent=s["cell"], fontName="Helvetica-Bold", textColor=_NAVY)
    s["group"] = ParagraphStyle("g", parent=s["cell"], fontName="Helvetica-Bold", textColor=_NAVY,
                                fontSize=7.8)
    s["label"] = ParagraphStyle("l", parent=s["cell"], fontSize=6.9, leading=8.6, textColor=_MUTED)
    s["value"] = ParagraphStyle("v", parent=s["cell"], fontName="Helvetica-Bold", fontSize=8.4,
                                leading=10.4, textColor=_INK)
    s["caption"] = ParagraphStyle("cap", parent=base, fontSize=7.4, leading=9.6,
                                  textColor=_MUTED, alignment=1)
    s["kpi"] = ParagraphStyle("k", parent=base, fontName="Helvetica-Bold", fontSize=17,
                              leading=20, alignment=1)
    s["kpil"] = ParagraphStyle("kl", parent=base, fontSize=7.2, leading=9, alignment=1,
                               textColor=_MUTED)
    s["item"] = ParagraphStyle("i", parent=s["body"], leftIndent=10, bulletIndent=0, spaceAfter=3)
    s["pill"] = ParagraphStyle("p", parent=s["cell"], fontSize=7.0, leading=8.6, alignment=1)
    return s


def _ai_unavailable(ai) -> bool:
    """The analyzer's placeholder when every AI provider failed (quota, bad keys)."""
    return (str(getattr(ai, "root_cause_category", "") or "").upper() == "AI_UNAVAILABLE"
            or str(getattr(ai, "likely_root_cause", "") or "").startswith(
                "The deterministic monitoring and correlation engines completed"))


def _code(tcode) -> str:
    """Display T-code: SM37_CANCELLED is a second SM37 check, not a T-code."""
    return str(tcode or "").split("_")[0] if str(tcode or "").startswith("SM37_") else str(tcode or "")


def _badge(status, styles):
    fg, _ = _STATUS_COLOURS.get(status, _STATUS_COLOURS["UNKNOWN"])
    label = _STATUS_LABEL.get(status, status)
    return Paragraph(f'<font color="{fg}"><b>{_t(label)}</b></font>', styles["pill"])


def _make_canvas(meta):
    class _ReportCanvas(_rl_canvas.Canvas):
        """Header band, footer and 'Page X of Y' on every page."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._decorate(total)
                super().showPage()
            super().save()

        def _decorate(self, total):
            width, height = A4
            left, right = 1.6 * cm, width - 1.6 * cm
            self.saveState()
            # thin pastel band across the top
            self.setFillColor(_HEAD_BG)
            self.rect(0, height - 0.32 * cm, width, 0.32 * cm, stroke=0, fill=1)
            self.setFont("Helvetica-Bold", 8.5)
            self.setFillColor(_NAVY)
            self.drawString(left, height - 1.15 * cm, "InfraBeatOps")
            self.setFont("Helvetica", 8.5)
            self.setFillColor(_MUTED)
            self.drawString(left + 2.05 * cm, height - 1.15 * cm, "SAP BASIS Monitoring Report")
            self.drawRightString(right, height - 1.15 * cm, meta["header_right"])
            self.setStrokeColor(_RULE)
            self.setLineWidth(0.6)
            self.line(left, height - 1.35 * cm, right, height - 1.35 * cm)
            self.line(left, 1.35 * cm, right, 1.35 * cm)
            self.setFont("Helvetica", 7.2)
            self.drawString(left, 0.95 * cm, meta["footer_left"])
            self.drawRightString(right, 0.95 * cm, f"Page {self._pageNumber} of {total}")
            self.restoreState()

    return _ReportCanvas


def _grid_style(extra=()):
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, _RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        *extra,
    ])


def _boxed(paragraph, width, fill, edge):
    box = Table([[paragraph]], colWidths=[width])
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(fill)),
                             ("LINEBEFORE", (0, 0), (0, 0), 3, colors.HexColor(edge)),
                             ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                             ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return box


def _note_for(n) -> str:
    """The row's short note: the observation unless it repeats the result or is boilerplate."""
    result_text = (n.result or n.value or "").strip()
    obs = (n.observation or "").strip()
    def norm(x):
        return _re.sub(r"[\s.;:,]+", " ", x).strip().lower()
    if not obs or norm(obs) == norm(result_text) or obs.startswith(_BOILERPLATE):
        return ""
    return _short(obs, 140)


def generate_system_pdf(result: MonitoringResult, gui_results=None, output_path=None):
    from reporting.check_narratives import narrate_all, deterministic_analysis
    from reporting.excel_template_writer import _system_config

    path = output_path or system_pdf_path(result.system, result.cycle_timestamp)
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    st = _styles()
    narratives = narrate_all(gui_results)
    # Checks without a SAP GUI capture this run: rows from the RFC reads of
    # the same run (reporting/rfc_fallback.py), so the report is never a
    # page of empty rows. Same order and wording as the Excel sheet.
    from reporting import rfc_fallback as _fb
    from reporting.check_narratives import CheckNarrative as _CN, TASKS as _TASKS
    from reporting.excel_template_writer import sheet_order as _sheet_order, _check_name
    _order = _sheet_order(result.system, result)          # this run's T-codes, the sheet's order
    narratives = [n for n in narratives if n.tcode in _order or n.tcode not in _known_tcodes()]
    _have = {n.tcode for n in narratives}
    _rfc_rows = 0           # rows added here (read over RFC or not captured at all)
    _rfc_read = 0           # ... of which have RFC figures
    for _tc in _order:
        if _tc in _have:
            continue
        _fr = _fb.row(result, _tc)
        if _fr is None:
            continue
        _actual, _status, _obs = _fr
        narratives.append(_CN(tcode=_tc, task=_TASKS.get(_tc) or _check_name(_tc), observation=_obs,
                              status=_status, value=_actual, result=_actual))
        _rfc_rows += 1
        _rfc_read += 0 if _actual.startswith("Not captured") else 1
    _skip = _fb.gui_skip_reason(result)
    da = deterministic_analysis(result, narratives, gui_results)
    counts = da["counts"]
    status = da["severity"]
    ai = result.ai_analysis

    config = _system_config(result.system)
    sid = str(config.get("sap_system_id") or "")
    environment = str(config.get("environment") or "")
    run_at = result.cycle_timestamp
    now = _dt.now()

    meta = {
        "header_right": f"{result.system} · Client {result.client} · {run_at:%d.%m.%Y %H:%M}",
        "footer_left": f"Generated {now:%d.%m.%Y %H:%M} by InfraBeatOps · Confidential",
    }
    width = A4[0] - 3.2 * cm

    # Section 1 groups the checks by area; the appendix follows the same order.
    area_of = {code: area for area, codes in _AREAS for code in codes}
    grouped: dict = {}
    for n in narratives:
        grouped.setdefault(area_of.get(n.tcode, "Other checks"), []).append(n)
    area_order = [a for a, _ in _AREAS] + ["Other checks"]
    in_order = [n for a in area_order for n in grouped.get(a, [])]

    # Figures first, so every row can point at the screen it was read from.
    figures = []
    fig_no = {}
    for n in in_order:
        if (n.facts or {}).get("reader"):
            # Every screen of these checks (ST04 overview and both large-table
            # lists, each STRUST PSE, STMS overview and connection test ...).
            shots = [p for p in n.screenshots if p and os.path.exists(p)]
        else:
            one = _primary_screenshot(n.screenshots)
            shots = [one] if one else []
        for shot in shots:
            figures.append((n, shot))
            fig_no.setdefault(id(n), len(figures))

    def fig_ref(n) -> str:
        return f"Fig. {fig_no[id(n)]}" if id(n) in fig_no else ""

    fig_by_code: dict = {}
    for n in in_order:
        if fig_ref(n):
            fig_by_code.setdefault(_code(n.tcode), fig_ref(n))

    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm,
                            topMargin=2.0 * cm, bottomMargin=1.9 * cm,
                            title=f"{result.system} SAP BASIS Monitoring Report",
                            author="InfraBeatOps")
    story = []

    # ==== page 1: executive summary =================================================
    title = Table([[Paragraph("SAP BASIS Monitoring Report", st["title"])],
                   [Paragraph(f"System <b>{_t(result.system)}</b> · Client {_t(result.client)} · "
                              f"run of {run_at:%d.%m.%Y} at {run_at:%H:%M:%S}", st["subtitle"])]],
                  colWidths=[width])
    title.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _BAND_BG),
                               ("LINEBEFORE", (0, 0), (0, -1), 4, _HEAD_BG),
                               ("LEFTPADDING", (0, 0), (-1, -1), 12),
                               ("TOPPADDING", (0, 0), (0, 0), 10), ("BOTTOMPADDING", (0, -1), (-1, -1), 10),
                               ("TOPPADDING", (0, 1), (-1, 1), 2), ("BOTTOMPADDING", (0, 0), (-1, 0), 2)]))
    story += [title, Spacer(1, 0.3 * cm)]

    captured_text = (f"{counts['captured']} of {counts['total']}" if not _rfc_rows else
                     f"{counts['total'] - _rfc_rows + _rfc_read} of {counts['total']}")
    evidence_text = ("1 screenshot per check" if not _rfc_rows else
                     f"{counts['total'] - _rfc_rows} screens, {_rfc_read} over RFC"
                     + (f", {_rfc_rows - _rfc_read} not captured" if _rfc_rows > _rfc_read else ""))
    facts_row = [
        ("System / SID", f"{result.system}" + (f" / {sid}" if sid else "")),
        ("Environment", environment or "—"),
        ("Run started", f"{run_at:%d.%m.%Y %H:%M}"),
        ("Checks read", captured_text),
        ("Evidence", evidence_text),
    ]
    cell_w = width / len(facts_row)
    info = Table([[Paragraph(_t(lbl), st["label"]) for lbl, _ in facts_row],
                  [Paragraph(_t(val), st["value"]) for _, val in facts_row]],
                 colWidths=[cell_w] * len(facts_row))
    info.setStyle(TableStyle([("LINEBELOW", (0, -1), (-1, -1), 0.6, _RULE),
                              ("LINEABOVE", (0, 0), (-1, 0), 0.6, _RULE),
                              ("LEFTPADDING", (0, 0), (-1, -1), 3),
                              ("TOPPADDING", (0, 0), (-1, 0), 5), ("BOTTOMPADDING", (0, -1), (-1, -1), 5),
                              ("TOPPADDING", (0, 1), (-1, 1), 1), ("BOTTOMPADDING", (0, 0), (-1, 0), 1)]))
    story += [info, Spacer(1, 0.35 * cm)]

    _desk = _fb.desktop_note(result)
    if _desk and not _skip:
        story += [_boxed(Paragraph(f"<b>Note on the screenshots.</b> {_t(_desk)}", st["body"]),
                         width, "#FFF6DA", "#E8C766"), Spacer(1, 0.3 * cm)]
    if _skip:
        story += [_boxed(Paragraph(f"<b>No SAP GUI screenshots this run.</b> {_t(_skip)} "
                                   "The figures below were read over RFC.", st["body"]),
                         width, "#FFF6DA", "#E8C766"), Spacer(1, 0.3 * cm)]

    fg, bg = _STATUS_COLOURS.get(status, _STATUS_COLOURS["UNKNOWN"])
    banner = Table([[
        Paragraph(f'<font color="{fg}" size="7.5">OVERALL STATUS</font><br/>'
                  f'<font color="{fg}" size="15"><b>{_t(_STATUS_LABEL.get(status, status))}</b></font>',
                  ParagraphStyle("bn", parent=st["body"], leading=17)),
        Paragraph(_t(da["headline"]), st["body"]),
    ]], colWidths=[4.4 * cm, width - 4.4 * cm])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg)),
        ("LINEBEFORE", (0, 0), (0, 0), 4, colors.HexColor(fg)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story += [banner, Spacer(1, 0.35 * cm)]

    tiles = [
        ("OK", counts["ok"], "Checks OK"),
        ("WARNING", counts["warning"], "Warnings"),
        ("CRITICAL", counts["critical"], "Critical"),
        ("UNKNOWN", counts["unknown"] + counts["failed"], "Not measured"),
        ("CONFIRM", counts["metric_findings"], f"RFC breaches ({counts['metric_critical']} critical)"),
    ]
    gap = 0.25 * cm
    tile_w = (width - gap * (len(tiles) - 1)) / len(tiles)
    row, widths, style = [], [], [("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                  ("TOPPADDING", (0, 0), (-1, -1), 7),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]
    for i, (key, number, label) in enumerate(tiles):
        tfg, tbg = _STATUS_COLOURS[key]
        row.append([Paragraph(f'<font color="{tfg}">{number}</font>', st["kpi"]),
                    Paragraph(_t(label), st["kpil"])])
        widths.append(tile_w)
        col = len(row) - 1
        style.append(("BACKGROUND", (col, 0), (col, 0), colors.HexColor(tbg)))
        if i < len(tiles) - 1:
            row.append("")
            widths.append(gap)
    kpi = Table([row], colWidths=widths)
    kpi.setStyle(TableStyle(style))
    story += [kpi]

    # ---- needs attention: one line per T-code, the screen and RFC readings together
    checked = {n.tcode for n in narratives}
    by_code: dict = {}
    for n in narratives:
        if n.status in ("CRITICAL", "FAILED", "WARNING"):
            e = by_code.setdefault(_code(n.tcode), {"status": n.status, "what": [], "rfc": [], "do": "",
                                                    "fig": fig_ref(n)})
            e["what"].append(f"<b>{_t(n.task)}</b>: {_t(_short(n.result or n.observation, 120))}")
            if _RANK[n.status] < _RANK[e["status"]]:
                e["status"] = n.status
            if n.recommendation and not e["do"]:
                e["do"] = n.recommendation
    for r in da["metric_rows"]:
        if r.get("source") == "screen" and r.get("tcode") in checked:
            continue  # the same reading as its check row
        code = _code(r.get("tcode") or "") or "RFC"
        e = by_code.setdefault(code, {"status": r["status"], "what": [], "rfc": [], "do": "",
                                      "fig": fig_by_code.get(code, "")})
        bit = f"{_t(_metric_label(r['name']))} {_t(r['value'])}"
        if r.get("detail"):
            bit += f" ({_t(_short(r['detail'], 70))})"
        e["rfc"].append(bit)
        if _RANK[r["status"]] < _RANK[e["status"]]:
            e["status"] = r["status"]
    attention = sorted(by_code.items(), key=lambda kv: (_RANK[kv[1]["status"]], kv[0]))

    story.append(Paragraph("Needs attention", st["h1"]))
    if not attention and not da.get("incidents"):
        story.append(Paragraph("Nothing: every check read within range and no RFC metric "
                               "breached a threshold.", st["body"]))
    else:
        head = [Paragraph(h, st["head"]) for h in ("Status", "T-code", "What was found", "What to do")]
        rows, extra = [head], []
        for text in da.get("incidents", []):
            rows.append([_badge("INCIDENT", st), Paragraph("—", st["cell"]),
                         Paragraph(_t(_short(text, 260)), st["cell"]), Paragraph("", st["cell"])])
            extra.append(("BACKGROUND", (0, len(rows) - 1), (0, len(rows) - 1),
                          colors.HexColor(_STATUS_COLOURS["INCIDENT"][1])))
        for code, e in attention[:10]:
            found = "<br/>".join(e["what"])
            if e["rfc"]:
                found += ("<br/>" if found else "") + "<font color='#5B6472'>RFC:</font> " + "; ".join(e["rfc"][:4])
            todo = _t(_short(e["do"], 130)) if e["do"] else ""
            if e["fig"]:
                todo += (" " if todo else "") + f"<font color='#6B7785'>({e['fig']})</font>"
            rows.append([_badge(e["status"], st), Paragraph(f"<b>{_t(code)}</b>", st["cell"]),
                         Paragraph(found, st["cell"]), Paragraph(todo, st["cell"])])
            extra.append(("BACKGROUND", (0, len(rows) - 1), (0, len(rows) - 1),
                          colors.HexColor(_STATUS_COLOURS[e["status"]][1])))
        t = Table(rows, colWidths=[2.1 * cm, 1.5 * cm, width - 9.3 * cm, 5.7 * cm], repeatRows=1)
        t.setStyle(_grid_style(extra))
        story.append(t)
        if len(attention) > 10:
            story.append(Spacer(1, 0.12 * cm))
            story.append(Paragraph(f"{len(attention) - 10} further item(s) in section 2.", st["muted"]))

    healthy = [n for n in narratives if n.status == "OK" and _code(n.tcode) not in by_code]
    unknown = [n for n in narratives if n.status in ("UNKNOWN",)]
    if healthy:
        story.append(Paragraph("Within range", st["h2"]))
        story.append(Paragraph(" · ".join(f"<b>{_t(_code(n.tcode))}</b> {_t(n.task)}" for n in healthy),
                               st["muted"]))
    if unknown:
        story.append(Paragraph("Not measured this run", st["h2"]))
        story.append(Paragraph(" · ".join(f"<b>{_t(_code(n.tcode))}</b> {_t(n.task)}"
                                          + (f" ({fig_ref(n)})" if fig_ref(n) else "") for n in unknown)
                               + " — compare with the screenshot in the appendix.", st["muted"]))
    if da["conflicts"]:
        story.append(Paragraph("Cross-checks to confirm", st["h2"]))
        for c in da["conflicts"]:
            story.append(Paragraph(_t(c), st["item"], bulletText="•"))

    # ==== 1. check results, grouped by area ===========================================
    story.append(PageBreak())
    story.append(Paragraph("1. Check results", st["h1"]))
    story.append(Paragraph(
        f"{counts['total'] - _rfc_rows} SAP GUI checks"
        + (f", {_rfc_read} read over RFC" if _rfc_rows else "")
        + (f", {_rfc_rows - _rfc_read} not captured" if _rfc_rows > _rfc_read else "")
        + f" for {_t(result.system)} client {_t(result.client)}. Status and figures match the Excel "
        "monitoring sheet row for row; the last column points to the screenshot each figure was read from.",
        st["muted"]))
    story.append(Spacer(1, 0.2 * cm))

    head = [Paragraph(h, st["head"]) for h in ("T-code", "Check", "Status", "Result", "Note", "Fig.")]
    rows, extra = [head], []
    for area in area_order:
        members = grouped.get(area) or []
        if not members:
            continue
        rows.append([Paragraph(_t(area), st["group"]), "", "", "", "", ""])
        r = len(rows) - 1
        extra += [("SPAN", (0, r), (-1, r)), ("BACKGROUND", (0, r), (-1, r), _GROUP_BG)]
        for k, n in enumerate(members):
            rows.append([Paragraph(f"<b>{_t(_code(n.tcode))}</b>", st["cell"]), Paragraph(_t(n.task), st["cell"]),
                         _badge(n.status, st), Paragraph(_t(n.result or n.value or ""), st["cell"]),
                         Paragraph(_t(_note_for(n)), st["cell"]),
                         Paragraph(_t(fig_ref(n).replace("Fig. ", "")), st["cell"])])
            r = len(rows) - 1
            _, sbg = _STATUS_COLOURS.get(n.status, _STATUS_COLOURS["UNKNOWN"])
            extra.append(("BACKGROUND", (2, r), (2, r), colors.HexColor(sbg)))
            if k % 2 == 1:
                extra.append(("BACKGROUND", (0, r), (1, r), _ZEBRA))
                extra.append(("BACKGROUND", (3, r), (-1, r), _ZEBRA))
    table = Table(rows, colWidths=[1.45 * cm, 2.9 * cm, 2.05 * cm, 5.4 * cm, width - 12.65 * cm, 0.85 * cm],
                  repeatRows=1)
    table.setStyle(_grid_style(extra))
    story.append(table)
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph("OK — within range · WARNING — review · CRITICAL — act now · "
                           "NOT MEASURED — the screen could not be read this run; compare with its "
                           "screenshot.", st["muted"]))

    # Figures read from the ST03N, SMICM, ST02, ST04, ST05, STMS and STRUST
    # screens, one table per check (sap_gui/tcode_actions_extra.py).
    details = [n for n in narratives if (n.facts or {}).get("report_rows")]
    if details:
        story.append(Paragraph("Figures read from the screens", st["h2"]))
        for n in details:
            drows = [[Paragraph(f"<b>{_t(_code(n.tcode))}</b> — {_t(n.task)}"
                                + (f" <font color='#6B7785'>({fig_ref(n)})</font>" if fig_ref(n) else ""),
                                st["cellb"]), _badge(n.status, st)]]
            body = [[Paragraph(_t(str(a)), st["cell"]), Paragraph(_t(str(b)), st["cell"])]
                    for a, b in (r for r in n.facts["report_rows"] if isinstance(r, (list, tuple)) and len(r) == 2)]
            dt = Table(drows + body, colWidths=[width * 0.38, width * 0.62], repeatRows=1)
            dt.setStyle(_grid_style())
            story += [Spacer(1, 0.15 * cm), dt]

    # ==== 2. analysis & actions =====================================================
    story.append(Paragraph("2. Analysis &amp; actions", st["h1"]))
    story.append(Paragraph(f"<b>Status: {_t(_STATUS_LABEL.get(status, status))}.</b> "
                           f"{_t(da['headline'])}", st["body"]))

    if da.get("incidents"):
        story.append(Paragraph("Correlated incidents", st["h2"]))
        for i in da["incidents"]:
            story.append(Paragraph(_t(i), st["item"], bulletText="•"))

    if da["metric_rows"]:
        story.append(Paragraph("Threshold breaches (RFC readings)", st["h2"]))
        mrows = [[Paragraph(h, st["head"]) for h in ("Severity", "Reading", "Value", "T-code", "Detail")]]
        mextra = []
        for i, r in enumerate(sorted(da["metric_rows"], key=lambda r: _RANK.get(r["status"], 9)), start=1):
            mrows.append([_badge(r["status"], st),
                          Paragraph(f"{_t(_metric_label(r['name']))}<br/>"
                                    f"<font size='6.4' color='#6B7785'>{_t(r['name'])}</font>", st["cell"]),
                          Paragraph(_t(r["value"]), st["cell"]),
                          Paragraph(_t(_code(r.get("tcode") or "")), st["cell"]),
                          Paragraph(_t(_short(r["detail"], 200)), st["cell"])])
            _, sbg = _STATUS_COLOURS[r["status"]]
            mextra.append(("BACKGROUND", (0, i), (0, i), colors.HexColor(sbg)))
        mt = Table(mrows, colWidths=[2.0 * cm, 4.6 * cm, 2.0 * cm, 1.4 * cm, width - 10.0 * cm],
                   repeatRows=1)
        mt.setStyle(_grid_style(mextra))
        story.append(mt)

    if ai and _ai_unavailable(ai):
        # No model ran. The placeholder's "severity" and its "retry" advice
        # were being printed as if a model had rated the cycle.
        story.append(Paragraph("AI root-cause assessment", st["h2"]))
        story.append(Paragraph("AI analysis was unavailable this run; the findings above are "
                               "rules-based.", st["muted"]))
    elif ai:
        story.append(Paragraph("AI root-cause assessment", st["h2"]))
        conf = getattr(ai, "confidence", "") or ""
        model_sev = str(getattr(ai, "severity", "") or "")
        note = f"Model confidence: {_t(conf)}." if conf else ""
        if model_sev and model_sev.upper() != status:
            note += (f" The model rated this cycle {_t(model_sev.upper())}; the report status "
                     f"above comes from the measured thresholds and checks.")
        if getattr(ai, "likely_root_cause", ""):
            story.append(_boxed(Paragraph(_t(ai.likely_root_cause), st["body"]), width, "#F3F0FA", "#B9AEDC"))
            story.append(Spacer(1, 0.1 * cm))
        for a in (ai.recommended_actions or []):
            story.append(Paragraph(_t(a), st["item"], bulletText="•"))
        if note:
            story.append(Paragraph(note.strip(), st["muted"]))

    if da["actions"]:
        story.append(Paragraph("Recommended actions", st["h2"]))
        for k, a in enumerate(da["actions"], start=1):
            code, _, text = str(a).partition(": ")
            line = (f"<b>{_t(_code(code))}</b> — {_t(text)}" if text else _t(a))
            story.append(Paragraph(line, st["item"], bulletText=f"{k}."))

    if not da["findings"]:
        story.append(Paragraph("No metric breached a threshold and every captured check read "
                               "normal.", st["body"]))

    # ==== appendix: screen evidence =================================================
    story.append(PageBreak())
    story.append(Paragraph("Appendix — Screen evidence", st["h1"]))
    if not figures:
        story.append(Paragraph("No readable SAP GUI screenshot files were available for "
                               "embedding." + (f" Reason: {_t(_skip)}" if _skip else ""), st["body"]))
    else:
        story.append(Paragraph("The screens the figures were read from, in the order of section 1. "
                               "Each caption repeats what the report read from that screen, so the two "
                               "can be compared at a glance. Full-resolution files are kept on the "
                               "collector under the Evidence ID.", st["muted"]))
        story.append(Spacer(1, 0.3 * cm))
        for index, (n, shot) in enumerate(figures, start=1):
            try:
                from PIL import Image as PILImage
                with PILImage.open(shot) as im:
                    w_px, h_px = im.size
                scale = min(width / max(w_px, 1), 10.4 * cm / max(h_px, 1))
                img = Image(shot, width=w_px * scale, height=h_px * scale)
            except Exception:
                img = Image(shot, width=width, height=width * 0.53)
            framed = Table([[img]], colWidths=[img.drawWidth + 2])
            framed.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.6, _HEAD_BG),
                ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
                ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]))
            captured = ""
            stamp = str(getattr(n, "captured_at", "") or "")
            if len(stamp) >= 19:
                captured = f" · captured {stamp[11:19]}"
            part = _shot_part(n.tcode, shot) if (n.facts or {}).get("reader") else ""
            sfg, _ = _STATUS_COLOURS.get(n.status, _STATUS_COLOURS["UNKNOWN"])
            caption = (f"Figure {index} · {_t(_code(n.tcode))} — {_t(n.task)}"
                       + (f" ({_t(part)})" if part else "") + f"{captured}"
                       + (f" · {_t(n.evidence_id)}" if n.evidence_id else ""))
            read = _short(n.result or n.value or "", 170)
            if read and fig_no.get(id(n)) == index:
                caption += (f"<br/>Read: <font color='#28323C'>{_t(read)}</font>"
                            f" · <font color='{sfg}'><b>{_t(_STATUS_LABEL.get(n.status, n.status))}</b></font>")
            story.append(KeepTogether([framed, Spacer(1, 0.12 * cm),
                                       Paragraph(caption, st["caption"]),
                                       Spacer(1, 0.5 * cm)]))

    doc.build(story, canvasmaker=_make_canvas(meta))
    return str(path)

def generate_system_reports(result: MonitoringResult, gui_results=None):
    return {
        "excel": generate_system_excel(result, gui_results),
        "pdf": generate_system_pdf(result, gui_results),
        "report_dir": str(system_report_dir(result.system, result.cycle_timestamp)),
        "evidence_root": str(system_evidence_root(result.system, result.cycle_timestamp)),
    }
