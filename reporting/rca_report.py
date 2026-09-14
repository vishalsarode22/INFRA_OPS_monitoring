"""
Performance RCA report (PDF).

Four parts, in the order a Basis consultant reads them at 2am:

    1. What is happening and who is causing it     -- one screen, no scrolling
    2. Root cause, actions now, actions later      -- the analysis
    3. Extracted metrics per screen                 -- the fields the script read
    4. The screens themselves                       -- evidence, each under its caption

The screenshots are the audit trail. A finding without its screen is an
assertion; the screen is what lets a second engineer disagree.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from utils.logger import get_logger

log = get_logger(__name__, "application")

_SEV_COLOUR = {"CRITICAL": "#C62828", "WARNING": "#EF6C00", "NORMAL": "#2E7D32"}


def _esc(s) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _facts_table(facts: dict, small) -> Table | None:
    """Render an action's extracted facts. Lists of dicts become tables; scalars a key/value list."""
    if not facts:
        return None
    rows = [["Field", "Value"]]
    for k, v in facts.items():
        if k in ("rca", "screens", "missing", "fields_set"):
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            # sub-table rendered as compact text lines
            lines = []
            for item in v[:10]:
                lines.append("; ".join(f"{kk}={vv}" for kk, vv in item.items() if vv not in ("", None, [])))
            val = "<br/>".join(_esc(x) for x in lines)
        elif isinstance(v, (dict, list)):
            val = _esc(json.dumps(v, default=str))[:600]
        else:
            val = _esc(v)
        rows.append([Paragraph(_esc(k), small), Paragraph(val, small)])
    if len(rows) == 1:
        return None
    t = Table(rows, colWidths=[4.2 * cm, 13.3 * cm], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474F")),
                           ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTSIZE", (0, 0), (-1, -1), 7),
                           ("GRID", (0, 0), (-1, -1), .3, colors.grey),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def _data_table(columns: list, rows: list, small) -> Table:
    """A readable table: header row, wrapped cells, widths shared from the page."""
    ncol = max(1, len(columns))
    width = 17.5 * cm
    # give text-heavy columns more room
    weights = [2.2 if any(k in str(c).lower() for k in ("program", "statement", "description", "argument", "action", "user action")) else 1.0 for c in columns]
    tot = sum(weights) or 1
    widths = [width * w / tot for w in weights]
    data = [[Paragraph(f"<b>{_esc(c)}</b>", small) for c in columns]]
    for r in rows:
        data.append([Paragraph(_esc(v)[:300], small) for v in (list(r) + [""] * ncol)[:ncol]])
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ECEFF1")),
                           ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#B0BEC5")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("FONTSIZE", (0, 0), (-1, -1), 7),
                           ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    return t


def build_rca_pdf(path: str, *, system: str, client: str, started: datetime,
                  trigger_reason: str, analysis: dict, evidence: list[dict], rfc: dict) -> str:
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=1.4 * cm, rightMargin=1.4 * cm,
                            topMargin=1.4 * cm, bottomMargin=1.4 * cm)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], textColor=colors.HexColor("#263238"))
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], textColor=colors.HexColor("#37474F"))
    body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("small", parent=body, fontSize=7, leading=9)
    sev = str(analysis.get("severity", "WARNING")).upper()
    sev_style = ParagraphStyle("sev", parent=body, fontSize=11,
                               textColor=colors.HexColor(_SEV_COLOUR.get(sev, "#37474F")))

    story = [
        Paragraph(f"Performance RCA — {_esc(system)}", h1),
        Paragraph(f"Client {_esc(client)} · Captured {started:%Y-%m-%d %H:%M:%S} · "
                  f"Trigger: {_esc(trigger_reason)}", body),
        Spacer(1, .3 * cm),
        Paragraph(f"<b>Severity: {sev}</b>", sev_style),
        Paragraph(f"<b>{_esc(analysis.get('headline', ''))}</b>", body),
        Spacer(1, .2 * cm),
    ]

    # ---- 1. Screens: for each T-code -- tables and readings ABOVE the screenshot, then the shot
    story += [Paragraph("1. Screen evidence", h2)]
    per_screen = analysis.get("per_screen") or {}
    focus_by_tcode = {e["tcode"]: (e.get("facts") or {}).get("focus") or [] for e in evidence}
    tables_by_tcode = {e["tcode"]: (e.get("facts") or {}).get("tables") or [] for e in evidence}
    printed = set()
    for e in evidence:
        tcode = e["tcode"]
        if tcode not in printed:
            printed.add(tcode)
            story.append(Paragraph(f"<b>{_esc(tcode)}</b>", h2))
            model_wrote_it = str(analysis.get("source", "rules")) != "rules"
            if per_screen.get(tcode) and model_wrote_it:
                story.append(Paragraph(_esc(per_screen[tcode]), body))
            for line in focus_by_tcode.get(tcode, []):
                story.append(Paragraph(f"• {_esc(line)}", body))
            for tbl in tables_by_tcode.get(tcode, []):
                story.append(Spacer(1, .12 * cm))
                story.append(Paragraph(f"<b>{_esc(tbl.get('title', ''))}</b>", small))
                story.append(_data_table(tbl.get("columns") or [], tbl.get("rows") or [], small))
            story.append(Spacer(1, .2 * cm))
        block = [Paragraph(f"{_esc(e['screen'])}" + (f" · {_esc(e['captured_at'])}" if e.get("captured_at") else ""), small)]
        p = e.get("path")
        if p and os.path.exists(p):
            try:
                from PIL import Image as PILImage
                with PILImage.open(p) as im:
                    w, h = im.size
                scale = min(17.5 * cm / max(w, 1), 15.5 * cm / max(h, 1))
                block.append(Image(p, width=w * scale, height=h * scale))
            except Exception as ex:  # noqa: BLE001
                block.append(Paragraph(f"(screenshot not embeddable: {_esc(type(ex).__name__)})", small))
        else:
            block.append(Paragraph("(screenshot file not found)", small))
        story.append(KeepTogether(block))
        story.append(Spacer(1, .3 * cm))

    # ---- 2. Summary (the model's when it ran; the readings otherwise)
    story += [PageBreak(), Paragraph("2. Summary", h2)]
    if rfc:
        by_inst = rfc.get("perf_response_by_instance") or {}
        resp = rfc.get("dialog_response_ms")
        line = (f"Live at capture: dialog response {resp:.0f} ms" if isinstance(resp, (int, float)) else "Live at capture:") + (
            " (" + ", ".join(f"{k} {v} ms" for k, v in by_inst.items()) + ")" if by_inst else "") + (
            f"; CPU {rfc['cpu']}%" if rfc.get("cpu") is not None else "") + (
            f"; memory {rfc['memory']}%" if rfc.get("memory") is not None else "")
        story.append(Paragraph(line, body))
    if analysis.get("summary") and str(analysis.get("source", "rules")) != "rules":
        story.append(Paragraph(_esc(analysis["summary"]), body))
    for tcode, para in (analysis.get("per_screen") or {}).items():
        if para:
            story.append(Paragraph(f"<b>{_esc(tcode)}</b> — {_esc(para)}", body))

    # ---- 3. Who / what
    story += [Spacer(1, .3 * cm), Paragraph("3. Who / what is causing it", h2)]
    culprits = analysis.get("culprits") or []
    if culprits:
        rows = [["Type", "Name", "Evidence", "Impact"]]
        for c in culprits:
            rows.append([Paragraph(_esc(c.get("type")), small), Paragraph(_esc(c.get("name")), small),
                         Paragraph(_esc(c.get("evidence")), small), Paragraph(_esc(c.get("impact")), small)])
        t = Table(rows, colWidths=[2.0 * cm, 4.0 * cm, 7.0 * cm, 4.5 * cm], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474F")),
                               ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("FONTSIZE", (0, 0), (-1, -1), 7),
                               ("GRID", (0, 0), (-1, -1), .3, colors.grey),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(t)
    else:
        story.append(Paragraph("No culprit could be tied to evidence in the captured screens.", body))

    # ---- 4. Root cause and actions
    story += [Spacer(1, .3 * cm), Paragraph("4. Root cause and actions", h2),
              Paragraph(f"<b>Root cause:</b> {_esc(analysis.get('root_cause', ''))}", body)]
    for title, key in (("Contributing factors", "contributing"), ("Actions now", "actions_now"),
                       ("Follow-up after the incident", "actions_later"), ("Considered and not supported by the evidence", "not_supported")):
        items = analysis.get(key) or []
        if items:
            story.append(Paragraph(f"<b>{title}</b>", body))
            for a in items:
                if isinstance(a, dict):
                    story.append(Paragraph(f"• {_esc(a.get('step'))} <i>({_esc(a.get('tcode', ''))})</i>", body))
                else:
                    story.append(Paragraph(f"• {_esc(a)}", body))
    story.append(Paragraph(
        f"<i>Confidence: {_esc(analysis.get('confidence', '?'))} — {_esc(analysis.get('confidence_reason', ''))}. "
        f"Source: {_esc(analysis.get('source', 'model'))}.</i>", body))

    doc.build(story)
    log.info(f"RCA PDF written: {path}")
    return path
