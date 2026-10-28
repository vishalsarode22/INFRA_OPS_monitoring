"""
One look for every IBOPS mail: a white card with a coloured band, a plain
headline, one or two sentences saying what happened in everyday words, and
then the detail. Names that matter (a user, a lock, a program, a server, a
job) are shown as coloured chips so they stand out on a phone.

Only inline styles and tables: Outlook (desktop and web), Gmail and phone
mail apps all render it the same.

Action buttons and the change list are added later by core/action_links
(footer_html) -- they go where page() leaves the END marker, inside the card.
"""
from __future__ import annotations

import html as _h
import re

END = "<!-- ibo-end -->"
FONT = "Segoe UI,Roboto,Helvetica,Arial,sans-serif"

ACCENT = {"CRITICAL": "#dc2626", "WARNING": "#d97706", "OK": "#16a34a", "NORMAL": "#16a34a",
          "INFO": "#0284c7", "RECOVERED": "#16a34a"}
# (background, text) for the chip of each kind of name
TONES = {
    "user": ("#dbeafe", "#1e3a8a"),
    "lock": ("#fee2e2", "#991b1b"),
    "table": ("#fee2e2", "#991b1b"),
    "program": ("#ede9fe", "#5b21b6"),
    "report": ("#ede9fe", "#5b21b6"),
    "transaction": ("#ede9fe", "#5b21b6"),
    "server": ("#e0f2fe", "#075985"),
    "workprocess": ("#e0f2fe", "#075985"),
    "job": ("#fef3c7", "#92400e"),
    "value": ("#f1f5f9", "#0f172a"),
    "tcode": ("#f1f5f9", "#334155"),
}
KIND_LABEL = {"user": "USER", "lock": "LOCK", "table": "TABLE", "program": "PROGRAM", "report": "PROGRAM",
              "transaction": "TRANSACTION", "server": "SERVER", "workprocess": "SERVER", "job": "JOB"}


def esc(s) -> str:
    return _h.escape(str(s if s is not None else ""))


def chip(text, tone: str = "value") -> str:
    """A highlighted name: CPIUSER, /SDF/SMON_CALL_KEY, sapxapcci_APC_00 ..."""
    bg, fg = TONES.get(tone, TONES["value"])
    return (f'<span style="background:{bg};color:{fg};padding:1px 6px;border-radius:4px;font-weight:700;'
            f'font-family:Consolas,Menlo,monospace;font-size:13px;white-space:nowrap">{esc(text)}</span>')


def pill(text, tone: str = "value") -> str:
    bg, fg = TONES.get(tone, TONES["value"])
    return (f'<span style="display:inline-block;background:{bg};color:{fg};padding:2px 8px;border-radius:10px;'
            f'font:700 11px {FONT};letter-spacing:.3px">{esc(text)}</span>')


def badge(level: str) -> str:
    col = ACCENT.get(str(level).upper(), "#0284c7")
    return (f'<span style="display:inline-block;background:{col};color:#fff;padding:3px 10px;border-radius:12px;'
            f'font:700 12px {FONT}">{esc(level)}</span>')


def highlight_names(text: str, names: dict[str, str]) -> str:
    """Escape `text` and put a chip on every name in `names` ({name: tone}) it contains."""
    out = esc(text)
    for name in sorted((n for n in names if n and len(n) >= 3), key=len, reverse=True):
        pat = re.escape(esc(name))
        out = re.sub(rf"(?<![\w/]){pat}(?![\w/])", lambda m, n=name: chip(n, names[n]), out, count=3)
    return out


def section(title: str, inner: str) -> str:
    return (f'<div style="margin:22px 0 0"><div style="font:700 15px {FONT};color:#0f172a;margin:0 0 8px">'
            f'{esc(title)}</div>{inner}</div>')


