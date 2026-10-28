"""
Knowledge base from past fixes: "last time this was fixed by ...".

WHERE ENTRIES COME FROM
  * an incident resolved on the Incidents page with a summary
    (core/incident_actions RESOLVE) -- the summary, actions taken and
    confirmed root cause are saved;
  * a ticket closed with resolution notes (core/tickets.py, ServiceNow
    close notes, or the notes typed when closing on the Operations page);
  * "Add a fix" on the Operations page, for anything learned elsewhere.

HOW A NEW ALERT FINDS THEM
Each entry carries a signature: the distinctive words of the problem --
SAP identifiers such as runtime error names (TSV_TNEW_PAGE_ALLOC_FAILED),
job names (Z_DAILY_BILLING), programs, tables, plus the alert kind or rule.
A new alert's text is reduced the same way and compared (shared words over
all words, plus a bonus for the same rule and the same system). The best
matches above a threshold are shown in the alert mail, on the wall and in
the ticket: "Last time (12 Sep, Om): released lock of user BATCH01 in SM12;
root cause: job Z_STOCK_UPD aborted mid-update".

Nothing is invented: a suggestion is always a fix a person recorded.
"""
from __future__ import annotations

import html
import re

from core import ops_db, timeline
from utils.logger import get_logger

log = get_logger(__name__)

_STOP = {
    "the", "and", "for", "with", "from", "this", "that", "into", "than", "then", "were", "have",
    "been", "after", "before", "when", "will", "while", "there", "their", "which", "about", "again",
    "system", "alert", "sap", "check", "count", "value", "error", "errors", "today", "since",
    "last", "time", "sweep", "infrabeatops", "monitoring", "critical", "warning", "status", "metric",
}
_ID = re.compile(r"\b[A-Z][A-Z0-9]*(?:[_/][A-Z0-9]+)+\b|\b[ZY][A-Z0-9_]{3,}\b|\b[A-Z]{2,}[0-9]{2,}\b")
_WORD = re.compile(r"[a-z][a-z0-9_]{3,}")


def tokens(text: str) -> set[str]:
    text = str(text or "")
    ids = {m.group(0).upper() for m in _ID.finditer(text)}
    words = {w for w in _WORD.findall(text.lower()) if w not in _STOP}
    # Metric names (sap.st22.dumps) carry the T-code: keep it as a token.
    tcodes = {m.group(1).upper() for m in re.finditer(r"sap\.([a-z]{2}\d{2}[a-z]?)\.", text.lower())}
    return {t for t in ids | words | tcodes if len(t) <= 60}


def signature(*parts: str) -> str:
    return " ".join(sorted(tokens(" ".join(str(p or "") for p in parts))))[:2000]


def add(title: str, fix: str, rule: str = "", system: str = "", source: str = "manual",
        source_ref: str = "", by: str = "", actions=None, root_cause: str = "",
        context: str = "") -> dict:
    title, fix = str(title or "").strip(), str(fix or "").strip()
    if not title or not fix:
        raise ValueError("A knowledge-base entry needs a title (the problem) and the fix.")
    by = by or timeline.actor_name()
    sig = signature(title, context, root_cause, rule)
    kid = ops_db.execute(
        "INSERT INTO kb(signature, rule, system, title, fix, actions, root_cause, source, source_ref, "
        "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sig, str(rule or ""), str(system or ""), title[:300], fix[:4000],
         ops_db.dumps([str(a) for a in (actions or []) if str(a).strip()]), str(root_cause or "")[:1000],
         source, str(source_ref or "")[:120], by, ops_db.now()))
    if system:
        timeline.add(system, "kb", f"Fix saved to knowledge base: {title[:120]}", fix[:500], actor=by,
                     ref=f"kb:{kid}")
    return get(kid)


def get(kid: int) -> dict | None:
    row = ops_db.one("SELECT * FROM kb WHERE id=?", (int(kid),))
    if row:
        row["actions"] = ops_db.loads(row.get("actions"), [])
    return row


