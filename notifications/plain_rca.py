"""
The RCA in everyday words, for the mail.

The analysis (rules or AI) is written in Basis shorthand -- "STAD 30 min: 13
steps, total 1.2 s, worst 296 ms, RFC". This module turns it into sentences a
consultant can act on from a phone, with the names that matter (user, lock,
table, program, server) highlighted. Anything it does not recognise is shown
as written, so nothing is lost; the PDF keeps the full detail.
"""
from __future__ import annotations

import re

from notifications.mail_style import chip, esc, highlight_names, ms

_PROGRAM_MEANING = {
    "RFC": "calls from another system (RFC)",
    "SAPMSSY1": "calls from another system (RFC)",
    "SAPMHTTP": "web / Fiori requests",
    "SAPMSYST": "the logon screen",
    "RSBTCRTE": "a background job",
    "SAPMSSY0": "a dialog screen",
}


def _num(s) -> float:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _secs_text(t: str) -> str:
    """'1.2 s' / '296 ms' / '12 min 6 s' as written by the rules -> same, but '296 ms' -> '0.3 s'."""
    t = str(t or "").strip()
    m = re.fullmatch(r"([\d.,]+)\s*ms", t)
    if m:
        return ms(_num(m.group(1))).replace(" seconds", " s")
    return t


def _minutes_text(mins) -> str:
    m = int(_num(mins))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def program_text(p: str) -> str:
    """A program chip plus what it is, when we know."""
    p = str(p or "").strip()
    if not p:
        return ""
    base = p.split()[0].upper()
    meaning = _PROGRAM_MEANING.get(base)
    if not meaning and base[:1] in "ZY":
        meaning = "custom program"
    rest = p[len(p.split()[0]):].strip()
    out = chip(p.split()[0], "program") + (f" {esc(rest)}" if rest else "")
    return out + (f" <span style='color:#64748b'>({esc(meaning)})</span>" if meaning else "")


def _programs(text: str, limit: int = 3) -> str:
    items = [x.strip() for x in str(text or "").split(",") if x.strip()]
    shown = [program_text(x) for x in items[:limit]]
    more = f" and {len(items) - limit} more" if len(items) > limit else ""
    return ", ".join(shown) + more


# ------------------------------------------------------------------ trigger

def trigger(reason: str) -> tuple[str, str]:
    """(sentence as HTML, short words for the subject)."""
    r = str(reason or "").strip()
    m = re.search(r"critical limit (\d+) ms:\s*(.+)$", r)
    if m:
        limit = _num(m.group(1))
        pairs = re.findall(r"([^,\s]+)\s+(\d+)\s*ms", m.group(2))
        if pairs:
            worst = max(_num(v) for _, v in pairs)
            if len(pairs) == 1:
                n, v = pairs[0]
                text = (f"Users on {chip(n, 'server')} waited <b>{ms(v)}</b> on average for each screen over the "
                        f"last 5 minutes. The limit is {ms(limit)}.")
            else:
                text = ("Users waited too long for each screen over the last 5 minutes: "
                        + ", ".join(f"{chip(n, 'server')} <b>{ms(v)}</b>" for n, v in pairs[:4])
                        + f". The limit is {ms(limit)}.")
            return text, f"slow screens ({ms(worst).replace(' seconds', ' s')})"
    m = re.search(r"SMLG (\d+) ms(?: on (\S+))? at or over (\d+) ms", r)
    if m:
        where = f" on {chip(m.group(2), 'server')}" if m.group(2) else ""
        return (f"During the monitoring run the average response time{where} was {ms(m.group(1))} "
                f"(limit {ms(m.group(3))}).", f"slow response ({ms(m.group(1)).replace(' seconds', ' s')})")
    if r.lower().startswith("manual"):
        return "Someone started this analysis with the Performance monitoring button.", "checked by hand"
    return esc(r), ""


# ------------------------------------------------------------------ culprits

