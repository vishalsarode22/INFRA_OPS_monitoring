"""
What changed before the incident.

Every 6 hours (IBO_CHANGE_SNAPSHOT_HOURS) per system, with the same RFC
session as the extra checks (core/check_alerts.py), this records:

    kernel        RFC_SYSTEM_INFO: kernel release, SAP release, DB, host
    kernel patch  from TH_SAPREL / TH_SAPREL2 when one exists (inspected first)
    components    CVERS: every software component and its support package level
    parameters    PAHI: profile parameter changes (fields found with
                  DDIF_FIELDINFO_GET -- the parameter history table)
    transports    TPALOG: imports with their return code

and compares with the previous snapshot. Every difference goes on the
system's timeline (category "change"), and alert mails and tickets get a
"Changed in the last 48 hours" block -- the single most useful thing to
read when something breaks.

Nothing is guessed: a source a system does not offer is skipped and listed
under "not available" in the snapshot.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)


def _path(system: str) -> str:
    from reporting.system_paths import safe_system_name
    from utils.paths import BASE_DIR
    return os.path.join(BASE_DIR, "dashboard", "changes", f"{safe_system_name(system)}.json")


def load(system: str) -> dict:
    try:
        from core.atomic_json import read_json
        return read_json(_path(system), default={}) or {}
    except Exception:
        return {}


def _save(system: str, data: dict) -> None:
    from core.atomic_json import write_json
    os.makedirs(os.path.dirname(_path(system)), exist_ok=True)
    write_json(_path(system), data)


def interval_s() -> float:
    try:
        return float(os.getenv("IBO_CHANGE_SNAPSHOT_HOURS", "6") or 6) * 3600
    except ValueError:
        return 6 * 3600.0


def _kernel(session) -> dict:
    out = {}
    got = session.call("RFC_SYSTEM_INFO")
    si = (got or {}).get("RFCSI_EXPORT") or {}
    for key, label in (("RFCKERNRL", "kernel_release"), ("RFCSAPRL", "sap_release"), ("RFCDBSYS", "db"),
                       ("RFCDBHOST", "db_host"), ("RFCOPSYS", "os"), ("RFCHOST", "host")):
        v = str(si.get(key, "")).strip()
        if v:
            out[label] = v
    from collectors.extra_checks import iface
    for fm in ("TH_SAPREL", "TH_SAPREL2"):
        params = iface(session, fm)
        if params is None:
            continue
        required = [n for n, p in params.items() if p.get("direction") == "RFC_IMPORT" and not p.get("optional")]
        if required:
            continue
        got = session.call(fm)
        if not got:
            continue
        for k, v in got.items():
            if "PATCH" in k.upper() and str(v).strip():
                out["kernel_patch"] = str(v).strip()
                break
        if "kernel_patch" in out:
            break
    return out


def _components(session) -> dict | None:
    rows = session.read_table("CVERS", ["COMPONENT", "RELEASE", "EXTRELEASE"], "", rows=500)
    if rows is None:
        return None
    return {r[0].strip(): f"{r[1].strip()} SP {r[2].strip()}" for r in rows if r and r[0].strip()}


def _pahi_fields(session) -> tuple[str, str, str] | None:
    from collectors.extra_checks import table_fields
    fields = table_fields(session, "PAHI") or set()
    name = next((f for f in ("PARNAME", "PARAMNAME", "PARAM_NAME") if f in fields), None)
    value = next((f for f in ("PARVALUE", "PARAMVALUE", "PARAM_VALUE") if f in fields), None)
    date = next((f for f in ("PARDATE", "PARAMDATE") if f in fields), None)
    return (name, value, date) if name and value and date else None


def _parameter_changes(session, since: datetime) -> tuple[list[dict] | None, str]:
    f = _pahi_fields(session)
    if not f:
        return None, "PAHI fields not found"
    name, value, date = f
    cap = 3000
    rows = session.read_table("PAHI", [date, name, value], f"{date} >= '{since:%Y%m%d}'", rows=cap)
    if rows is None:
        return None, f"PAHI read failed: {session.last_error[:80]}"
    if len(rows) >= cap:
        # RFC_READ_TABLE has no ORDER BY: a capped read is a random subset,
        # which would make changes appear and vanish between snapshots.
        return None, f"PAHI has {cap}+ rows since {since:%Y-%m-%d} -- parameter changes not compared"
    out = []
    for r in rows:
        d, n, v = (x.strip() for x in (r + ["", "", ""])[:3])
        if n:
            out.append({"date": d, "param": n, "value": v[:120]})
    return out, ""


def snapshot(session, system: str, since: datetime | None = None) -> dict:
    """Read everything now. Never raises. `since`: the previous snapshot's
    time -- parameter changes are read from a day before it (30 days at most)."""
    snap: dict = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "unavailable": []}
    try:
        snap["kernel"] = _kernel(session)
    except Exception as exc:  # noqa: BLE001
        snap["unavailable"].append(f"kernel: {exc}")
    try:
        comps = _components(session)
        if comps is None:
            snap["unavailable"].append(f"CVERS: {session.last_error[:80]}")
        else:
            snap["components"] = comps
    except Exception as exc:  # noqa: BLE001
        snap["unavailable"].append(f"CVERS: {exc}")
    try:
        floor = datetime.now() - timedelta(days=30)
        start = max(floor, since - timedelta(days=1)) if since else floor
        params, why = _parameter_changes(session, start)
        if params is None:
            snap["unavailable"].append(why)
        else:
            snap["parameters"] = params
    except Exception as exc:  # noqa: BLE001
        snap["unavailable"].append(f"PAHI: {exc}")
    try:
        from collectors.extra_checks import recent_imports
        imports, why = recent_imports(session, system, days=30)
        if imports is None:
            snap["unavailable"].append(why)
        else:
            snap["imports"] = imports
    except Exception as exc:  # noqa: BLE001
        snap["unavailable"].append(f"TPALOG: {exc}")
    return snap


def diff(old: dict, new: dict) -> list[tuple[str, str]]:
    """[(title, detail)] of what changed between two snapshots."""
    out: list[tuple[str, str]] = []
    ok, nk = old.get("kernel") or {}, new.get("kernel") or {}
    for key in ("kernel_release", "kernel_patch", "sap_release", "db", "db_host", "host"):
        if ok.get(key) and nk.get(key) and ok[key] != nk[key]:
            out.append((f"{key.replace('_', ' ')} changed: {ok[key]} -> {nk[key]}", ""))
    oc, nc = old.get("components"), new.get("components")
    if oc and nc:
        for comp in sorted(set(oc) | set(nc)):
            if oc.get(comp) != nc.get(comp):
                out.append((f"component {comp}: {oc.get(comp, 'new')} -> {nc.get(comp, 'removed')}", ""))
    seen = {(p["date"], p["param"], p["value"]) for p in (old.get("parameters") or [])}
    if "parameters" in old:
        for p in new.get("parameters") or []:
            if (p["date"], p["param"], p["value"]) not in seen:
                out.append((f"profile parameter {p['param']} = {p['value']}", f"changed {p['date']}"))
    seen_imp = {(i["trkorr"], i["at"]) for i in (old.get("imports") or [])}
    if "imports" in old:
        for i in new.get("imports") or []:
            if (i["trkorr"], i["at"]) not in seen_imp:
                out.append((f"transport {i['trkorr']} imported (RC {i['rc']})", f"at {i['at']}"))
    return out


def maybe_snapshot(session, system: str, force: bool = False) -> list[tuple[str, str]]:
    """Take a snapshot if due, record differences on the timeline. Returns them."""
    old = load(system)
    last = old.get("at")
    if not force and last:
        try:
            if (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds() < interval_s():
                return []
        except ValueError:
            pass
    if not getattr(session, "ok", False):
        return []
    try:
        prev_at = datetime.strptime(last, "%Y-%m-%d %H:%M:%S") if last else None
    except ValueError:
        prev_at = None
    new = snapshot(session, system, since=prev_at)
    changes = diff(old, new) if old else []
    # Keep the parameter rows the old snapshot saw but this (shorter) read
    # no longer covers, so the next diff still knows them.
    if "parameters" not in new and old.get("parameters"):
        new["parameters"] = old["parameters"]     # this read failed / was capped: keep the baseline
    elif "parameters" in new and old.get("parameters"):
        keep = {(p["date"], p["param"], p["value"]) for p in new["parameters"]}
        cut = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        new["parameters"] += [p for p in old["parameters"]
                              if (p["date"], p["param"], p["value"]) not in keep and p["date"] >= cut]
    try:
        _save(system, new)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"{system}: change snapshot not saved: {exc}")
    if changes:
        from core import timeline
        for title, detail in changes[:100]:
            sev = "warning" if title.startswith(("kernel", "component", "sap release")) or "(RC 8" in title \
                or re.search(r"\(RC (1[2-9]|[2-9]\d)\)", title) else "info"
            timeline.add(system, "change", title, detail, severity=sev)
        log.info(f"{system}: {len(changes)} change(s) recorded")
    return changes


def recent(system: str, hours: float = 48) -> list[dict]:
    from core import timeline
    return timeline.query(system, hours / 24, ["change"], limit=40)


def _when(at: str) -> str:
    try:
        return datetime.strptime(str(at)[:16], "%Y-%m-%d %H:%M").strftime("%d %b %H:%M")
    except Exception:  # noqa: BLE001
        return str(at)[5:16]


def summary_lines(rows: list[dict], limit: int = 5) -> list[str]:
    """Plain lines; transports imported in the same minute become one line."""
    out, groups = [], {}
    order = []
    for r in rows:
        title = str(r.get("title") or "")
        m = re.match(r"transport (\S+) imported \(RC (\d+)\)", title)
        if m:
            k = ("tr", str(r.get("at"))[:16])
            if k not in groups:
                groups[k] = []
                order.append(k)
            if m.group(1).upper() != "ALL":
                groups[k].append((m.group(1), int(m.group(2))))
            continue
        order.append(("x", title, str(r.get("at"))))
    for k in order:
        if k[0] == "tr":
            items = groups[k]
            if not items:
                continue
            worst = max(rc for _, rc in items)
            ids = [i for i, _ in items]
            shown = ", ".join(ids[:3]) + (f" and {len(ids) - 3} more" if len(ids) > 3 else "")
            state = "all OK" if worst <= 4 else f"with errors (worst return code {worst})"
            n = len(ids)
            out.append(f"{_when(k[1])} -- {n} transport{'s' if n != 1 else ''} imported, {state} ({shown})")
        else:
            out.append(f"{_when(k[2])} -- {k[1]}")
    more = len(out) - limit
    return out[:limit] + ([f"... and {more} more on the Timeline page"] if more > 0 else [])


def mail_block(system: str, hours: float = 48) -> str:
    import html
    rows = recent(system, hours)
    lines = summary_lines(rows)
    if not lines:
        return ""
    items = "".join(f"<li style='margin:0 0 3px'>{html.escape(x)}</li>" for x in lines)
    return (f'<div style="margin-top:16px;padding:10px 12px;background:#f8fafc;border:1px solid #e2e8f0;'
            f'border-radius:6px;font:13px/1.5 Segoe UI,Arial,sans-serif;color:#334155">'
            f'<b>Recent changes on {html.escape(system)}</b> (last {int(hours)} h) -- check these if the '
            f'problem started right after one of them.'
            f'<ul style="margin:6px 0 0 18px;padding:0">{items}</ul></div>')

