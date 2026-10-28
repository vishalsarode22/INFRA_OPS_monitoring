"""
Screen readers for ST03N, SMICM, ST02, ST04, ST05, STMS and STRUST.

Built from a read-only probe of CEAT S4_QAS (13 Oct 2026, release 752,
HANA 2.0 SPS08): every field name, grid column and tree node used here was
seen on that system (scripts/probe_gui_tcodes.py). Same contract as
sap_gui.tcode_actions: action(session, capture) -> dict of facts; capture()
takes a screenshot for the PDF.

READ-ONLY. Nothing here saves, activates, imports or changes anything:
  * ST05: the trace on/off state is READ; no trace button is ever pressed.
  * STMS: Overview > Systems and SAP's Connection Test (an RFC ping per
    system); no import, check, distribute or lock.
  * STRUST: only in display mode; F2 on the own-certificate subject shows
    its details. In change mode nothing is opened (it would lock the PSE).
  * ST04: display screens; "Apply Selection" runs the Large Tables query.
  * Unexpected popups are closed with Cancel (F12), never Yes/OK.

Every action also returns:
  report_summary  one line for the monitoring sheet's "Actual Result"
  report_rows     [[label, value], ...] for the PDF table
Per-system settings (Profile page, core/tcode_sets.py) arrive in SETTINGS,
set by the collector before each action.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime

from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy
from utils.logger import get_logger

log = get_logger(__name__, "application")

SETTINGS: dict = {}          # this T-code's settings for this system (set by the collector)


def setting(name, default):
    v = (SETTINGS or {}).get(name)
    return default if v in (None, "", []) else v


# ---------------------------------------------------------------- helpers

def _try(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 -- best effort, the screenshot stays the evidence
        return default


def _attr(obj, name, default=""):
    return _try(lambda: getattr(obj, name), default)


def _children(obj):
    if obj is None or not _attr(obj, "ContainerType", False):
        return []
    ch = _attr(obj, "Children", None)
    n = int(_attr(ch, "Count", 0) or 0) if ch is not None else 0
    out = []
    for i in range(n):
        c = _try(lambda i=i: ch.Item(i)) or _try(lambda i=i: ch.ElementAt(i))
        if c is not None:
            out.append(c)
    return out


def _walk(obj, depth=0, max_depth=20):
    if obj is None or depth > max_depth:
        return
    yield obj
    for c in _children(obj):
        yield from _walk(c, depth + 1, max_depth)


def _roots(session) -> list:
    """The work area and every docking area of the main window."""
    out = []
    usr = _try(lambda: session.findById("wnd[0]/usr"))
    if usr is not None:
        out.append(usr)
    for c in _children(_try(lambda: session.findById("wnd[0]"))):
        if "shellcont" in str(_attr(c, "Id", "")).rsplit("/", 1)[-1]:
            out.append(c)
    return out


def _all(session):
    for r in _roots(session):
        yield from _walk(r)


def _wait(session):
    wait_until_not_busy(session)


def _cancel_popups(session) -> list[str]:
    """Close any popup with Cancel (F12). Returns their titles."""
    seen = []
    for _ in range(3):
        found = False
        for n in (2, 1):
            w = _try(lambda n=n: session.findById(f"wnd[{n}]"))
            if w is None:
                continue
            found = True
            body = " ".join(str(_attr(o, "Text", "") or "").strip() for o in _walk(w, max_depth=6)
                            if _attr(o, "Type") in ("GuiLabel", "GuiTextField", "GuiCTextField", "GuiTextedit"))
            title = str(_attr(w, "Text", ""))
            seen.append(f"{title}: {body.strip()}"[:300] if body.strip() else title)
            _try(lambda w=w: w.sendVKey(12))
            _wait(session)
            if _try(lambda n=n: session.findById(f"wnd[{n}]")) is not None:
                _try(lambda w=w: w.close())
                _wait(session)
        if not found:
            break
    return seen


_AUTH = {"text": ""}          # "You are not authorized to use transaction ..." seen by _goto


def _goto(session, tcode):
    goto_tcode(session, tcode)
    _wait(session)
    text, _typ = _status(session)
    if re.search(r"not authori[sz]ed|no authori[sz]ation", text, re.I):
        _AUTH["text"] = text
    popups = _cancel_popups(session)
    for t in popups:
        if re.search(r"not authori[sz]ed|no authori[sz]ation", t, re.I) and not _AUTH["text"]:
            _AUTH["text"] = t
    return popups


def _status(session) -> tuple[str, str]:
    sb = _try(lambda: session.findById("wnd[0]/sbar"))
    return str(_attr(sb, "Text", "") or ""), str(_attr(sb, "MessageType", "") or "")


def _title(session) -> str:
    return str(_attr(_try(lambda: session.findById("wnd[0]")), "Text", "") or "")


def _field(session, name: str):
    """The text field with exactly this screen name (e.g. CERT-SUBJECT, not PSE-OWNCERT-SUBJECT)."""
    for o in _all(session):
        if _attr(o, "Type") in ("GuiTextField", "GuiCTextField") and \
                str(_attr(o, "Name", "")).upper() == name.upper():
            return o
    return None


def _field_text(session, name_suffix: str) -> str:
    return str(_attr(_field(session, name_suffix), "Text", "") or "").strip()


def _shells(session, subtype: str) -> list:
    return [o for o in _all(session) if _attr(o, "Type") == "GuiShell" and _attr(o, "SubType") == subtype]


def _tree_keys(t) -> list[str]:
    keys = _try(lambda: t.GetAllNodeKeys())
    if keys is None:
        return []
    try:
        return [str(k) for k in keys]
    except TypeError:
        return [str(_try(lambda i=i: keys.Item(i), "")) for i in range(int(_attr(keys, "Count", 0) or 0))]


def _node_text(t, k) -> str:
    return str(_try(lambda: t.GetNodeTextByKey(k), "") or "").strip()


def _parent(t, k) -> str:
    return str(_try(lambda: t.GetParent(k), "") or "")


def _tree(session, *words):
    """The tree whose node texts contain one of `words` (the navigation tree, not a system list)."""
    trees = _shells(session, "Tree")
    for t in trees:
        texts = " ".join(_node_text(t, k) for k in _tree_keys(t)[:300]).lower()
        if any(w in texts for w in words):
            return t
    return trees[0] if trees else None


def _kids(t, k) -> list[str]:
    return [c for c in _tree_keys(t) if _parent(t, c) == k]


def _open(session, t, k) -> list[str]:
    _try(lambda: setattr(t, "SelectedNode", k))
    _try(lambda: t.DoubleClickNode(k))
    _wait(session)
    return _cancel_popups(session)


def _grid_with(session, column_id: str):
    for g in _shells(session, "GridView"):
        order = _try(lambda g=g: g.ColumnOrder)
        try:
            cols = [str(c) for c in order] if order is not None else []
        except TypeError:
            cols = [str(_try(lambda i=i: order.Item(i), "")) for i in range(int(_attr(order, "Count", 0) or 0))]
        if column_id in cols:
            return g, cols
    return None, []


def _grid_rows(g, cols, limit=500, only=None) -> list[dict]:
    """Rows of an ALV grid; `only` limits the columns read (each cell is one call to SAP GUI)."""
    n = int(_attr(g, "RowCount", 0) or 0)
    use = [c for c in cols if c in only] if only else list(cols)
    rows = []
    for r in range(min(n, limit)):
        if r and r % 20 == 0:
            _try(lambda r=r: setattr(g, "FirstVisibleRow", r))      # load the next page of rows
        rows.append({c: str(_try(lambda r=r, c=c: g.GetCellValue(r, c), "") or "").strip() for c in use})
    return rows


def _expand_under(session, t, roots: list, rounds: int = 4) -> list[str]:
    """Expand every folder below `roots` (children appear only once their parent is expanded)."""
    done: set = set()
    for _ in range(rounds):
        keys = _tree_keys(t)
        inside = set(roots)
        changed = True
        while changed:                       # every key whose ancestor chain reaches a root
            changed = False
            for k in keys:
                if k not in inside and _parent(t, k) in inside:
                    inside.add(k)
                    changed = True
        todo = [k for k in keys if k in inside and k not in done and _try(lambda k=k: t.IsFolder(k), False)]
        if not todo:
            break
        for k in todo:
            done.add(k)
            _try(lambda k=k: t.ExpandNode(k))
        _wait(session)
    return _tree_keys(t)


def num(text):
    """'16,245.7' -> 16245.7; '' -> None. SAP may put the minus at the end ('12-')."""
    s = str(text or "").strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    neg = s.endswith("-")
    s = s.rstrip("-")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _icon_text(text: str) -> str:
    """'@3C\\QActive@' -> 'Active'."""
    t = str(text or "")
    m = re.search(r"\\Q([^@]*)@", t)
    if m:
        return m.group(1).strip()
    return re.sub(r"@[0-9A-Z]{2}@", "", t).strip()


def _list_labels(session) -> dict:
    """ABAP list screen: {row: [(col, text, label_object), ...]} from wnd[0]/usr/lbl[col,row]."""
    rows: dict = {}
    usr = _try(lambda: session.findById("wnd[0]/usr"))
    for o in _children(usr):
        m = re.search(r"lbl\[(\d+),(\d+)\]$", str(_attr(o, "Id", "")))
        if m:
            rows.setdefault(int(m.group(2)), []).append((int(m.group(1)), str(_attr(o, "Text", "")), o))
    for r in rows.values():
        r.sort(key=lambda x: x[0])
    return rows


def _gb(text) -> float | None:
    """'1.77 TB' / '48.03 GB' / '512 MB' -> GB."""
    m = re.match(r"\s*([\d.,]+)\s*([KMGTP]?B)", str(text or ""), re.I)
    if not m:
        return None
    v = num(m.group(1))
    if v is None:
        return None
    return round(v * {"KB": 1 / 1024 ** 2, "MB": 1 / 1024, "GB": 1, "TB": 1024, "PB": 1024 ** 2}[m.group(2).upper()], 3)


def _pair(text) -> tuple:
    """'1.77 TB /1.84 TB' -> (used_gb, total_gb, pct)."""
    parts = str(text or "").split("/")
    if len(parts) != 2:
        return None, None, None
    used, total = _gb(parts[0]), _gb(parts[1])
    pct = round(used / total * 100, 1) if used is not None and total else None
    return used, total, pct


def _pct(v) -> str:
    return "-" if v is None else f"{v}%"


def _size(gb) -> str:
    if gb is None:
        return "-"
    return f"{gb / 1024:.2f} TB" if gb >= 1024 else f"{gb:.2f} GB"


def _sap_date(text) -> date | None:
    for fmt in ("%d.%m.%Y", "%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(str(text or "").strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


# ================================================================== ST03N

_ST03_COLS = {"steps": "DIASTEPCNT", "avg_ms": "MRESPTIME", "cpu_ms": "MCPUTI", "db_ms": "MDBTI"}
DEFAULT_ST03_TYPES = ["DIALOG", "BACKGROUND", "WS-RFC"]


def _st03_read(session) -> tuple[dict, str, str]:
    g, cols = _grid_with(session, "TASKTYPE")
    out = {}
    if g is not None:
        for r in _grid_rows(g, cols, 80, only={"TASKTYPE", *_ST03_COLS.values()}):
            name = r.get("TASKTYPE", "").strip().upper()
            if name:
                out[name] = {k: num(r.get(c)) for k, c in _ST03_COLS.items()}
    return out, _field_text(session, "G_WL_DYNPRO-PERIOD_TYPE"), _field_text(session, "G_WL_DYNPRO-INSTANCE")


def _st03_tree(session):
    return _tree(session, "workload")


def action_st03n_workload(session, capture):
    """
    ST03N workload overview: steps, total time, average time and CPU time for
    the chosen task types (default Dialog, Background, WS-RFC).
    period "today" (default): today so far, all instances together -- SAP has
    no TOTAL for today until the day is over ("Total data for Today Not
    available"), so today's figures of each instance are combined (steps
    added, averages weighted by steps). Falls back to the latest full day of
    TOTAL when today is not readable.
    period "previous_day": TOTAL of the latest full day.
    A screen is only used when its Instance field shows what was opened, so
    a click that did not land never passes off old figures as new ones.
    """
    wanted = [str(t).strip().upper() for t in setting("task_types", DEFAULT_ST03_TYPES)]
    period = str(setting("period", "today")).lower()
    facts = {"tcode": "ST03N", "popups": _goto(session, "ST03N"), "wanted": wanted}
    t = _st03_tree(session)
    wl = None
    if t is not None:
        wl = next((k for k in _tree_keys(t) if not _parent(t, k) and _node_text(t, k).lower() == "workload"), None)
    if wl is None:
        capture("as_found")
        facts["error"] = "ST03N workload tree not found"
        return _st03_finish(facts, {}, "", "")
    _expand_under(session, t, [wl], rounds=3)
    instances = [k for k in _kids(t, wl) if _node_text(t, k).lower() != "total"]
    total = next((k for k in _kids(t, wl) if _node_text(t, k).lower() == "total"), None)

    def leaf(parent, folder_text, leaf_test):
        f = next((k for k in _kids(t, parent) if _node_text(t, k).lower() == folder_text), None)
        return next((k for k in (_kids(t, f) if f else []) if leaf_test(_node_text(t, k))), None)

    data, label, instance = {}, "", ""
    if period == "today":
        per_inst, skipped = {}, []
        for inst in instances:
            iname = _node_text(t, inst)
            today = leaf(inst, "day", lambda x: x.lower() == "today")
            if not today:
                skipped.append(f"{iname}: no 'Today' entry")
                continue
            facts["popups"].extend(_open(session, t, today))
            rows, per, shown = _st03_read(session)
            if shown.strip().lower() != iname.strip().lower():
                skipped.append(f"{iname}: screen showed instance '{shown or 'none'}'")
            elif not (_sap_date(per) == date.today() or "today" in per.lower()):
                skipped.append(f"{iname}: screen showed period '{per or 'none'}', not today")
            elif rows:
                per_inst[iname] = rows
                capture(f"today_{iname}")
            else:
                skipped.append(f"{iname}: no rows")
            t = _st03_tree(session) or t
        if skipped:
            facts["skipped"] = skipped
        if per_inst and not skipped:
            data = _combine(per_inst)
            label = f"Today {datetime.now():%d.%m.%Y} so far, all instances ({', '.join(per_inst)})"
            instance = "all instances"
            facts["st03_instances"] = list(per_inst)
        elif skipped:
            facts["note"] = ("today could not be read on every instance (" + "; ".join(skipped)
                             + "); latest full day of TOTAL shown instead")
    if not data and total is not None:
        target = leaf(total, "day", lambda x: _sap_date(x) is not None)
        if target is not None:
            facts["popups"].extend(_open(session, t, target))
            rows, per, shown = _st03_read(session)
            if shown.strip().upper() == "TOTAL" and _sap_date(per) == _sap_date(_node_text(t, target)):
                data, instance = rows, "TOTAL"
                label = f"{per or _node_text(t, target)}, total of all instances (latest full day)"
                if period == "today":
                    facts.setdefault("note", "today's figures were not readable; latest full day shown")
            else:
                facts["error"] = (f"ST03N showed instance '{shown or 'none'}', period '{per or 'none'}' "
                                  f"instead of the total of {_node_text(t, target)}")
            capture("workload_total_day")
    if not data and not facts.get("error"):
        facts["error"] = "no workload figures could be read"
        capture("as_found")
    return _st03_finish(facts, data, label, instance)


def _combine(per_inst: dict) -> dict:
    out = {}
    for rows in per_inst.values():
        for name, v in rows.items():
            o = out.setdefault(name, {"steps": 0.0, "_t": 0.0, "_cpu": 0.0, "_db": 0.0, "_bad": False})
            if v.get("steps") is None or (v.get("steps") and (v.get("avg_ms") is None or v.get("cpu_ms") is None)):
                o["_bad"] = True                  # never average a figure that was not read
                continue
            s = v.get("steps") or 0.0
            o["steps"] += s
            o["_t"] += s * (v.get("avg_ms") or 0.0)
            o["_cpu"] += s * (v.get("cpu_ms") or 0.0)
            o["_db"] += s * (v.get("db_ms") or 0.0)
    for o in out.values():
        s = o["steps"]
        o["avg_ms"] = round(o["_t"] / s, 1) if s else 0.0
        o["cpu_ms"] = round(o["_cpu"] / s, 1) if s else 0.0
        o["db_ms"] = round(o["_db"] / s, 1) if s else 0.0
        if o.pop("_bad"):
            o["avg_ms"] = o["cpu_ms"] = o["db_ms"] = None
        for k in ("_t", "_cpu", "_db"):
            o.pop(k)
    return out


def _hms(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def _st03_finish(facts, data, label, instance):
    rows, summary = [], []
    sel = {}
    for tt in facts["wanted"]:
        v = data.get(tt)
        if v is None:
            rows.append([tt, "no steps in this period" if data else "not read"])
            continue
        if v.get("steps") is None or v.get("avg_ms") is None:
            rows.append([tt, "figures not readable (see screenshot)"])
            continue
        steps = v.get("steps") or 0
        total_s = round(steps * (v.get("avg_ms") or 0) / 1000, 1)
        sel[tt] = {**v, "total_s": total_s}
        rows.append([tt, f"{steps:,.0f} steps; total time {_hms(total_s)}; avg {v.get('avg_ms') or 0:,.1f} ms; "
                         f"avg CPU {v.get('cpu_ms') or 0:,.1f} ms; avg DB {v.get('db_ms') or 0:,.1f} ms"])
        summary.append(f"{tt} {steps:,.0f} steps, total {_hms(total_s)}, avg {v.get('avg_ms') or 0:,.0f} ms, "
                       f"CPU {v.get('cpu_ms') or 0:,.0f} ms")
    facts.update({"period": label, "instance": instance, "task_types": sel, "all_task_types": sorted(data)})
    facts["report_rows"] = [["Period", label or "-"]] + rows + ([["Note", facts["note"]]] if facts.get("note") else [])
    facts["report_summary"] = (f"{label}: " + "; ".join(summary)) if summary else \
        (facts.get("error") or "no workload data read")
    return facts


# ================================================================== SMICM

_ACTIVE_ICONS = ("OKAY", "GREEN", "CHECK", "S_OK", "@01@", "@5B@", "@08@")
_INACTIVE_ICONS = ("NONO", "CANC", "RED", "ERROR", "FAIL", "@02@", "@0A@", "@5C@", "INACT")


_ICON_PROPS = ("IconName", "Text", "Tooltip", "DefaultTooltip", "AccText", "AccTooltip")


def _row_objects(session, y, x_actv) -> list[str]:
    """Every object on list line y near the Actv column, with what it says about itself (diagnostics)."""
    out = []
    usr = _try(lambda: session.findById("wnd[0]/usr"))
    for o in _children(usr):
        m = re.search(r"\[(\d+),(\d+)\]$", str(_attr(o, "Id", "")))
        if not m or int(m.group(2)) != y or x_actv is None or not (x_actv - 3 <= int(m.group(1)) <= x_actv + 6):
            continue
        props = {a: str(_attr(o, a, "") or "") for a in _ICON_PROPS + ("Type", "Name")}
        out.append(str(_attr(o, "Id", "")).rsplit("/", 1)[-1] + " " +
                   " ".join(f"{k}={v!r}" for k, v in props.items() if v))
    return out[:6] or ["no object at this position"]


def action_smicm_services(session, capture):
    """SMICM > Goto > Services: every ICM service with port and whether it is active."""
    facts = {"tcode": "SMICM", "popups": _goto(session, "SMICM")}
    capture("monitor")
    if not _menu(session, "goto", "services"):
        facts["error"] = "menu Goto > Services not found"
        facts["report_summary"] = facts["error"]
        facts["report_rows"] = []
        return facts
    rows = _list_labels(session)
    header_y = next((y for y, cells in sorted(rows.items()) if any(t.strip() == "Protocol" for _, t, _o in cells)), None)
    services = []
    if header_y is not None:
        cols = {t.strip(): x for x, t, _o in rows[header_y]}
        x_actv = cols.get("Actv")
        for y in sorted(k for k in rows if k > header_y):
            cells = {x: (t, o) for x, t, o in rows[y]}
            proto = next((t.strip() for x, (t, _o) in cells.items() if x == cols.get("Protocol")), "")
            if not proto:
                continue
            port = next((t.strip() for x, (t, _o) in cells.items() if x == cols.get("Service Name/Port")), "")
            host = next((t.strip() for x, (t, _o) in cells.items() if x == cols.get("Host Name")), "")
            active = None
            o = None
            if x_actv is not None:
                for x in (x_actv, x_actv + 1, x_actv - 1):
                    o = _try(lambda x=x, y=y: session.findById(f"wnd[0]/usr/chk[{x},{y}]"))
                    if o is not None:
                        break
            if o is None and x_actv is not None:
                # The icon is the label exactly under "Actv" (lbl[93,y] on 14 Oct); SAP puts
                # empty labels beside it (lbl[92,y]), so an empty one is never taken.
                for x in (x_actv, x_actv + 1, x_actv - 1, x_actv + 2):
                    cand = cells.get(x, (None, None))[1] or \
                        _try(lambda x=x, y=y: session.findById(f"wnd[0]/usr/lbl[{x},{y}]"))
                    if cand is not None and any(str(_attr(cand, a, "") or "").strip() for a in _ICON_PROPS):
                        o = cand
                        break
            sig = " ".join(str(_attr(o, a, "") or "") for a in _ICON_PROPS).upper() if o is not None else ""
            if o is not None and _attr(o, "Type") == "GuiCheckBox":
                active = bool(_attr(o, "Selected", False))
                sig = f"checkbox {'ticked' if active else 'not ticked'}"
            elif any(w in sig for w in _ACTIVE_ICONS) or sig.strip() in ("X", "YES", "ACTIVE"):
                active = True
            elif any(w in sig for w in _INACTIVE_ICONS):
                active = False
            if active is None:
                cell = _row_objects(session, y, x_actv)
                log.warning(f"SMICM: state of {proto} {port} not recognised (cell: {sig.strip() or 'empty'}); "
                            f"objects near the Actv column: {cell}")
                facts.setdefault("actv_debug", []).append({"service": f"{proto} {port}", "objects": cell})
            services.append({"protocol": proto, "port": port, "host": host, "active": active,
                             "icon": sig.strip()[:40]})
    capture("services")
    inactive = [s for s in services if s["active"] is False]
    unknown = [s for s in services if s["active"] is None]
    facts.update({"services": services, "inactive": len(inactive), "unknown_state": len(unknown)})
    facts["report_rows"] = [[f"{s['protocol']} {s['port']}",
                             "active" if s["active"] else "NOT active" if s["active"] is False
                             else f"state not readable ({s['icon'] or 'empty cell'})"]
                            for s in services]
    for d in facts.get("actv_debug") or []:
        facts["report_rows"].append([f"Actv cell {d['service']} (diagnostics)", "; ".join(d["objects"])[:300]])
    facts["report_summary"] = (f"{len(services)} service(s): " + ", ".join(
        f"{s['protocol']} {s['port']} {'active' if s['active'] else 'NOT active' if s['active'] is False else '?'}"
        for s in services)) if services else "no ICM service listed"
    return facts


def _menu(session, *path) -> bool:
    node = _try(lambda: session.findById("wnd[0]/mbar"))
    for needle in path:
        nxt = next((c for c in _children(node)
                    if needle.lower() in str(_attr(c, "Text", "")).lower().replace("&", "")), None)
        if nxt is None:
            return False
        node = nxt
    ok = _try(lambda: (node.Select(), True)[-1], False)
    _wait(session)
    _cancel_popups(session)
    return bool(ok)


# ================================================================== ST02

_ST02_HEAD = {"HitRatio %": "hit_ratio", "Alloc. KB": "alloc_kb", "Freesp. KB": "free_kb",
              "% Free Sp.": "free_pct", "Dir. Size": "dir_size", "FreeDirEnt": "free_dir",
              "% Free Dir": "free_dir_pct", "Swaps": "swaps", "DB Accs": "db_accesses"}
_MEM_HEAD = {"Curr.Use %": "use_pct", "CurUse[KB]": "use_kb", "Max.Use[KB]": "max_kb",
             "In Mem[KB]": "in_mem_kb", "OnDisk[KB]": "on_disk_kb"}
DEFAULT_ST02_GRADED = ["Program", "CUA", "Screen", "Tables Buffer", "Nametab Table definition",
                       "Nametab Field definition"]


def _row_values(cells, head: dict) -> dict:
    out = {}
    for x, text, _o in cells:
        best = None
        for hx, key in head.items():
            if hx <= x + 2 and (best is None or hx > best[0]):
                best = (hx, key)
        if best and best[0] != min(head) and best[1]:      # not the name column, not an unused column
            v = num(text)
            if v is not None:
                out[best[1]] = v
    return out


def action_st02_tune(session, capture):
    """ST02 Tune Summary of the logon instance: buffers (hit ratio, free space, swaps) and SAP memory."""
    facts = {"tcode": "ST02", "popups": _goto(session, "ST02")}
    rows = _list_labels(session)
    capture("tune_summary")
    flat = {y: [(x, t.strip()) for x, t, _o in cells] for y, cells in rows.items()}

    def find(y, x0):
        return next((t for x, t in flat.get(y, []) if x == x0), "")
    snap_y = next((y for y, c in flat.items() if any(t.startswith("Date + Time of Snapshot") for _, t in c)), None)
    if snap_y is not None:
        vals = [t for x, t in sorted(flat[snap_y]) if t and not t.startswith(("Date", "Startup"))]
        facts["snapshot"] = " ".join(vals[:2])
        facts["startup"] = " ".join(vals[2:4])
    facts["instance"] = (re.search(r"\(([^)]+)\)", _title(session)) or [None, ""])[1]

    buf_y = next((y for y, c in sorted(flat.items()) if any(t == "Buffer" for _, t in c)), None)
    mem_y = next((y for y, c in sorted(flat.items()) if any(t == "SAP Memory" for _, t in c)), None)
    buffers, memory = [], []
    if buf_y is not None:
        head = {x: _ST02_HEAD.get(t) for x, t in flat[buf_y] if t}
        head[min(x for x, _ in flat[buf_y])] = "name"
        parent = ""
        end = mem_y or 10 ** 6
        for y in sorted(k for k in rows if buf_y < k < end):
            cells = rows[y]
            value_cols = [h for h in head if h != min(head)]
            if not value_cols:
                break
            name_cell = next((t for x, t, _o in cells if x < min(value_cols) - 1), "")
            if not name_cell.strip():
                continue
            vals = _row_values(cells, head)
            name = name_cell.strip()
            if not name_cell.startswith("  "):
                parent = name
            elif parent:
                name = f"{parent.split(' (')[0]} {name}"
            if "hit_ratio" in vals or "alloc_kb" in vals:
                buffers.append({"buffer": name, **vals})
    if mem_y is not None:
        head = {x: _MEM_HEAD.get(t) for x, t in flat[mem_y] if t}
        head[min(x for x, _ in flat[mem_y])] = "name"
        for y in sorted(k for k in rows if mem_y < k < mem_y + 12):
            cells = rows[y]
            name = next((t.strip() for x, t, _o in cells if x < 10), "")
            if not name or name.startswith("Call Stat"):
                if name.startswith("Call Stat"):
                    break
                continue
            memory.append({"area": name, **_row_values(cells, head)})
    facts.update({"buffers": buffers, "memory": memory})
    def n0(v):
        return f"{v:,.0f}" if isinstance(v, (int, float)) else "-"
    facts["report_rows"] = [[b["buffer"], f"hit {b.get('hit_ratio', '-')}%, free {b.get('free_pct', '-')}%, "
                                          f"swaps {n0(b.get('swaps'))} since startup"] for b in buffers] + \
                           [[f"Memory: {m['area']}", f"{m.get('use_pct', '-')}% used ({n0(m.get('use_kb'))} KB)"]
                            for m in memory]
    want = {str(n).strip().lower() for n in setting("graded_buffers", DEFAULT_ST02_GRADED)}
    graded = [b for b in buffers if b["buffer"].strip().lower() in want] or buffers
    parts = []
    hits = [b for b in graded if b.get("hit_ratio") is not None]
    if hits:
        low = min(hits, key=lambda b: b["hit_ratio"])
        parts.append(f"lowest hit ratio {low['buffer']} {low['hit_ratio']}%")
    swapping = sorted((b for b in graded if b.get("swaps")), key=lambda b: -b["swaps"])
    parts.append("swaps since startup: " + (", ".join(f"{b['buffer']} {b['swaps']:,.0f}" for b in swapping[:4])
                                            if swapping else "none"))
    em = next((m for m in memory if m["area"].lower() == "extended memory"), None)
    if em and em.get("use_pct") is not None:
        parts.append(f"extended memory {em['use_pct']}% used")
    facts["report_summary"] = (f"{facts.get('instance') or 'logon instance'} (snapshot {facts.get('snapshot', '-')}, "
                               f"started {facts.get('startup', '-')}): " + "; ".join(parts)) if buffers else \
        "ST02 buffer list not readable (see screenshot)"
    if not buffers:
        facts["error"] = "ST02 buffer list not readable"
    return facts


# ================================================================== ST04

DEFAULT_TOP_N = 10


def action_st04_db(session, capture):
    """
    ST04. HANA: memory used / allocation limit, tenant memory, data and log
    volume, plus Large Tables (column tables) top N by memory and top N by
    records. Other databases: the overview screen and its size figures.
    """
    top_n = max(1, min(int(setting("top_n", DEFAULT_TOP_N) or DEFAULT_TOP_N), 50))
    facts = {"tcode": "ST04", "popups": _goto(session, "ST04"), "top_n": top_n}
    if _field(session, "HDB_OVERVIEW-DB_MEMORY") is None:
        return _st04_other(session, capture, facts)
    facts["database"] = "HANA"
    m_used, m_total, m_pct = _pair(_field_text(session, "HDB_OVERVIEW-DB_MEMORY"))
    t_used, t_total, t_pct = _pair(_field_text(session, "HDB_OVERVIEW-DB_MEMORY_TEN"))
    d_used, d_total, d_pct = _pair(_field_text(session, "HDB_OVERVIEW-DB_STORAGE_DATA"))
    l_used, l_total, l_pct = _pair(_field_text(session, "HDB_OVERVIEW-DB_STORAGE_LOG"))
    facts.update({
        "state": _field_text(session, "HDB_OVERVIEW-STATE"),
        "version": _field_text(session, "HDB_OVERVIEW-DB_RELEASE"),
        "memory": {"used_gb": m_used, "limit_gb": m_total, "pct": m_pct},
        "tenant_memory": {"used_gb": t_used, "total_gb": t_total, "pct": t_pct},
        "data_volume": {"used_gb": d_used, "disk_gb": d_total, "pct": d_pct},
        "log_volume": {"used_gb": l_used, "disk_gb": l_total, "pct": l_pct},
        "alerts": [a for a in (_field_text(session, f"HDB_OVERVIEW-ALERTS_{i}") for i in (1, 2, 3)) if a],
    })
    capture("overview")
    tables = _large_tables(session, capture, facts, top_n)
    rows = [["Memory used / allocation limit", f"{_size(m_used)} / {_size(m_total)} ({_pct(m_pct)})"],
            ["Tenant memory used / total", f"{_size(t_used)} / {_size(t_total)} ({_pct(t_pct)})"],
            ["Data volume / disk", f"{_size(d_used)} / {_size(d_total)} ({_pct(d_pct)})"],
            ["Log volume / disk", f"{_size(l_used)} / {_size(l_total)} ({_pct(l_pct)})"]]
    for title, key in (("by memory", "top_by_memory"), ("by records", "top_by_records")):
        for i, r in enumerate(tables.get(key) or [], 1):
            rows.append([f"Top {i} column table {title}", f"{r['table']}: {_size(r['memory_gb'])}, {r['records']:,.0f} records"])
    if tables.get("note"):
        rows.append(["Large tables", tables["note"]])
    facts["report_rows"] = rows
    facts["report_summary"] = (f"HANA memory {_pct(m_pct)} ({_size(m_used)} of {_size(m_total)}), "
                               f"tenant {_pct(t_pct)}, data volume {_pct(d_pct)}, log volume {_pct(l_pct)}"
                               + (f"; largest column table {tables['top_by_memory'][0]['table']} "
                                  f"({_size(tables['top_by_memory'][0]['memory_gb'])})"
                                  if tables.get("top_by_memory") else ""))
    return facts


def _large_tables(session, capture, facts, top_n) -> dict:
    out: dict = {}
    t = _tree(session, "system information", "large tables")
    key = None
    if t is not None:
        for k in _tree_keys(t):
            if _node_text(t, k).lower() == "system information":
                _try(lambda k=k: t.ExpandNode(k))
        _wait(session)
        for k in _tree_keys(t):
            if _node_text(t, k).lower() == "large tables" and "system information" in _node_text(t, _parent(t, k)).lower():
                key = k
                break
        key = key or next((k for k in _tree_keys(t) if _node_text(t, k).lower() == "large tables"), None)
    if key is None:
        out["note"] = "Large Tables screen not found in the DBA Cockpit tree"
        facts["large_tables"] = out
        return out
    facts.setdefault("popups", []).extend(_open(session, t, key))
    tab = next((o for o in _all(session) if _attr(o, "Type") == "GuiTab"
                and "column tables" in str(_attr(o, "Text", "")).lower()), None)
    if tab is not None:
        _try(lambda: tab.select())
        _wait(session)

    def apply():
        btn = next((o for o in _all(session) if _attr(o, "Type") == "GuiButton"
                    and "apply selection" in str(_attr(o, "Text", "")).lower()), None)
        if btn is None:
            return False
        _try(lambda: btn.press())
        _wait(session)
        _cancel_popups(session)
        return True

    def read():
        g, cols = _grid_with(session, "TABLE_NAME")
        rows = []
        if g is not None:
            mem_col = "MEMORY_SIZE_IN_TOTAL" if "MEMORY_SIZE_IN_TOTAL" in cols else None
            rec_col = "RECORD_COUNT" if "RECORD_COUNT" in cols else None
            if mem_col is None or rec_col is None:
                titles = {c: str(_try(lambda c=c: g.GetDisplayedColumnTitle(c), "") or "") for c in cols}
                mem_col = mem_col or next((c for c, tt in titles.items() if tt.lower().startswith("total memory size")), None)
                rec_col = rec_col or next((c for c, tt in titles.items() if tt.strip().lower() == "records"), None)
            if mem_col is None:
                return rows
            sch_col = "SCHEMA_NAME" if "SCHEMA_NAME" in cols else None
            for r in _grid_rows(g, cols, 100, only={"TABLE_NAME", mem_col, rec_col, sch_col}):
                mem, rec = num(r.get(mem_col)), num(r.get(rec_col)) if rec_col else None
                if r.get("TABLE_NAME") and mem is not None:
                    rows.append({"table": r["TABLE_NAME"], "schema": r.get(sch_col, "") if sch_col else "",
                                 "memory_gb": round(mem / 1024 ** 3, 3), "records": rec or 0})
        return rows

    if not apply():
        out["note"] = "Apply Selection button not found"
        facts["large_tables"] = out
        return out
    by_mem = read()
    capture("large_tables_by_memory")
    out["top_by_memory"] = sorted(by_mem, key=lambda r: -r["memory_gb"])[:top_n]
    combo = next((o for o in _all(session) if _attr(o, "Type") == "GuiComboBox"
                  and str(_attr(o, "Name", "")).upper().endswith("SORT_BY")), None)
    rec_key = None
    if combo is not None:
        entries = _attr(combo, "Entries", None)
        for i in range(int(_attr(entries, "Count", 0) or 0)):
            e = _try(lambda i=i: entries.Item(i))
            if e is not None and "record" in str(_attr(e, "Value", "")).lower():
                rec_key = str(_attr(e, "Key", ""))
                break
    if rec_key is not None and _try(lambda: (setattr(combo, "Key", rec_key), True)[-1], False) and apply():
        by_rec = read()
        capture("large_tables_by_records")
        out["top_by_records"] = sorted(by_rec, key=lambda r: -r["records"])[:top_n]
    else:
        out["top_by_records"] = sorted(by_mem, key=lambda r: -r["records"])[:top_n]
        out["note"] = "top by records taken from the 100 largest tables by memory (no record sort offered)"
    facts["large_tables"] = out
    return out


def _st04_other(session, capture, facts):
    """Not HANA: the overview screenshot and every label/value pair about size or space."""
    capture("overview")
    pairs, label = {}, None
    for o in _all(session):
        typ, txt = _attr(o, "Type"), str(_attr(o, "Text", "") or "").strip()
        if not txt:
            continue
        if typ == "GuiLabel" or (typ in ("GuiTextField", "GuiCTextField") and not re.search(r"\d", txt)):
            label = txt
        elif label and re.search(r"\d", txt) and re.search(r"size|free|used|space|total|data|log", label, re.I):
            pairs[label] = txt
            label = None
    # Only the window title names the database here; a text further down the
    # screen (a second DB connection, a help line) is not evidence.
    title = _title(session)
    db = next((name for name, pat in (("SAP ASE", r"\bASE\b|Sybase"), ("Oracle", r"Oracle"),
                                      ("MS SQL Server", r"SQL Server|MSSQL"), ("IBM Db2", r"\bDB[26]\b|Db2"),
                                      ("SAP MaxDB", r"MaxDB")) if re.search(pat, title)), "")
    mentions_hana = any("hana" in str(_attr(o, "Text", "") or "").lower() for o in _all(session))
    facts.update({"database": db or "not recognised", "title": title, "fields": pairs,
                  "mentions_hana": mentions_hana or "hana" in title.lower()})
    facts["report_rows"] = [[k, v] for k, v in list(pairs.items())[:20]] or [["Overview", "see screenshot"]]
    head = f"{db} database" if db else "Database not recognised as HANA"
    facts["report_summary"] = (f"{head}: " + "; ".join(f"{k} {v}" for k, v in list(pairs.items())[:4])) \
        if pairs else f"{head}: overview captured, values not read (see screenshot)"
    return facts


# ================================================================== ST05

_ST05_TYPES = {"SQL_TRACE": "SQL", "BUF_TRACE": "Buffer", "ENQ_TRACE": "Enqueue", "RFC_TRACE": "RFC",
               "HTTP_TRACE": "HTTP", "APC_TRACE": "APC", "AMC_TRACE": "AMC"}


def action_st05_status(session, capture):
    """ST05: trace state (on/off), selected trace types, stack trace and progress display. Nothing is pressed."""
    facts = {"tcode": "ST05", "popups": _goto(session, "ST05")}
    selected = {}
    for o in _all(session):
        name = str(_attr(o, "Name", "")).upper()
        if _attr(o, "Type") == "GuiCheckBox" and name in _ST05_TYPES:
            selected[_ST05_TYPES[name]] = bool(_attr(o, "Selected", False))

    def radio(name):
        o = next((x for x in _all(session) if _attr(x, "Type") == "GuiRadioButton"
                  and str(_attr(x, "Name", "")).upper() == name), None)
        return None if o is None else bool(_attr(o, "Selected", False))
    stack_on, progress_on = radio("STACK_ON"), radio("PROGRESS_ON")
    lines = []
    for o in _all(session):
        if _attr(o, "Type") in ("GuiTextField", "GuiCTextField") and \
                "MAINSWITCHES" in str(_attr(o, "Id", "")).upper():
            t = str(_attr(o, "Text", "") or "").strip()
            if t:
                lines.append(t)
    capture("trace_status")
    all_off = any("all traces are switched off" in ln.lower() for ln in lines)
    on_lines = [] if all_off else [ln for ln in lines if re.search(r"\b(on|active|activated)\b", ln, re.I)
                                   and not re.search(r"\bswitched off\b", ln, re.I)]
    if not all_off and lines and not on_lines:
        on_lines = [ln for ln in lines if not re.search(r"\bswitched off\b", ln, re.I)][:1]
    facts.update({"trace_state": lines, "traces_on": len(on_lines) if lines else None, "all_off": all_off,
                  "trace_types_selected": selected, "stack_trace": stack_on, "progress_display": progress_on})
    onoff = lambda v: "On" if v else "Off" if v is False else "-"  # noqa: E731
    facts["report_rows"] = ([["Trace state", ln] for ln in lines] or [["Trace state", "not readable"]]) + \
        [[f"{k} trace type", "ticked" if v else "not ticked"] for k, v in selected.items()] + \
        [["Stack trace", onoff(stack_on)], ["Progress display", onoff(progress_on)]]
    facts["report_summary"] = ("All traces are switched off" if all_off else
                               f"{len(on_lines)} trace(s) ON: " + "; ".join(on_lines[:3]) if on_lines else
                               "; ".join(lines[:2]) or "trace state not readable") + \
        f"; stack trace {onoff(stack_on)}, progress display {onoff(progress_on)}"
    return facts


# ================================================================== STMS

def _toolbar_button(session, tooltip_start: str):
    for o in _children(_try(lambda: session.findById("wnd[0]/tbar[1]"))):
        if str(_attr(o, "Tooltip", "")).strip().lower().startswith(tooltip_start.lower()):
            return o
    return None


def action_stms_systems(session, capture):
    """STMS > System Overview: every system of the transport domain with its status, and SAP's Connection Test per system."""
    test = str(setting("connection_test", True)).lower() not in ("false", "0", "no", "off")
    facts = {"tcode": "STMS", "popups": _goto(session, "STMS")}
    btn = _toolbar_button(session, "System Overview")
    opened = bool(btn is not None and _try(lambda: (btn.press(), True)[-1], False))
    if opened:
        _wait(session)
        facts["popups"].extend(_cancel_popups(session))
    else:
        opened = _menu(session, "overview", "systems")
    g, cols = _grid_with(session, "SYSNAM")
    if not opened or g is None:
        capture("as_found")
        facts.update({"error": "STMS system overview not opened", "report_rows": [],
                      "report_summary": "STMS system overview not opened"})
        return facts
    domain = (re.search(r"Domain\s+(\S+)", _title(session)) or [None, ""])[1]
    systems = []
    for r in _grid_rows(g, cols, 200, only={"SYSNAM", "SYSTXT", "SAPREL", "STATE", "TYPE"}):
        if r.get("SYSNAM"):
            systems.append({"system": r["SYSNAM"], "text": r.get("SYSTXT", ""),
                            "release": r.get("SAPREL", ""), "status": _icon_text(r.get("STATE", "")),
                            "controller": "controller" in r.get("TYPE", "").lower()})
    capture("system_overview")
    if test and systems:
        tbtn = _toolbar_button(session, "Connection Test")
        if tbtn is not None:
            for i, s in enumerate(systems):
                _try(lambda i=i: g.setCurrentCell(i, "SYSNAM"))
                _try(lambda i=i: setattr(g, "SelectedRows", str(i)))
                _try(lambda: tbtn.press())
                _wait(session)
                popups = _cancel_popups(session)
                text, typ = _status(session)
                about_it = bool(re.search(rf"\b{re.escape(s['system'])}\b", text))
                s["connection"] = text or ("; ".join(p for p in popups if p) or "no answer")
                if about_it and re.search(r"\bOK\b", text) and typ.upper() in ("S", ""):
                    s["connection_ok"] = True
                elif about_it or popups or typ.upper() in ("E", "A"):
                    s["connection_ok"] = False
                else:
                    s["connection_ok"] = None          # the answer did not name this system: not judged
                g = _grid_with(session, "SYSNAM")[0] or g
                tbtn = _toolbar_button(session, "Connection Test") or tbtn
            capture("connection_test")
        else:
            facts["note"] = "Connection Test button not found; status column only"
    bad = [s for s in systems if s["status"].lower() != "active" or s.get("connection_ok") is False]
    unjudged = [s["system"] for s in systems if s.get("connection_ok", True) is None]
    facts.update({"domain": domain, "systems": systems, "unavailable": len(bad), "unjudged": unjudged})

    def state(s):
        out = s["status"] or "status not shown"
        if "connection_ok" in s:
            out += {True: "; connection OK", False: f"; connection FAILED ({s['connection']})",
                    None: f"; connection not judged ({s['connection']})"}[s["connection_ok"]]
        return out
    facts["report_rows"] = [[s["system"] + (" (controller)" if s["controller"] else ""), state(s)] for s in systems]
    if facts.get("note"):
        facts["report_rows"].append(["Note", facts["note"]])
    facts["report_summary"] = (f"Domain {domain}: {len(systems)} systems, "
                               + ("all available" if not bad else
                                  f"NOT available: {', '.join(s['system'] for s in bad)}")
                               + (f"; connection not judged for {', '.join(unjudged)}" if unjudged else ""))
    return facts