def culprit(c: dict) -> dict:
    """{kind, tone, who (HTML), what (HTML), why (text)} for one culprit."""
    t = str(c.get("type") or "").lower()
    name = str(c.get("name") or "")
    ev = str(c.get("evidence") or "")
    why = _impact(str(c.get("impact") or ""))
    kind, tone = {"user": ("USER", "user"), "lock": ("LOCK", "lock"), "report": ("PROGRAM", "program"),
                  "transaction": ("TRANSACTION", "program"), "workprocess": ("SERVER", "server"),
                  "job": ("JOB", "job")}.get(t, (t.upper() or "FOUND", "value"))
    who, what = chip(name, tone), esc(ev)

    if t == "user":
        um = re.match(r"(.+?) \((.+)\)$", name)
        who = chip(um.group(1), "user") + " running " + program_text(um.group(2)) if um else chip(name, "user")
        m = re.match(r"STAD (\S+) min: (\d+) steps, total (.+?), worst (.+?)(?:, (.*))?$", ev)
        if m:
            what = (f"Among the busiest users in the last {esc(m.group(1))} minutes: {m.group(2)} actions, "
                    f"{esc(_secs_text(m.group(3)))} in total; the slowest one took {esc(_secs_text(m.group(4)))}.")
            if m.group(5):
                what += f" Doing: {_programs(m.group(5))}."
        m = m or None
        m2 = re.match(r"ST03N user profile: total response (\S+) ms over (\S+) steps", ev)
        if m2:
            what = (f"One of the heaviest users in the ST03N workload: {ms(m2.group(1))} of response time "
                    f"over {esc(m2.group(2))} actions.")
        m3 = re.match(r"(SM50|SM66) long-running: WP (\S+) DIA (\S+)s on current step,?\s*(.*)$", ev)
        if m3:
            what = (f"Has been on one screen action for {esc(_minutes_text(_num(m3.group(3)) / 60))} "
                    f"(work process {esc(m3.group(2))}). That work process is blocked for everyone else.")
        m4 = re.match(r"(SM50|SM66): (\d+) active work processes \((.*)\)", ev)
        if m4:
            what = f"Is using {m4.group(2)} work processes at the same time ({_programs(m4.group(3))})."

    elif t == "lock":
        m = re.match(r"SM12: lock (\S+) min old, argument (.*)$", ev)
        nm = re.match(r"(\S+) on (\S+)$", name)
        if m and nm:
            who = chip(nm.group(1), "user") + " holding a lock on " + chip(nm.group(2), "table")
            what = (f"This lock has been held for <b>{esc(_minutes_text(m.group(1)))}</b>. Anyone who needs the "
                    f"same record has to wait until it is released. A lock this old usually means a failed "
                    f"update (SM13) or a session that hung.")
        m = re.match(r"SM12: same table\+argument held by (.*)$", ev)
        if m:
            tbl = name.split()[0] if name else ""
            users = [u.strip() for u in m.group(1).split(",") if u.strip()]
            who = chip(tbl, "table")
            what = ("Several users are waiting on the same locked record: "
                    + ", ".join(chip(u, "user") for u in users[:5]) + ".")
        m = re.match(r"SM12: (\d+) of (\S+) locks held by one user", ev)
        if m:
            who = chip(name, "user")
            what = (f"Holds {m.group(1)} of the {esc(m.group(2))} locks in the system -- usually a big batch "
                    f"job or a stuck session. Too many locks can fill the lock table.")

    elif t in ("report", "transaction"):
        who = program_text(name) if " " not in name.strip() else (
            chip(name.split()[0], "user") + " running " + _programs(name.split(None, 1)[1]))
        m = re.match(r"STAD: (\d+) steps, total (.+?), DB (.+?), CPU (.+)$", ev)
        if m:
            what = (f"The program that took the most time: {m.group(1)} actions, {esc(_secs_text(m.group(2)))} "
                    f"in total (database {esc(_secs_text(m.group(3)))}, processing {esc(_secs_text(m.group(4)))}).")
        m = re.match(r"STAD: peak memory (\S+) in one step", ev)
        if m:
            what = "Used the most memory in a single action in this window."
        m = re.match(r"ST03N transaction profile: total (\S+) ms, avg (\S+) ms, DB (\S+) ms", ev)
        if m:
            what = (f"One of the slowest transactions: {ms(m.group(2))} per action on average "
                    f"({ms(m.group(1))} in total, database {ms(m.group(3))}).")

    elif t == "workprocess":
        m = re.match(r"ST03N (.*?) on (\S+): avg dialog (\S+) ms over (\S+) steps; DB (\S+)%, CPU (\S+)%, "
                     r"wait (\S+) ms, roll (\S+) ms", ev)
        if m:
            who = chip(m.group(2), "server")
            win = f" between {esc(m.group(1).replace('-', ' and '))}" if m.group(1) else ""
            parts = [f"processing (CPU) {esc(m.group(6))}%", f"database {esc(m.group(5))}%"]
            if _num(m.group(8)) >= 50:
                parts.append(f"loading user memory (roll-in) {ms(m.group(8))}")
            if _num(m.group(7)) >= 50:
                parts.append(f"waiting for a free work process {ms(m.group(7))}")
            what = (f"Whole server{win}: screens took <b>{ms(m.group(3))}</b> on average over "
                    f"{esc(m.group(4))} actions. Time went to: " + ", ".join(parts) + ".")
        m = re.match(r"(SM50|SM66): PRIV mode, (.*?), elapsed (\S+)s", ev)
        if m:
            wm = re.match(r"WP (\S+)\s*(.*)$", name)
            who = (f"work process {chip(wm.group(1), 'server')}"
                   + (f" of {chip(wm.group(2), 'user')}" if wm and wm.group(2) else "")) if wm else chip(name, "server")
            what = (f"Stuck in private memory mode (PRIV) running {program_text(m.group(2))} for "
                    f"{esc(_minutes_text(_num(m.group(3)) / 60))}. Nobody else can use this work process "
                    f"until it finishes.")
    return {"kind": kind, "tone": tone, "who": who, "what": what, "why": why}


