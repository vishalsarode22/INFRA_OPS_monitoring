"""
Post-incident report: one click on the timeline -> a document with the
sequence of events, who did what, the changes before it, the fix, and the
screenshots from the last sweep.

    build(system, start, end, audience)  -> dict with every section
    to_html(report)                      -> print-ready HTML (browser: Save as PDF)
    to_pdf(report, path)                 -> PDF (reportlab, as reporting/pdf_builder.py)

Without start/end the most recent outage on the timeline is used (from the
first non-OK state line to "Reachable again"), or the last 24 hours.

audience="customer" leaves out internal detail: names of people, e-mail
addresses, recipients and the platform's own errors -- what a customer
should read. audience="internal" has everything.
"""
from __future__ import annotations

import html
import os
from datetime import datetime, timedelta

from core import ops_db, timeline

INTERNAL_ONLY = {"silence", "park", "runbook", "error", "config", "escalation"}


def last_outage(system: str, days: float = 30) -> tuple[datetime, datetime] | None:
    """(start, end) of the most recent outage on the timeline; end = now while it is still open."""
    rows = list(reversed(timeline.query(system, days, ["state", "vpn"], limit=2000)))
    start, last = None, None
    for r in rows:
        t = ops_db.parse(r["at"])
        if t is None:
            continue
        if r["title"].startswith("[") and start is None:
            start = t
        elif r["title"].startswith("Reachable again") and start is not None:
            last, start = (start, t), None
    if start is not None:
        return start, datetime.now()
    return last


