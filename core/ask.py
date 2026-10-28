"""
Ask the platform: "what happened on PS4 last night?"

The question is answered from what the platform recorded -- the timeline
(alerts, state changes, sweeps, acknowledgements, changes, actions), the
open problems, the knowledge base and the unusual-value flags -- never
from general knowledge.

With IBO_ENABLE_AI=1 the recorded facts and the question go to the
configured AI provider (with the same host / user masking as the RCA,
evaluation/ai_analyzer.get_provider) and it writes the answer, citing
times. With AI off the answer is the matching facts themselves, newest
first. Either way the facts used are returned so the page can show them.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from core import ops_db, timeline


def _window(question: str) -> tuple[datetime, datetime, str]:
    q = question.lower()
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "last night" in q or "tonight" in q:
        return today - timedelta(hours=6), min(now, today + timedelta(hours=9)), "last night (18:00-09:00)"
    if "yesterday" in q:
        return today - timedelta(days=1), today, "yesterday"
    if "today" in q:
        return today, now, "today"
    m = re.search(r"last (\d+) (hour|day|week)s?", q)
    if m:
        n = int(m.group(1))
        delta = {"hour": timedelta(hours=n), "day": timedelta(days=n), "week": timedelta(weeks=n)}[m.group(2)]
        return now - delta, now, m.group(0)
    if "this week" in q or "last week" in q:
        return now - timedelta(days=7), now, "the last 7 days"
    return now - timedelta(hours=48), now, "the last 48 hours"


def _systems_in(question: str, visible: list[str]) -> list[str]:
    q = question.lower()
    hits = [s for s in visible if s.lower() in q or s.lower().replace("_", " ") in q]
    return hits or list(visible)


_WORDS = re.compile(r"[a-z][a-z0-9_.]{2,}")
_STOP = {"what", "happened", "when", "why", "who", "the", "and", "was", "were", "did", "last", "night", "today",
         "yesterday", "system", "systems", "with", "for", "from", "there", "any", "all", "show", "tell", "about",
         "how", "many", "much", "this", "that", "week", "hours", "days", "is", "are", "has", "have"}


def gather(question: str, visible: list[str], limit: int = 250, platform: bool = True) -> dict:
    start, end, label = _window(question)
    systems = _systems_in(question, visible)
    events = timeline.query(None, since=ops_db.fmt(start), until=ops_db.fmt(end), limit=2000,
                            systems=systems + (["IBOPS"] if platform else []))
    words = {w for w in _WORDS.findall(question.lower()) if w not in _STOP}
    words -= {s.lower() for s in systems}
    if words:
        keyed = [e for e in events if any(w in (e["title"] + " " + e["detail"] + " " + e["category"]).lower()
                                          for w in words)]
        if keyed:
            events = keyed
    events = events[:limit]
    try:
        from core import standby
        from core.reachability import current_failures
        open_now = standby.only_active({k: v for k, v in current_failures().items() if k in systems})
    except Exception:
        open_now = {}
    kb = []
    try:
        from core import knowledge
        for s in systems[:5]:
            kb += knowledge.suggest(s, "", question, limit=2)
    except Exception:
        pass
    return {"window": label, "start": ops_db.fmt(start), "end": ops_db.fmt(end), "systems": systems,
            "events": events, "open_now": open_now, "kb": kb[:5]}


def _facts_text(ctx: dict) -> str:
    lines = [f"Period: {ctx['start']} to {ctx['end']} ({ctx['window']}). Systems: {', '.join(ctx['systems'])}."]
    if ctx["open_now"]:
        lines.append("Open now: " + "; ".join(f"{k}: [{v.get('state')}] since {v.get('since')}"
                                             for k, v in ctx["open_now"].items()))
    for e in reversed(ctx["events"]):
        lines.append(f"{e['at']} {e['system']} [{e['category']}] {e['title']}"
                     + (f" -- {e['detail'][:200]}" if e["detail"] else "") + (f" (by {e['actor']})" if e["actor"] else ""))
    for k in ctx["kb"]:
        lines.append(f"Recorded fix ({k['created_at'][:10]}, {k['system'] or 'any'}): {k['title']} -> {k['fix'][:200]}")
    return "\n".join(lines)


# Without AI: what mattered first (outages, changes, actions), the routine
# rest after it, and repeats folded into one line with a count.
_FIRST = ("state", "failover", "escalation", "action", "change", "correlation", "error", "check", "synthetic")


def _summary_lines(events: list[dict], limit: int = 15) -> list[str]:
    groups: dict[tuple, dict] = {}
    for e in events:                                     # newest first
        k = (e["system"], e["category"], e["title"])
        g = groups.get(k)
        if g is None:
            groups[k] = {"e": e, "n": 1, "first": e["at"]}
        else:
            g["n"] += 1
            g["first"] = e["at"]
    ordered = sorted(groups.values(), key=lambda g: g["e"]["at"], reverse=True)
    important = [g for g in ordered if g["e"]["category"] in _FIRST]
    rest = [g for g in ordered if g["e"]["category"] not in _FIRST]
    out = []

    def line(g):
        e = g["e"]
        when = e["at"][5:16] if g["n"] == 1 else f"{g['first'][5:16]}..{e['at'][11:16]}"
        return (f"• {when} {e['system']}: {e['title']}" + (f" (×{g['n']})" if g["n"] > 1 else "")
                + (f" (by {e['actor']})" if e["actor"] else ""))
    if important:
        out.append("What mattered, newest first:")
        out += [line(g) for g in important[:limit]]
    room = limit - min(len(important), limit)
    if rest and room > 0:
        out.append("Also recorded:")
        out += [line(g) for g in rest[:room]]
    hidden = len(important) + len(rest) - (min(len(important), limit) + min(len(rest), max(room, 0)))
    if hidden > 0:
        out.append(f"… and {hidden} more line(s) on the Timeline page.")
    return out


def answer(question: str, visible: list[str], platform: bool = True) -> dict:
    """platform=False for users limited to some customers: no IBOPS (platform) events."""
    question = str(question or "").strip()[:500]
    if not question:
        raise ValueError("Ask a question.")
    ctx = gather(question, visible, platform=platform)
    from evaluation.ai_analyzer import ai_enabled
    text, used_ai = None, False
    if ai_enabled() and ctx["events"]:
        try:
            from evaluation.ai_analyzer import get_provider
            prompt = ("You are the assistant of an SAP Basis monitoring platform. Answer the question using ONLY the "
                      "recorded facts below. Quote times. If the facts do not answer it, say so plainly. Plain "
                      "sentences, at most 12 lines, no markdown tables.\n\nQUESTION: " + question +
                      "\n\nRECORDED FACTS (oldest first):\n" + _facts_text(ctx)[:24000])
            text = str(get_provider().generate(prompt) or "").strip()
            used_ai = bool(text)
        except Exception as exc:  # noqa: BLE001
            text = None
            ctx["ai_error"] = f"{type(exc).__name__}: {exc}"[:200]
    if not text:
        if not ctx["events"] and not ctx["open_now"]:
            text = f"Nothing was recorded for {', '.join(ctx['systems'][:6])} in {ctx['window']}."
        else:
            parts = []
            if ctx["open_now"]:
                parts.append("Open now: " + "; ".join(f"{k} [{v.get('state')}] since {str(v.get('since'))[5:16]}"
                                                      for k, v in ctx["open_now"].items()) + ".")
            parts.append(f"{len(ctx['events'])} recorded event(s) in {ctx['window']}.")
            parts += _summary_lines(ctx["events"])
            text = "\n".join(parts)
    return {"answer": text, "ai": used_ai, "window": ctx["window"], "systems": ctx["systems"],
            "events": ctx["events"][:60], "open_now": ctx["open_now"], "kb": ctx["kb"],
            "ai_error": ctx.get("ai_error")}