_IMPACTS = [
    (r"highest total response time", "One of the users putting the most load on the system right then."),
    (r"top program by response time", "This program took the most time in this window."),
    (r"largest memory allocation", "Big memory use can push others out of memory (roll-in, slow screens)."),
    (r"the instance is slow for every dialog user", "Every user on this server felt it."),
    (r"stale lock", "Others needing the same record are blocked."),
    (r"users serialised on one enqueue entry", "Users have to wait for each other, one at a time."),
    (r"holds a dialog work process exclusively", "Other users queue for a free work process."),
    (r"dialog work process held for minutes", "One work process fewer for everyone else."),
    (r"one user occupying a large share", "One user is taking a big share of the server."),
    (r"highest response-time consumer", "This user had the most response time in the workload."),
    (r"LOCK_TABLE_OVERFLOW", "If the lock table fills up, other users cannot save."),
    (r"custom code at the top", "Custom (Z) code at the top of the list -- a developer can look at it."),
]


def _impact(text: str) -> str:
    for pat, plain in _IMPACTS:
        if re.search(pat, text, re.I):
            return plain
    return text


# ------------------------------------------------------------------ actions

_ACTIONS = [
    (r"^(.+?): single step of (.+?) by (\S+), (CPU|database)-bound\.",
     lambda m, n: (f"Ask whoever owns {chip(m.group(3), 'user')} what {program_text(m.group(1))} was doing: one "
                   f"action took {esc(m.group(2))}, mostly "
                   + ("processing (CPU) -- a developer can profile it in" if m.group(4) == "CPU" else
                      "database time -- trace its SQL in")
                   + (" SAT." if m.group(4) == "CPU" else " ST05.")
                   + " Work this long belongs in a background job, not on a user's screen.")),
    (r"^Roll in/wait (\d+) ms",
     lambda m, n: (f"Screens waited {ms(m.group(1))} for user memory to be loaded back in (roll-in). The server "
                   f"is short of memory or very busy: check memory use in ST02 and the roll / extended memory "
                   f"settings.")),
    (r"^GUI time (\d+) ms is (\d+)%",
     lambda m, n: (f"{m.group(2)}% of the time ({ms(m.group(1))}) was spent getting screens to users' PCs. That "
                   f"is the network or the PCs, not the SAP server -- check with the users / network team if it "
                   f"stays high.")),
    (r"^(\d+) lock\(s\) older than 60 min",
     lambda m, n: (f"{m.group(1)} lock(s) have been held for more than an hour. First look in SM13 for failed "
                   f"updates; delete a lock in SM12 only when its owner's session is gone.")),
    (r"^(\d+) work process\(es\) in PRIV mode",
     lambda m, n: (f"{m.group(1)} work process(es) are stuck in private memory mode. Find the user and program in "
                   f"SM50 and decide with the user whether to cancel it.")),
    (r"^STAD wait time up to (\d+) ms",
     lambda m, n: (f"Users queued up to {ms(m.group(1))} for a free work process. Check in SM50 how many "
                   f"dialog work processes are free.")),
    (r"^STAD enqueue time up to (\d+) ms",
     lambda m, n: f"Users waited up to {ms(m.group(1))} for locks. Look for the lock holder in SM12."),
    (r"^Find the session holding the contended enqueue key",
     lambda m, n: ("Find who holds the locked record everyone is waiting for (SM12, owner column) and check "
                   "whether that session is still alive.")),
    (r"^DB time (\d+) ms is (\d+)%",
     lambda m, n: (f"{m.group(2)}% of the time ({ms(m.group(1))}) was database. Look for slow SQL in ST05 / "
                   f"SQLM and for missing indexes.")),
    (r"^CPU time (\d+) ms is (\d+)%",
     lambda m, n: (f"{m.group(2)}% of the time ({ms(m.group(1))}) was ABAP processing. Either heavy programs "
                   f"are running or the server is short of CPU (check OS07N / ST06).")),
    (r"^Wait time (\d+) ms",
     lambda m, n: (f"Users waited {ms(m.group(1))} for a free work process: all dialog processes were busy. "
                   f"Check SM50 / SM66 for long-running work.")),
    (r"^Enqueue time (\d+) ms",
     lambda m, n: f"Users waited {ms(m.group(1))} for locks. Look for the lock holder in SM12."),
]