def build(system: str, start: datetime | None = None, end: datetime | None = None,
          audience: str = "internal") -> dict:
    if start is None or end is None:
        found = last_outage(system)
        if found:
            start, end = found
        else:
            end = datetime.now()
            start = end - timedelta(hours=24)
    window_start = start - timedelta(minutes=30)
    events = list(reversed(timeline.query(system, since=ops_db.fmt(window_start), until=ops_db.fmt(end + timedelta(minutes=30)),
                                          limit=3000)))
    customer = audience == "customer"
    if customer:
        events = [e for e in events if e["category"] not in INTERNAL_ONLY]
    changes = list(reversed(timeline.query(system, since=ops_db.fmt(start - timedelta(hours=48)),
                                           until=ops_db.fmt(start), categories=["change"], limit=200)))
    acks = [e for e in events if e["category"] == "silence"]
    first_ack = next((e for e in acks if "Acknowledged" in e["title"] or "Ignored" in e["title"]), None)
    tickets = ops_db.rows("SELECT * FROM tickets WHERE system=? AND created_at>=? AND created_at<=?",
                          (system, ops_db.fmt(window_start), ops_db.fmt(end + timedelta(hours=1))))
    kb = ops_db.rows("SELECT * FROM kb WHERE system=? AND created_at>=? AND retired=0",
                     (system, ops_db.fmt(start)))
    duration = int((end - start).total_seconds() // 60)
    fix = "; ".join(k["fix"] for k in kb) or next((t["resolution"] for t in tickets if t.get("resolution")), "") or ""
    snap = {}
    try:
        from core.status_snapshot import load_snapshot
        snap = load_snapshot(system) or {}
    except Exception:
        pass
    shots = []
    try:
        from core.tickets import _screenshots
        shots = _screenshots(snap, limit=4)
    except Exception:
        pass
    crit = [m for m in (snap.get("metrics") or []) if str(m.get("status")) == "CRITICAL"]
    for e in events:
        if customer:
            e["actor"] = ""
            if e["category"] == "alert":
                e["detail"] = ""
    return {"system": system, "start": ops_db.fmt(start), "end": ops_db.fmt(end), "duration_minutes": duration,
            "audience": audience, "generated_at": ops_db.now(), "events": events, "changes": changes,
            "first_ack": None if customer or not first_ack else {"at": first_ack["at"], "by": first_ack["actor"]},
            "minutes_to_ack": (int((ops_db.parse(first_ack["at"]) - start).total_seconds() // 60)
                               if first_ack else None),
            "tickets": [{"id": t["external_id"] or t["id"], "status": t["status"], "url": t["url"]} for t in tickets],
            "fix": fix, "root_cause": "; ".join(k["root_cause"] for k in kb if k.get("root_cause")),
            "critical_metrics": [{"name": m.get("name"), "value": m.get("display_value"), "detail": m.get("detail")}
                                 for m in crit[:20]],
            "snapshot_at": snap.get("cycle_timestamp"), "screenshots": shots,
            "ai_summary": (snap.get("ai_analysis") or {}).get("root_cause") or ""}


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def to_html(r: dict) -> str:
    rows = "".join(f"<tr><td class='t'>{_e(e['at'][5:19])}</td><td>{_e(e['category'])}</td>"
                   f"<td><b>{_e(e['title'])}</b>{('<br><span class=d>' + _e(e['detail'])[:400] + '</span>') if e['detail'] else ''}"
                   f"{('<br><span class=d>by ' + _e(e['actor']) + '</span>') if e.get('actor') else ''}</td></tr>"
                   for e in r["events"])
    changes = "".join(f"<li>{_e(c['at'][5:16])} {_e(c['title'])}</li>" for c in r["changes"]) or "<li>none recorded</li>"
    crit = "".join(f"<li><b>{_e(m['name'])}</b>: {_e(m['value'])} {_e(m['detail'])}</li>"
                   for m in r["critical_metrics"]) or "<li>none in the last sweep</li>"
    ack = (f"{r['minutes_to_ack']} min after the start" + (f" by {_e(r['first_ack']['by'])}" if r.get("first_ack") else "")
           if r["minutes_to_ack"] is not None else "not acknowledged")
    tickets = ", ".join(_e(t["id"]) + f" ({_e(t['status'])})" for t in r["tickets"]) or "none"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Incident report {_e(r['system'])}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;color:#0f172a;max-width:900px;margin:24px auto;padding:0 16px;font-size:13px}}
h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:15px;margin:22px 0 8px;border-bottom:1px solid #e2e8f0;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%}}td{{border-top:1px solid #e2e8f0;padding:6px;vertical-align:top}}
td.t{{font-family:Consolas,monospace;white-space:nowrap;color:#475569}}.d{{color:#475569;font-size:12px}}
.kv td:first-child{{color:#475569;width:180px}}@media print{{body{{margin:0}}}}</style></head><body>
<h1>Incident report — {_e(r['system'])}</h1>
<div class="d">{_e(r['start'])} to {_e(r['end'])} · generated {_e(r['generated_at'])}{' · customer version' if r['audience'] == 'customer' else ''}</div>
<h2>Summary</h2><table class="kv">
<tr><td>Duration</td><td>{r['duration_minutes']} minutes</td></tr>
<tr><td>Acknowledged</td><td>{ack}</td></tr>
<tr><td>Tickets</td><td>{tickets}</td></tr>
<tr><td>Root cause</td><td>{_e(r['root_cause'] or r['ai_summary'] or 'not recorded yet')}</td></tr>
<tr><td>Fix</td><td>{_e(r['fix'] or 'not recorded yet -- resolve the incident or close the ticket with the fix')}</td></tr>
</table>
<h2>Changes in the 48 hours before</h2><ul>{changes}</ul>
<h2>Critical readings (last sweep {_e(r['snapshot_at'] or '-')})</h2><ul>{crit}</ul>
<h2>Sequence of events</h2><table>{rows or '<tr><td>no events recorded</td></tr>'}</table>
</body></html>"""


def to_pdf(r: dict, path: str) -> str:
    """Write the report as a PDF with reportlab (installed with the project). Returns the path."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    normal, h1, h2 = styles["Normal"], styles["Title"], styles["Heading2"]
    small = styles["BodyText"]
    story = [Paragraph(f"Incident report - {_e(r['system'])}", h1),
             Paragraph(f"{_e(r['start'])} to {_e(r['end'])} &middot; generated {_e(r['generated_at'])}", normal),
             Spacer(1, 0.4 * cm), Paragraph("Summary", h2)]
    ack = (f"{r['minutes_to_ack']} min after the start" if r["minutes_to_ack"] is not None else "not acknowledged")
    summary = [["Duration", f"{r['duration_minutes']} minutes"], ["Acknowledged", ack],
               ["Tickets", ", ".join(str(t["id"]) for t in r["tickets"]) or "none"],
               ["Root cause", Paragraph(_e(r["root_cause"] or r["ai_summary"] or "not recorded yet"), small)],
               ["Fix", Paragraph(_e(r["fix"] or "not recorded yet"), small)]]
    t = Table(summary, colWidths=[4 * cm, 13 * cm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey)]))
    story += [t, Paragraph("Changes in the 48 hours before", h2)]
    for c in r["changes"][:40] or [{"at": "", "title": "none recorded"}]:
        story.append(Paragraph(f"{_e(c['at'][5:16])} {_e(c['title'])}", small))
    story.append(Paragraph("Sequence of events", h2))
    data = [[_e(e["at"][5:19]), _e(e["category"]),
             Paragraph(f"<b>{_e(e['title'])[:300]}</b>" + (f"<br/>{_e(e['detail'])[:300]}" if e["detail"] else "")
                       + (f"<br/>by {_e(e['actor'])}" if e.get("actor") else ""), small)]
            for e in r["events"][:400]] or [["", "", "no events recorded"]]
    t = Table(data, colWidths=[3 * cm, 2.2 * cm, 11.8 * cm], repeatRows=0)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (1, -1), 8),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey)]))
    story.append(t)
    for shot in r.get("screenshots") or []:
        try:
            story += [Spacer(1, 0.3 * cm), Paragraph(_e(os.path.basename(shot)), small),
                      Image(shot, width=17 * cm, height=10 * cm, kind="proportional")]
        except Exception:
            continue
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    SimpleDocTemplate(path, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.5 * cm,
                      bottomMargin=1.5 * cm, title=f"Incident report {r['system']}").build(story)
    return path