def retire(kid: int, by: str = "") -> None:
    ops_db.execute("UPDATE kb SET retired=1 WHERE id=?", (int(kid),))


def listing(q: str = "", limit: int = 200) -> list[dict]:
    if q:
        like = f"%{q}%"
        out = ops_db.rows("SELECT * FROM kb WHERE retired=0 AND (title LIKE ? OR fix LIKE ? OR signature LIKE ? "
                          "OR system LIKE ? OR rule LIKE ?) ORDER BY id DESC LIMIT ?",
                          (like, like, like.upper(), like, like, limit))
    else:
        out = ops_db.rows("SELECT * FROM kb WHERE retired=0 ORDER BY id DESC LIMIT ?", (limit,))
    for r in out:
        r["actions"] = ops_db.loads(r.get("actions"), [])
    return out


def suggest(system: str, rule: str, text: str = "", limit: int = 3, min_score: float = 0.18) -> list[dict]:
    """Best past fixes for a problem, highest score first."""
    try:
        query = tokens(f"{text} {rule}")
        rows = ops_db.rows("SELECT * FROM kb WHERE retired=0 ORDER BY id DESC LIMIT 2000")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"kb lookup failed: {exc}")
        return []
    scored = []
    for r in rows:
        sig = set(r["signature"].split()) if r["signature"] else set()
        overlap = len(sig & query)
        union = len(sig | query) or 1
        score = overlap / union
        same_rule = bool(rule) and r["rule"] == rule
        if same_rule:
            score += 0.3
        if system and r["system"] == system:
            score += 0.1
        # Same rule alone is not enough for a generic kind with no text --
        # but for alert kinds (system_down, credentials) the kind IS the problem.
        if score >= min_score and (overlap or same_rule):
            r = dict(r)
            r["score"] = round(score, 3)
            r["actions"] = ops_db.loads(r.get("actions"), [])
            scored.append(r)
    scored.sort(key=lambda x: (x["score"], x["id"]), reverse=True)
    return scored[:limit]


def mail_block(system: str, kind: str, text: str = "") -> str:
    hits = suggest(system, kind, text, limit=2)
    if not hits:
        return ""
    items = "".join(
        f"<li><b>{html.escape(h['created_at'][:10])}</b>, {html.escape(h['created_by'] or '?')}"
        f"{(' on ' + html.escape(h['system'])) if h['system'] and h['system'] != system else ''}: "
        f"{html.escape(h['fix'][:400])}"
        f"{('<br><i>Root cause: ' + html.escape(h['root_cause'][:200]) + '</i>') if h.get('root_cause') else ''}"
        f"</li>" for h in hits)
    return (f'<div style="margin-top:12px;padding:10px 12px;background:#f0f9ff;border-left:3px solid #0284c7;'
            f'font:13px Segoe UI,Arial,sans-serif;color:#0f172a"><b>Last time this was fixed</b>'
            f'<ul style="margin:6px 0 0 18px;padding:0">{items}</ul></div>')


def from_incident(system: str, incident) -> dict | None:
    """Save a resolved incident's recorded fix. Returns the entry or None."""
    res = getattr(incident, "resolution", None)
    if res is None:
        return None
    summary = str(getattr(res, "summary", "") or "").strip()
    actions = list(getattr(res, "actions_taken", []) or [])
    if not summary and not actions:
        return None
    context = " ".join([str(getattr(incident, "description", "") or ""),
                        " ".join(str(e) for e in (getattr(incident, "evidence", []) or [])[:20]),
                        " ".join(getattr(incident, "affected_metrics", []) or [])])
    return add(title=str(getattr(incident, "title", "") or incident.rule_id),
               fix=summary or "; ".join(actions), rule=str(getattr(incident, "rule_id", "") or ""),
               system=system, source="incident", source_ref=str(incident.incident_id),
               by=str(getattr(res, "resolver", "") or ""), actions=actions,
               root_cause=str(getattr(res, "confirmed_root_cause", "") or ""), context=context)