def action(a, names: dict[str, str]) -> str:
    """One 'what to do now' line as HTML."""
    if isinstance(a, dict):
        step, tcode = str(a.get("step") or ""), str(a.get("tcode") or "")
    else:
        step, tcode = str(a or ""), ""
    for pat, fn in _ACTIONS:
        m = re.search(pat, step)
        if m:
            return fn(m, names)
    out = highlight_names(step, names)
    if tcode:
        out += " " + " ".join(chip(t.strip(), "tcode") for t in re.split(r"[/,]", tcode) if t.strip())
    return out


# ------------------------------------------------------------------ the rest

def names_of(culprits: list) -> dict[str, str]:
    """{name: tone} of everything worth highlighting in free text."""
    out: dict[str, str] = {}
    for c in culprits or []:
        t = str(c.get("type") or "").lower()
        tone = {"report": "program", "workprocess": "server"}.get(t, t or "value")
        name = str(c.get("name") or "")
        m = re.match(r"(\S+) on (\S+)$", name)
        if m:
            out[m.group(1)] = "user"
            out[m.group(2)] = "table"
            continue
        m = re.match(r"(.+?) \((.+)\)$", name)
        if m:
            out[m.group(1)] = "user"
            out[m.group(2)] = "program"
            continue
        if t == "workprocess":
            out[name.split()[0]] = "server"
        elif name:
            out[name] = tone
    return out


def confidence(analysis: dict) -> str:
    level = str(analysis.get("confidence") or "").lower()
    reason = str(analysis.get("confidence_reason") or "").lower()
    if "fail" in reason or "rules" in reason or analysis.get("source") == "rules":
        how = "The AI was not available, so IBOPS's built-in rules picked these causes from the SAP screens."
    else:
        how = "Written by the AI from the SAP screens IBOPS captured."
    word = {"high": "high", "medium": "medium", "low": "low"}.get(level, level or "not stated")
    return f"How sure: <b>{esc(word)}</b>. {how}"
