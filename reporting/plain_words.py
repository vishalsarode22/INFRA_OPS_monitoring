"""
Plain wording for the monitoring sheet and report.

The figures come from the readers; this module only words them the way a
Basis consultant writes a daily check: short, specific, no repeated stock
phrases ("All within limits", "Number of Entries Displayed = ..."), no
"(CRITICAL)" tags inside sentences, and an observation only when there is
something to say.

Every rule falls back to the existing text (returns None) when the data it
needs is missing, so nothing is ever reported that was not read.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime


def _n(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else v        # 893.0 -> 893 ("893 ms", not "893.0 ms")
    t = str(v).strip()
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", t):
        return int(re.sub(r"[.,]", "", t))
    try:
        f = float(t.replace(",", ""))
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def _f(v) -> str:
    n = _n(v)
    if n is None:
        return str(v or "")
    return f"{n:,}" if isinstance(n, int) else f"{n:,.1f}"


def _pl(n, one, many=None) -> str:
    return f"{_f(n)} {one if _n(n) == 1 else (many or one + 's')}"


def _short_prog(p) -> str:
    return re.sub(r"=+CP$", "", str(p or "")).strip()


def _ago(stamp: str, finished: str = "") -> str:
    """'11.10.2026 05:44:21' -> '5 days ago' (relative to the capture time)."""
    try:
        when = datetime.strptime(str(stamp)[:19], "%d.%m.%Y %H:%M:%S")
    except ValueError:
        return ""
    try:
        now = datetime.strptime(str(finished)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        now = datetime.now()
    h = (now - when).total_seconds() / 3600
    if h < 0:
        return ""
    if h < 1:
        return "under an hour ago"
    if h < 36:
        return f"{int(round(h))} h ago"
    return f"{int(round(h / 24))} days ago"


# ---------------------------------------------------------------- result column

def actual(tcode: str, data: dict, status: str, text: str) -> str | None:
    t = tcode.upper()
    d = data or {}
    if d.get("extraction_failed") or str(text or "").startswith(("Not measured", "Not captured", "Not verified")):
        return None
    try:
        fn = _ACTUAL.get(t)
        return fn(d, text) if fn else None
    except Exception:  # noqa: BLE001 -- wording must never break the sheet
        return None


def _a_db01(d, text):
    locks, dead = _n(d.get("lock_count")), _n(d.get("deadlock_count"))
    if locks == 0 and dead in (0, None):
        return "No blocked transactions, no deadlocks"
    if locks is not None:
        return f"{_pl(locks, 'blocked transaction')}, {_pl(dead or 0, 'deadlock')}"
    return None


def _usage(item):
    try:
        from utils.sap_numbers import parse_usage_ratio
        raw = str((item or {}).get("raw") or "")
        p = parse_usage_ratio(raw) if raw else {}
        return p if p.get("usage_percent") is not None else (item or {})
    except Exception:  # noqa: BLE001
        return item or {}


def _a_db02(d, text):
    mem = _usage((d.get("memory") or {}).get("mdc"))
    ten = _usage((d.get("memory") or {}).get("tenant"))
    dat = _usage((d.get("storage") or {}).get("data"))
    log = _usage((d.get("storage") or {}).get("log"))
    if mem.get("usage_percent") is None:
        return None
    raw = str(((d.get("memory") or {}).get("mdc") or {}).get("raw") or "").replace(" /", " of ")
    parts = [f"Memory {mem['usage_percent']:.1f}%" + (f" ({raw.replace(',', '.')})" if raw else "")]
    if ten.get("usage_percent") is not None:
        parts.append(f"tenant {ten['usage_percent']:.1f}%")
    if dat.get("usage_percent") is not None:
        parts.append(f"data volume {dat['usage_percent']:.1f}%")
    if log.get("usage_percent") is not None:
        parts.append(f"log volume {log['usage_percent']:.1f}%")
    return ", ".join(parts)


def _a_db12(d, text):
    latest = d.get("latest_backup") or {}
    end = str(latest.get("end_time") or "").strip()
    if not end:
        return None
    ago = _ago(end, d.get("finished_at", ""))
    state = str(latest.get("status") or "successful").lower()
    head = "Last full data backup" if state in ("", "successful") else f"Last data backup ({state})"
    out = f"{head} finished {end[:16]}" + (f" ({ago})" if ago else "")
    running = str((d.get("running_backup") or {}).get("start_time") or "").strip()
    if running:
        out += f"; a backup is running since {running[:16]}"     # was dropped from the plain wording
    return out


def _a_scot(d, text):
    port = _n(d.get("mail_port"))
    if port is None:
        if _n(d.get("smtp_nodes_configured")) == 0:
            return "No SMTP node in SCOT"
        return None
    node = next((x.get("node") for x in d.get("smtp_nodes") or [] if x.get("node")), "SMTP")
    host = str(d.get("mail_host") or "").strip()
    return f"Node {node}: {host + ', ' if host else ''}port {port}"


def _a_sm12(d, text):
    c = _n(d.get("lock_count"))
    return None if c is None else ("No lock entries" if c == 0 else _pl(c, "lock entry", "lock entries"))


def _a_sm13(d, text):
    c = _n(d.get("update_count"))
    if c is None:
        return None
    if c == 0:
        return "No update requests"
    states = Counter(str(r.get("STATUS") or "").strip() for r in (d.get("update_records") or [])
                     if isinstance(r, dict))
    states.pop("", None)
    if states:
        return f"{_pl(c, 'update record')}: " + ", ".join(f"{n} {s}" for s, n in states.most_common(3))
    return None


def _a_sm21(d, text):
    logs, err, warn = _n(d.get("log_count")), _n(d.get("error_count")), _n(d.get("warning_count"))
    if logs is None:
        return None
    s = f"{_f(logs)} entries: {_pl(err or 0, 'error')}, {_pl(warn or 0, 'warning')}"
    echo = _n(d.get("dump_echo_count"))
    if echo:
        s += f" (+{_f(echo)} dump messages, see ST22)"
    return s


def _a_sm37(d, text):
    a = _n(d.get("active_job_count"))
    if a is None:
        return None
    if a == 0:
        return "No jobs running at the time of the check"
    s = f"{_pl(a, 'job')} running"
    lj = d.get("longest_running_job") or {}
    secs = _n(lj.get("duration_seconds"))
    if lj.get("job_name") and secs:
        s += f"; longest {lj['job_name']} ({_dur(secs)})"
    return s


def _dur(secs) -> str:
    secs = int(secs)
    if secs < 120:
        return f"{secs} s"
    m = secs // 60
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60} min"


def _cancelled_today(d):
    jobs = [j for j in d.get("cancelled_jobs") or d.get("jobs") or [] if isinstance(j, dict)]
    stamp = str(d.get("finished_at") or "")[:10]
    try:
        today = datetime.strptime(stamp, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return None, jobs
    return sum(1 for j in jobs if str(j.get("start_date") or "").strip() == today), jobs


def _a_sm37c(d, text):
    c = _n(d.get("cancelled_job_count"))
    if c is None:
        c = _n(d.get("records_passed"))
    if c is None:
        return None
    if c == 0:
        return "No cancelled jobs since yesterday"
    since = str((d.get("date_filter") or {}).get("from_date") or "").strip()
    today, jobs = _cancelled_today(d)
    s = f"{_f(c)} cancelled" + (f" since {since}" if since else "")
    if today is not None and jobs and len(jobs) >= c:          # the list is complete
        s += f" ({today} today)"
    return s


def _a_sm51(d, text):
    c = _n(d.get("instances_started") if d.get("instances_started") is not None else d.get("instance_count"))
    return None if c is None else f"{_pl(c, 'application server')} active"


def _a_sm58(d, text):
    failed = _n(d.get("failed_entries"))
    shown = _n(d.get("entries_displayed"))
    if failed is None and shown is None:
        return None
    if not failed and not shown:
        return "No tRFC entries waiting or failed"
    return f"{_f(shown or 0)} tRFC entries, {_f(failed or 0)} failed"


def _a_sm66(d, text):
    run, wait = _n(d.get("running_processes")), _n(d.get("waiting_processes"))
    if run is None:
        return None
    hold = _n(d.get("on_hold_processes"))
    # "On hold" processes are occupied too (waiting for RFC, locks, CPIC...):
    # leaving them out reported "6 busy" for 17 occupied work processes.
    if hold:
        active = _n(d.get("active_processes")) or (run + hold)
        s = f"{_f(active)} busy ({_f(run)} running, {_f(hold)} on hold)"
    else:
        s = f"{_f(run)} busy"
    s += f", {_f(wait)} free" if wait is not None else ", free processes not read"
    rows = [r for r in d.get("process_rows") or [] if isinstance(r, dict)]
    busy = [r for r in rows if "run" in " ".join(str(v).lower() for v in r.values())]
    if len(busy) == 1:
        r = busy[0]
        user = next((str(v) for k, v in r.items() if re.search(r"USER|UNAME", str(k).upper()) and v), "")
        prog = next((_short_prog(v) for k, v in r.items() if re.search(r"REPORT|PROGRAM", str(k).upper()) and v), "")
        if user or prog:
            s += f" (busy one: {user}{' / ' if user and prog else ''}{prog})"
    priv = _n(d.get("priv_mode_processes"))
    if priv:
        s += f"; {_f(priv)} in PRIV mode"
    return s


def _a_smlg(d, text):
    inst = [i for i in d.get("instances") or [] if isinstance(i, dict)]
    vals = []
    for i in inst:
        ms = _n(i.get("response_time_ms"))
        if ms is not None:
            vals.append((str(i.get("instance") or ""), ms))
    if not vals:
        return None
    vals.sort(key=lambda x: -x[1])
    return "; ".join(f"{n} {_f(ms)} ms" for n, ms in vals)


def _a_smq(d, text):
    e, q = _n(d.get("entries_displayed")), _n(d.get("queues_displayed"))
    if e is None:
        return None
    if e == 0:
        return "Empty"
    return f"{_f(e)} entries in {_pl(q or 0, 'queue')}"


def _a_sost(d, text):
    total, sent, err, wait = (_n(d.get(k)) for k in ("send_requests", "sent", "errors", "waiting"))
    if total is None:
        return None
    if total == 0:
        return "No send requests in the period"
    parts = [f"{_f(sent if sent is not None else 0)} sent"]
    if wait is not None:
        parts.append(f"{_f(wait)} waiting")
    elif sent is not None and err is not None:
        parts.append(f"{_f(max(total - sent - err, 0))} waiting")
    if err is not None:
        parts.append(f"{_f(err)} in error")
    return f"{_f(total)} requests: " + ", ".join(parts)


def _a_sp01(d, text):
    c = _n(d.get("spool_requests"))
    if c is None:
        return None
    err = _n(d.get("spool_errors")) or 0
    return f"{_pl(c, 'spool request')} today, {('none' if not err else _f(err))} in error"


def _a_st22(d, text):
    c = _n(d.get("dump_count"))
    if c is None:
        return None
    return "No dumps today" if c == 0 else f"{_pl(c, 'dump')} today"


_ACTUAL = {"DB01": _a_db01, "DB02": _a_db02, "DB12": _a_db12, "SCOT": _a_scot, "SM12": _a_sm12,
           "SM13": _a_sm13, "SM21": _a_sm21, "SM37": _a_sm37, "SM37_CANCELLED": _a_sm37c,
           "SM51": _a_sm51, "SM58": _a_sm58, "SM66": _a_sm66, "SMLG": _a_smlg, "SMQ1": _a_smq,
           "SMQ2": _a_smq, "SOST": _a_sost, "SP01": _a_sp01, "ST22": _a_st22}


# ---------------------------------------------------------------- observation column

def note(tcode: str, data: dict, status: str, text: str) -> str | None:
    t = tcode.upper()
    d = data or {}
    if d.get("extraction_failed") or status in ("UNKNOWN", "NOT AUTHORIZED", "NOT APPLICABLE"):
        return None
    try:
        fn = _NOTE.get(t)
        out = fn(d, status, text) if fn else None
    except Exception:  # noqa: BLE001
        out = None
    if out is None:
        out = _tidy(text)
    if out == "" and status in ("WARNING", "CRITICAL"):
        # A WARNING/CRITICAL row always says why: the plain rule had nothing
        # to add, so keep the original explanation (SCOT came out empty).
        out = _tidy(text)
    return out


def _tidy(text) -> str | None:
    """Generic clean-up of the old wording."""
    s = str(text or "").strip()
    if not s:
        return None
    if s in ("All within limits", "All within limits."):
        return ""
    s = re.sub(r"\s*\((?:OK|WARNING|CRITICAL|NORMAL)\)", "", s)
    s = s.replace(" -- ", " - ").replace("—", "-")
    return s


def _o_db01(d, status, text):
    return "" if status == "OK" else None


def _o_db02(d, status, text):
    bits = []
    state = str(d.get("operational_state") or "").strip()
    if state and "all services" not in state.lower():
        bits.append(state.rstrip(".") + ".")
    mem = _usage((d.get("memory") or {}).get("mdc")).get("usage_percent")
    if mem is not None and status in ("WARNING", "CRITICAL"):
        bits.append(f"HANA memory is at {mem:.1f}% of the allocation limit - check the top consumers (ST04).")
    counts = []
    for a in d.get("alerts") or []:
        m = re.match(r"\s*(\d+)\s+warnings?\s+with\s+(\w+)", str(a), re.I)
        if m and int(m.group(1)):
            counts.append(f"{m.group(1)} {m.group(2).lower()}")
    if counts:
        bits.append("Open HANA alerts: " + ", ".join(counts) + ".")
    return " ".join(bits) if bits else ("All services up." if status == "OK" else None)


def _o_db12(d, status, text):
    if status == "WARNING":
        return "No full backup in the last 26 h - check the backup schedule (DB13 / backint log)."
    return ""


def _o_scot(d, status, text):
    if d.get("values_marked"):
        return ("SCOT shows the node's host and port with '#'. With nothing sent in SOST, "
                "check the node is active (Node in use) and the send job runs.")
    if _n(d.get("smtp_nodes_configured")) == 0:
        return "No SMTP node - outbound mail cannot leave the system. Set one up in SCOT."
    return ""


def _o_sm12(d, status, text):
    return "" if status == "OK" else None


def _o_sm13(d, status, text):
    if status == "OK":
        return "Nothing stuck or failed."
    return None


def _o_sm21(d, status, text):
    errs = [e for e in d.get("error_entries") or [] if isinstance(e, dict)]
    ids = [(i, n) for i, n in (d.get("message_ids") or []) if i]
    if not errs and not ids:
        return None if status != "OK" else ""
    lines = []
    texts = Counter(_clip(re.sub(r"\s+", " ", str(e.get("text") or "")).replace(" )", ")"), 70)
                    for e in errs if e.get("text"))
    for t, _ in texts.most_common(2):
        lines.append(f"'{t}'")
    s = "Mostly " + " and ".join(lines) if lines else ""
    if s:
        first = errs[0] if errs else {}
        when = str(first.get("time") or "").strip()
        user = str(first.get("user") or "").strip()
        msgid = str(first.get("id") or "").strip()
        if len(errs) == 1 and (when or user):
            # One entry: say exactly when and who, as the original wording did.
            s = (f"1 error-level entry" + (f" {msgid}" if msgid else "") + (f" at {when}" if when else "") + (f" (user {user})" if user else "")
                 + f": {lines[0]}")
        s += ". Check SM21 around " + ("that time." if len(errs) == 1 else "those times.")
    return s or None


def _clip(t: str, n: int) -> str:
    """Cut at a word boundary, never mid-word."""
    t = t.strip()
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + " ..."


def _o_sm37(d, status, text):
    return ""


def _o_sm37c(d, status, text):
    jobs = [j for j in d.get("cancelled_jobs") or d.get("jobs") or [] if isinstance(j, dict)]
    if not jobs:
        return None
    top = Counter((str(j.get("job_name") or "").strip(), str(j.get("created_by") or "").strip())
                  for j in jobs).most_common(2)
    (name, user), n = top[0]
    s = f"Mostly {name}" + (f" ({user})" if user else "") + f", {n}x"
    if len(top) > 1:
        (n2, u2), c2 = top[1]
        s += f"; also {n2}" + (f" ({u2})" if u2 else "") + f", {c2}x"
    return s + ". Check the job logs and ST22 at the cancel times."


def _o_sm51(d, status, text):
    return ""


def _o_sm58(d, status, text):
    return "" if status == "OK" else None


def _o_sm66(d, status, text):
    failed = _n(d.get("failed_processes"))
    if failed:
        return (f"{_pl(failed, 'work process', 'work processes')} "
                f"{'shows an earlier failure' if failed == 1 else 'show earlier failures'} in the "
                "Failures column; check its dev trace (SM50) if the count keeps rising.")
    priv = _n(d.get("priv_mode_processes"))
    if priv:
        return "Work process in PRIV mode - check the user's memory use."
    return ""


def _o_smlg(d, status, text):
    return "" if status == "OK" else None


def _o_smq1(d, status, text):
    top = [q for q in d.get("top_queues") or [] if isinstance(q, (list, tuple)) and len(q) == 3]
    e = _n(d.get("entries_displayed"))
    if not e:
        return ""
    if top:
        s = "Largest: " + ", ".join(f"{q[0]} ({_f(q[2])})" for q in top[:3])
        if any(str(q[0]).upper().startswith("MCEX") for q in top[:3]):
            s += ". MCEX queues are LO extraction queues; they clear when the V3 collective job runs."
        return s
    return None


def _o_smq2(d, status, text):
    return "" if not _n(d.get("entries_displayed")) else None


def _o_sost(d, status, text):
    total, sent = _n(d.get("send_requests")), _n(d.get("sent"))
    if total and sent == 0:
        return "Nothing went out - check the SMTP node in SCOT and the send job RSCONN01."
    err = _n(d.get("errors"))
    if err:
        return f"{_pl(err, 'request')} in error - open them in SOST for the reason."
    return ""


def _o_sp01(d, status, text):
    return "" if status == "OK" else None


def _o_st22(d, status, text):
    dumps = [x for x in d.get("dumps") or [] if isinstance(x, dict)]
    if not dumps:
        return "" if status == "OK" else None
    c = Counter((str(x.get("runtime_error") or "").strip(), _short_prog(x.get("program")),
                 str(x.get("user") or "").strip()) for x in dumps)
    parts = []
    for (err, prog, user), n in c.most_common(3):
        parts.append(f"{n}x {err}" + (f" in {prog}" if prog else "") + (f" ({user})" if user else ""))
    return "; ".join(parts) + "."


_NOTE = {"DB01": _o_db01, "DB02": _o_db02, "DB12": _o_db12, "SCOT": _o_scot, "SM12": _o_sm12,
         "SM13": _o_sm13, "SM21": _o_sm21, "SM37": _o_sm37, "SM37_CANCELLED": _o_sm37c,
         "SM51": _o_sm51, "SM58": _o_sm58, "SM66": _o_sm66, "SMLG": _o_smlg, "SMQ1": _o_smq1,
         "SMQ2": _o_smq2, "SOST": _o_sost, "SP01": _o_sp01, "ST22": _o_st22}


# ---------------------------------------------------------------- reader checks (ST03N, ST02, ST04 ...)

def reader(tcode: str, data: dict, status: str, actual_text: str, obs: str) -> tuple[str, str]:
    """Shorter result and plainer note for the screen-reader checks."""
    t = tcode.upper()
    a, o = str(actual_text or ""), str(obs or "")
    try:
        if t == "ST03N":
            m = re.findall(r"(DIALOG|BACKGROUND|WS-RFC|RFC)\s+([\d,.]+)\s+steps.*?avg\s+([\d,.]+)\s*ms", a)
            if m:
                a = "; ".join(f"{k.capitalize() if k != 'WS-RFC' else 'WS-RFC'} {s} steps, avg {_ms(v)}"
                              for k, s, v in m)
            if o.startswith("All within limits"):
                o = ""
        elif t == "SMICM":
            m = re.findall(r"(HTTPS?|SMTP|P4|IIOP)\s+(\d+)\s+(active|inactive)", a, re.I)
            if m:
                a = ", ".join(f"{p} {port}" + ("" if s.lower() == "active" else " (inactive)") for p, port, s in m)
                a = ("Active: " + a) if all(s.lower() == "active" for _, _, s in m) else a
            if o.startswith("All within limits"):
                o = ""
        elif t == "ST02":
            hit = re.search(r"lowest hit ratio\s+(\S+(?:\s\S+)?)\s+([\d.]+)%", a)
            swaps = re.search(r"most swaps\s+(\S+(?:\s\S+)?)\s+([\d,]+)/day", o)
            ext = re.search(r"extended memory\s+([\d.]+)% used", a)
            if hit:
                a = f"Lowest hit ratio {hit.group(1)} {hit.group(2)}%" + \
                    (f"; {swaps.group(1)} swapping ~{swaps.group(2)}/day" if swaps else "") + \
                    (f"; extended memory {ext.group(1)}% used" if ext else "")
            if swaps and status in ("WARNING", "CRITICAL"):
                o = f"{swaps.group(1)} buffer is too small for the load - consider increasing it (ST02 > Current parameters)."
            elif o.startswith("All within limits"):
                o = ""
        elif t == "ST04":
            mem = re.search(r"HANA memory\s+([\d.]+)%\s*\(([^)]+)\)", a)
            ten = re.search(r"tenant\s+([\d.]+)%", a)
            big = re.search(r"largest column table\s+(\S+)\s+\(([^)]+)\)", a)
            if mem and status in ("WARNING", "CRITICAL"):
                o = (f"HANA memory at {mem.group(1)}% of the allocation limit"
                     + (f" (tenant memory {ten.group(1)}%)" if ten else "")
                     + " - review the top memory consumers and table unloads."
                     + (f" Largest table {big.group(1)} ({big.group(2)})." if big else ""))
            elif o.startswith("All within limits"):
                o = ""
        elif t == "ST05":
            if "switched off" in a.lower():
                on = [x.strip() for x in re.findall(r"([A-Za-z ]+?)\s+On\b", a.split(";", 1)[-1])]
                a = "All traces off" + (f"; {', '.join(x.lower() for x in on)} on" if on else "")
            if o.startswith("All within limits"):
                o = ""
        elif t == "STMS":
            m = re.search(r"Domain\s+(\S+):\s+(\d+)\s+systems?,\s+all available", a)
            if m:
                a = f"{m.group(2)} systems in {m.group(1)}, all reachable"
            if o.startswith("All within limits"):
                o = ""
        elif t == "STRUST":
            m = re.search(r"soonest expiry\s+(\S+)\s+\((\d+)\s+days\)", a)
            n = re.match(r"\s*(\d+)\s+own certificate", a)
            if m:
                a = ((f"{n.group(1)} own certificates; " if n else "")
                     + f"first expiry {m.group(1)} (in {int(m.group(2)):,} days)")
            if o.startswith("All within limits"):
                o = ""
        else:
            o = _tidy(o) or ""
    except Exception:  # noqa: BLE001
        return actual_text, obs
    o = re.sub(r"\s*\((?:OK|WARNING|CRITICAL|NORMAL)\)", "", o).replace(" -- ", " - ")
    return a, o


def _ms(v) -> str:
    n = _n(v)
    if n is None:
        return f"{v} ms"
    return f"{n / 1000:.1f} s" if n >= 1000 else f"{int(round(n))} ms"


def environment_label(env: str, system: str, sid: str) -> str:
    """'' / '—' -> a word a person would write, guessed from the name only when obvious."""
    env = str(env or "").strip()
    if env and env not in ("—", "-"):
        return env
    name = f"{system} {sid}".upper()
    for token, label in (("PRD", "Production"), ("PROD", "Production"), ("QAS", "Quality"),
                         ("QA", "Quality"), ("DEV", "Development"), ("SBX", "Sandbox")):
        if re.search(rf"(?<![A-Z]){token}(?![A-Z])", name.replace("_", " ")):
            return label
    return ""