def callout(inner: str, level: str = "WARNING") -> str:
    col = ACCENT.get(str(level).upper(), "#0284c7")
    bg = {"#dc2626": "#fef2f2", "#d97706": "#fffbeb", "#16a34a": "#f0fdf4"}.get(col, "#f0f9ff")
    return (f'<div style="background:{bg};border-left:4px solid {col};border-radius:6px;padding:12px 14px;'
            f'font:15px/1.5 {FONT};color:#0f172a">{inner}</div>')


def facts(rows: list[tuple[str, str]], raw: bool = False) -> str:
    """A two-column table. raw=True: the values are HTML already."""
    body = "".join(
        f'<tr><td style="padding:5px 14px 5px 0;color:#64748b;vertical-align:top;white-space:nowrap">{esc(k)}</td>'
        f'<td style="padding:5px 0;color:#0f172a">{v if raw else esc(v)}</td></tr>' for k, v in rows if v not in (None, ""))
    return f'<table role="presentation" style="border-collapse:collapse;font:14px/1.45 {FONT}">{body}</table>'


def steps(items: list[str]) -> str:
    """Numbered steps; items are HTML."""
    lis = "".join(f'<li style="margin:0 0 8px">{x}</li>' for x in items)
    return f'<ol style="margin:0;padding:0 0 0 20px;font:14px/1.5 {FONT};color:#0f172a">{lis}</ol>'


def note(text_html: str) -> str:
    return f'<div style="margin:18px 0 0;font:12px/1.5 {FONT};color:#64748b">{text_html}</div>'


def page(level: str, eyebrow: str, title: str, lead_html: str, blocks: list[str] | None = None,
         foot_html: str = "") -> str:
    """The whole mail. `level` picks the colour; `lead_html` is the plain-language summary."""
    col = ACCENT.get(str(level).upper(), "#0284c7")
    return (
        f'<html><body style="margin:0;padding:0;background:#f1f5f9">'
        f'<table role="presentation" width="100%" style="border-collapse:collapse;background:#f1f5f9">'
        f'<tr><td align="center" style="padding:18px 10px">'
        f'<table role="presentation" width="100%" style="max-width:680px;border-collapse:collapse;background:#ffffff;'
        f'border-radius:10px;border-top:6px solid {col}">'
        f'<tr><td style="padding:20px 22px 22px">'
        f'<div style="font:600 12px {FONT};color:#64748b;margin:0 0 6px">{eyebrow}</div>'
        f'<div style="font:700 21px/1.3 {FONT};color:#0f172a;margin:0 0 12px">{esc(title)}</div>'
        f'<div style="font:15px/1.55 {FONT};color:#1e293b">{lead_html}</div>'
        + "".join(blocks or [])
        + (note(foot_html) if foot_html else "")
        + f'{END}</td></tr></table></td></tr></table></body></html>')


def insert(html: str, block: str) -> str:
    """Put `block` inside the card (at END), else before </body>, else at the end."""
    if not block:
        return html
    if END in (html or ""):
        return html.replace(END, block + END, 1)
    low = (html or "").lower()
    i = low.rfind("</body>")
    return (html[:i] + block + html[i:]) if i >= 0 else (html or "") + block


def ist(stamp=None) -> str:
    """'27 Oct 2026, 08:58 AM IST' -- always labelled, so a mailbox set to another zone is not confusing."""
    from datetime import datetime
    try:
        from utils.display_time import to_display
        s = to_display(stamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return re.sub(r"(\d\d:\d\d):\d\d", r"\1", s)
    except Exception:  # noqa: BLE001
        return str(stamp or datetime.now().strftime("%Y-%m-%d %H:%M"))


def duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} s" if s < 60 else "1 min"
    m = round(s / 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    d, h = divmod(h, 24)
    return f"{d} days {h} h" if h else f"{d} days"


def ms(v) -> str:
    """2948 -> '2.9 seconds', 296 -> '0.3 seconds' (people read seconds, not ms)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if x >= 60000:
        return duration(x / 1000)
    return f"{x / 1000:.1f} seconds"