# ================================================================== STRUST

def action_strust_certs(session, capture):
    """STRUST (display mode only): each PSE's own certificate with valid-to date and days left."""
    facts = {"tcode": "STRUST", "popups": _goto(session, "STRUST")}
    if "change" in _title(session).lower():
        # Opened in change mode (the user may change STRUST). No PSE is open
        # yet, so nothing is locked; switch to display before opening any.
        btn = _toolbar_button(session, "Display <-> Change")
        if btn is not None:
            _try(lambda: btn.press())
            _wait(session)
            facts["popups"].extend(_cancel_popups(session))
            facts["switched_to_display"] = True
    if "display" not in _title(session).lower():
        capture("as_found")
        facts.update({"error": f"STRUST opened as '{_title(session)}', not in display mode",
                      "report_rows": [], "report_summary":
                      "STRUST not in display mode for the monitoring user: certificates not opened "
                      "(opening a PSE in change mode would lock it)"})
        return facts
    t = _tree(session, "system pse", "ssl server")
    if t is None:
        capture("as_found")
        facts.update({"error": "STRUST tree not found", "report_rows": [], "report_summary": "STRUST tree not found"})
        return facts
    tops = [k for k in _tree_keys(t) if not _parent(t, k)]
    _expand_under(session, t, tops, rounds=4)
    leaves = []
    for k in _tree_keys(t):
        if _try(lambda k=k: t.IsFolder(k), True):
            continue
        txt = _node_text(t, k)
        chain, cur, guard = [], _parent(t, k), 0
        while cur and guard < 10:
            chain.insert(0, _node_text(t, cur))
            cur, guard = _parent(t, cur), guard + 1
        if (chain[0] if chain else txt).lower() == "file":
            continue
        leaves.append((k, " / ".join(chain + [txt])))
    today = date.today()
    certs = []
    last_shown = None                 # (PSE type, serial) of the certificate shown for the previous PSE
    for k, label in leaves[:40]:
        if not _ok_to_continue(session):
            break
        t = _tree(session, "system pse", "ssl server") or t
        _open(session, t, k)
        if "display" not in _title(session).lower():          # never stay anywhere but display mode
            facts["error"] = f"left display mode at {label}"
            break
        text, _typ = _status(session)
        entry = {"pse": label}
        subj = _field(session, "PSE-OWNCERT-SUBJECT")
        own = str(_attr(subj, "Text", "") or "").strip()
        # A PSE that is not set up shows "Error while loading PSE", the field's
        # placeholder "Subject" and the PREVIOUS PSE's certificate below it.
        if "error while loading" in text.lower() or subj is None or not own or own.lower() == "subject":
            entry.update({"configured": False, "note": text or "no own certificate"})
            certs.append(entry)
            continue
        kind = label.split(" / ")[0]

        def details():
            _try(lambda: subj.setFocus())
            _try(lambda: session.findById("wnd[0]").sendVKey(2))     # F2: show the own certificate below
            _wait(session)
            _cancel_popups(session)
            return _field_text(session, "CERT-SUBJECT"), _field_text(session, "CERT-SNUMBER")

        shown, serial = details()
        # The details pane keeps the previous PSE's certificate until it is
        # replaced. The same certificate on two instances of one PSE type is
        # normal (system-wide SSL); the same one on another PSE type means
        # the pane did not change: try once more, then say it was not read.
        same_as = ""
        if last_shown and serial and serial == last_shown[1]:
            shown, serial = details()
            if serial == last_shown[1]:
                same_as = last_shown[2]
        ok = shown == own
        valid_to = _sap_date(_field_text(session, "CERT-DATETO")) if ok else None
        entry.update({"configured": True, "subject": own,
                      "valid_from": _field_text(session, "CERT-DATEFROM") if ok else "",
                      "valid_to": valid_to.strftime("%d.%m.%Y") if valid_to else "",
                      "days_left": (valid_to - today).days if valid_to else None,
                      "serial": serial if ok else ""})
        if not ok:
            entry["note"] = "certificate details did not open"
        else:
            if same_as:
                entry["note"] = f"same certificate (serial {serial}) as {same_as}"
            last_shown = (kind, serial, label)
        certs.append(entry)
        capture(f"pse_{len(certs)}")
    with_date = [c for c in certs if c.get("days_left") is not None]
    soonest = min(with_date, key=lambda c: c["days_left"]) if with_date else None
    unread = [c["pse"] for c in certs if c.get("configured") and c.get("days_left") is None]
    facts.update({"certificates": certs, "min_days_left": soonest["days_left"] if soonest else None,
                  "soonest": soonest, "unreadable": unread})

    def line(c):
        if not c.get("configured"):
            return "no own certificate (" + (c.get("note") or "not configured") + ")"
        if c.get("days_left") is None:
            return "valid-to not readable" + (f" ({c['note']})" if c.get("note") else "")
        d = c["days_left"]
        out = f"valid to {c['valid_to']} ({d} days left)" if d >= 0 else f"EXPIRED on {c['valid_to']} ({-d} days ago)"
        return out + (f"; {c['note']}" if c.get("note") else "")
    facts["report_rows"] = [[c["pse"], line(c)] for c in certs]
    facts["report_summary"] = ((f"{len(with_date)} own certificate(s); soonest expiry {soonest['valid_to']} "
                                f"({soonest['days_left']} days) - {soonest['pse']}") if soonest
                               else "no own certificate with a readable expiry date") + \
        (f"; expiry not readable for {len(unread)}" if unread else "")
    return facts


def _ok_to_continue(session) -> bool:
    """False when SAP GUI is gone (the collector's retry takes over)."""
    return _try(lambda: session.findById("wnd[0]"), None) is not None


ACTIONS = {
    "st03n_workload": action_st03n_workload,
    "smicm_services": action_smicm_services,
    "st02_tune": action_st02_tune,
    "st04_db": action_st04_db,
    "st05_status": action_st05_status,
    "stms_systems": action_stms_systems,
    "strust_certs": action_strust_certs,
}


def _tagged(name, fn):
    """Every result says which reader made it and with which settings (core/gui_checks.py grades it)."""
    def run(session, capture):
        _AUTH["text"] = ""
        facts = fn(session, capture) or {}
        if _AUTH["text"]:
            facts.update({"not_authorized": True, "error": _AUTH["text"],
                          "report_summary": f"No authorization: {_AUTH['text']}"})
        facts["reader"] = name
        facts["settings"] = dict(SETTINGS or {})
        return facts
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def register(actions: dict) -> dict:
    actions.update({name: _tagged(name, fn) for name, fn in ACTIONS.items()})
    return actions
