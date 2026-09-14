"""
Emergency RCA capture actions -- second cut, rebuilt from the first real run.

Same contract as sap_gui.tcode_actions: each takes (session, capture) and
returns a dict of facts. Registered into the shared ACTIONS registry under
rca_* names. Nothing in tcode_actions.py is modified.

WHAT THE FIRST RUN ON PS4 TAUGHT
  SM50   grid found (92 rows) but no memory column: this release does not
         show one. The screen the consultant wants is the ACTIVE processes
         and the LONG-RUNNING ones with their user, not everything.
  ST03N  text search found "Last Minutes' Load" and opened its selection
         screen -- and stopped there, because nothing pressed the confirm
         button. The instance has to be chosen from the tree, not the screen.
  SM12   this release is the "Enqueue Administration" variant; the classic
         SEQG3 fields do not exist. The working IDs are in action_sm12.
  STAD   F8 on tbar[1]/btn[8] did nothing; both shots were the selection
         screen. sendVKey(8) on the window is the reliable F8.
  ST04   HANA. detect_database() saw only the window title "Overview"; the
         database name is in the tree root. And no tree candidate ID matched.

WHAT CHANGED
  Controls are DISCOVERED, not guessed: walk wnd[0]'s children and pick by
  type (GuiShell/GridView, GuiShell/Tree), by the label to the left of a
  field, or by a button's text. A control ID that is only a candidate is
  never the reason a capture fails.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy
from utils.logger import get_logger

log = get_logger(__name__, "application")

# Set by the pipeline before capture: {"instances": {name: resp_ms}, "client": "500"}
RUN_CONTEXT: dict = {}


# ==========================================================================
# Discovery helpers -- the SAP GUI object model, not IDs
# ==========================================================================

def _try(fn, what: str, default=None):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 -- best effort by design
        log.debug(f"RCA optional ({what}): {type(e).__name__}: {e}")
        return default


def _attr(obj, name, default=""):
    return _try(lambda: getattr(obj, name), name, default)


def _walk(obj, depth=0, max_depth=14):
    """Yield every descendant of a GUI object."""
    if obj is None or depth > max_depth:
        return
    yield obj
    if not _attr(obj, "ContainerType", False):
        return
    children = _attr(obj, "Children", None)
    if children is None:
        return
    count = _attr(children, "Count", 0) or 0
    for i in range(int(count)):
        child = _try(lambda: children.Item(i), "child") or _try(lambda: children.ElementAt(i), "child")
        if child is not None:
            yield from _walk(child, depth + 1, max_depth)


def _root(session, root_id="wnd[0]"):
    return _try(lambda: session.findById(root_id), root_id)


def _shells(session, subtype: str) -> list:
    """All GuiShell controls of a subtype: 'GridView', 'Tree', 'Toolbar'..."""
    out = []
    for o in _walk(_root(session)):
        if _attr(o, "Type") == "GuiShell" and str(_attr(o, "SubType")).lower() == subtype.lower():
            out.append(o)
    return out


def _grid(session, exclude_titles: tuple = ("msgtime", "message")):
    """
    The data grid on screen. A screen can hold several ALVs -- DBA Cockpit
    shows a message log under every view, SM12 has a selection block above
    its result -- so pick the one with the most rows whose headers are not
    a message log. Ties go to the first found.
    """
    best, best_rows = None, -1
    for g in _shells(session, "GridView"):
        titles = " ".join(_grid_headers(g)).lower()
        if any(x in titles for x in exclude_titles) and len(_shells(session, "GridView")) > 1:
            continue
        n = int(_attr(g, "RowCount", 0) or 0)
        if n > best_rows:
            best, best_rows = g, n
    return best


def _tree(session):
    t = _shells(session, "Tree")
    return t[0] if t else None


_FIELD_TYPES = {"GuiTextField", "GuiCTextField", "GuiPasswordField"}


def _field_by_label(session, label: str):
    """
    The input field on the same row as, and to the right of, a label whose
    text contains `label`. Uses screen geometry, which is release-stable
    where control IDs are not.
    """
    lab = label.lower()
    labels, fields = [], []
    for o in _walk(_root(session, "wnd[0]/usr")):
        t = _attr(o, "Type")
        if t == "GuiLabel" and lab in str(_attr(o, "Text")).lower():
            labels.append(o)
        elif t in _FIELD_TYPES:
            fields.append(o)
    for L in labels:
        top, left = int(_attr(L, "ScreenTop", 0)), int(_attr(L, "ScreenLeft", 0))
        same_row = [f for f in fields
                    if abs(int(_attr(f, "ScreenTop", -999)) - top) <= 6 and int(_attr(f, "ScreenLeft", 0)) > left]
        if same_row:
            return min(same_row, key=lambda f: int(_attr(f, "ScreenLeft", 0)))
    return None


def _button_by_text(session, *needles: str, root="wnd[0]"):
    ns = [n.lower() for n in needles]
    for o in _walk(_root(session, root)):
        if _attr(o, "Type") == "GuiButton":
            text = f"{_attr(o, 'Text')} {_attr(o, 'Tooltip')}".lower()
            if any(n in text for n in ns):
                return o
    return None


def _press_grid_toolbar(grid, *needles: str) -> str | None:
    """Press an ALV toolbar button by its text/tooltip. Returns the id pressed."""
    ns = [n.lower() for n in needles]
    count = int(_attr(grid, "ToolbarButtonCount", 0) or 0)
    for i in range(count):
        text = f"{_try(lambda: grid.GetToolbarButtonText(i), 't')} {_try(lambda: grid.GetToolbarButtonTooltip(i), 'tt')}".lower()
        if any(n in text for n in ns):
            bid = _try(lambda: grid.GetToolbarButtonId(i), "id")
            if bid and _try(lambda: (grid.PressToolbarButton(bid), True)[-1], f"press {bid}", False):
                return str(bid)
    return None


def _f8(session):
    _try(lambda: session.findById("wnd[0]").sendVKey(8), "F8")
    wait_until_not_busy(session)


def _enter(session):
    _try(lambda: session.findById("wnd[0]").sendVKey(0), "Enter")
    wait_until_not_busy(session)


def _dismiss_popup(session):
    for bid in ("wnd[1]/usr/btnBUTTON_1", "wnd[1]/usr/btnBUTTON_2", "wnd[1]/tbar[0]/btn[0]"):
        if _try(lambda: (session.findById(bid).press(), True)[-1], bid, False):
            wait_until_not_busy(session)
            return


def _title(session) -> str:
    return str(_try(lambda: session.findById("wnd[0]").text, "title", "") or "")


def _statusbar(session) -> str:
    return str(_try(lambda: session.findById("wnd[0]/sbar").text, "sbar", "") or "")


# ---- grids ----------------------------------------------------------------

def _grid_headers(grid) -> dict[str, str]:
    """{display title (lower) : technical column name}"""
    out = {}
    cols = _try(lambda: list(grid.ColumnOrder), "ColumnOrder", []) or []
    for c in cols:
        title = _try(lambda: grid.GetDisplayedColumnTitle(c), "title", "") or ""
        out[str(title).strip().lower()] = str(c)
    return out


def _col(headers: dict, *needles: str) -> str | None:
    for n in needles:
        n = n.lower()
        for title, tech in headers.items():
            if n == title or n in title or n == tech.lower():
                return tech
    return None


def _rows(grid, columns: dict[str, str], limit: int = 500) -> list[dict]:
    """
    Read a grid into dicts. SAP GUI only materialises the rows an ALV has
    rendered; GetCellValue on a row below the visible window returns "".
    So scroll (FirstVisibleRow) a page at a time while reading.
    """
    rows = []
    count = min(int(_attr(grid, "RowCount", 0) or 0), limit)
    page = max(1, int(_attr(grid, "VisibleRowCount", 0) or 0) or 20)
    first0 = int(_attr(grid, "FirstVisibleRow", 0) or 0)
    for r in range(count):
        if r % page == 0 and r >= page:
            _try(lambda r=r: setattr(grid, "FirstVisibleRow", r), "scroll")
        row = {}
        for friendly, tech in columns.items():
            if not tech:
                row[friendly] = ""
                continue
            row[friendly] = str(_try(lambda r=r, tech=tech: grid.GetCellValue(r, tech), tech, "") or "").strip()
        rows.append(row)
    _try(lambda: setattr(grid, "FirstVisibleRow", first0), "scroll back")
    return rows


def _sort_desc(grid, tech: str | None) -> bool:
    if not tech:
        return False
    if not _try(lambda: (grid.selectColumn(tech), True)[-1], f"select {tech}", False):
        return False
    for code in ("&SORT_DOWN", "&SORT_DSC"):
        if _try(lambda: (grid.pressToolbarButton(code), True)[-1], code, False):
            return True
    return bool(_press_grid_toolbar(grid, "descend"))


def _num(s) -> float:
    """SAP-formatted number -> float. '1.326,1' -> 1326.1; '48.885' -> 48885; '0,9' -> 0.9; '2.048,00 MB' -> 2048."""
    m = re.search(r"-?\d[\d.,]*", str(s or ""))
    if not m:
        return 0.0
    txt = m.group(0)
    if "," in txt and "." in txt:
        dec = txt[max(txt.rfind(","), txt.rfind("."))]          # the LAST separator is the decimal one
        ip, fp = txt.rsplit(dec, 1)
        txt = ip.replace(".", "").replace(",", "") + "." + fp
    elif "," in txt:
        ip, fp = txt.rsplit(",", 1)
        txt = (ip.replace(",", "") + "." + fp) if len(fp) != 3 else (ip + fp).replace(",", "")
    elif "." in txt:
        ip, fp = txt.rsplit(".", 1)
        txt = ip.replace(".", "") + fp if len(fp) == 3 else txt.replace(".", "", txt.count(".") - 1)
    try:
        return float(txt)
    except ValueError:
        return 0.0


def _secs(hms: str) -> float:
    """'01:30:43' -> seconds; '51' -> 51."""
    parts = [p for p in re.split(r"[:]", str(hms or "").strip()) if p]
    try:
        vals = [float(p.replace(",", ".")) for p in parts]
    except ValueError:
        return 0.0
    while len(vals) < 3:
        vals.insert(0, 0.0)
    return vals[-3] * 3600 + vals[-2] * 60 + vals[-1]


def _value_near(session, label: str, pattern: str = r"\d") -> str:
    """The text nearest to the right of / below a label whose text contains `label`, matching `pattern`."""
    lab = label.lower()
    cells = [(int(_attr(o, "ScreenTop", 0)), int(_attr(o, "ScreenLeft", 0)), str(_attr(o, "Text", "") or "").strip())
             for o in _walk(_root(session)) if _attr(o, "Type") in ("GuiLabel", "GuiTextField", "GuiCTextField")]
    for top, left, t in cells:
        if lab in t.lower():
            cands = [(abs(tt - top) * 3 + max(0, ll - left), txt) for tt, ll, txt in cells
                     if txt and lab not in txt.lower() and re.search(pattern, txt)
                     and -4 <= tt - top <= 40 and ll >= left - 4]
            if cands:
                return min(cands)[1]
    return ""


# ---- classic ABAP lists (rows of GuiLabel cells) ---------------------------

def _list_rows(session) -> list[list[str]]:
    """A classic list screen as rows of cell texts, grouped by ScreenTop, ordered by ScreenLeft."""
    cells = []
    for o in _walk(_root(session, "wnd[0]/usr")):
        if _attr(o, "Type") == "GuiLabel":
            t = str(_attr(o, "Text", "") or "").strip()
            if t:
                cells.append((int(_attr(o, "ScreenTop", 0)), int(_attr(o, "ScreenLeft", 0)), t))
    rows: dict[int, list] = {}
    for top, left, t in cells:
        rows.setdefault(top, []).append((left, t))
    return [[t for _, t in sorted(v)] for _, v in sorted(rows.items())]


def _list_table(session, header_needles: list[str]) -> list[dict]:
    """
    Turn a classic list into dicts using its header row. The header is the
    first row containing all `header_needles` (case-insensitive); column
    boundaries are the header cells' ScreenLeft; each data cell maps to the
    header whose column starts nearest to its left.
    """
    cells = []
    for o in _walk(_root(session, "wnd[0]/usr")):
        if _attr(o, "Type") == "GuiLabel":
            t = str(_attr(o, "Text", "") or "").strip()
            if t:
                cells.append((int(_attr(o, "ScreenTop", 0)), int(_attr(o, "ScreenLeft", 0)), t))
    by_top: dict[int, list] = {}
    for top, left, t in cells:
        by_top.setdefault(top, []).append((left, t))
    tops = sorted(by_top)
    hdr_top = None
    for top in tops:
        joined = " ".join(t for _, t in by_top[top]).lower()
        if all(n.lower() in joined for n in header_needles):
            hdr_top = top
            break
    if hdr_top is None:
        return []
    headers = sorted(by_top[hdr_top])
    out = []
    for top in tops:
        if top <= hdr_top:
            continue
        row: dict[str, str] = {}
        for left, t in sorted(by_top[top]):
            h = min(headers, key=lambda hh: abs(hh[0] - left))[1].lower()
            row[h] = (row.get(h, "") + " " + t).strip()
        if len(row) >= 3:
            out.append(row)
    return out


def _menu(session, *path: str) -> bool:
    """Choose a menu by the text of each level, e.g. _menu(session, 'extras', 'top capacity', 'current')."""
    node = _root(session, "wnd[0]/mbar")
    for needle in path:
        needle = needle.lower()
        nxt = None
        children = _attr(node, "Children", None)
        for i in range(int(_attr(children, "Count", 0) or 0)):
            c = _try(lambda i=i: children.Item(i), "menu child")
            if c is not None and needle in str(_attr(c, "Text", "")).lower().replace("&", ""):
                nxt = c
                break
        if nxt is None:
            return False
        node = nxt
    ok = _try(lambda: (node.Select(), True)[-1], "menu select", False)
    if ok:
        wait_until_not_busy(session)
    return bool(ok)


def _screen_texts(session) -> list[str]:
    """Every label/text on the main window, for detection by content."""
    out = []
    for o in _walk(_root(session)):
        if _attr(o, "Type") in ("GuiLabel", "GuiTextField", "GuiCTextField", "GuiSimpleContainer", "GuiTitlebar"):
            t = str(_attr(o, "Text", "") or "").strip()
            if t:
                out.append(t)
    return out


# ---- trees ----------------------------------------------------------------

def _node_keys(tree) -> list[str]:
    keys = _try(lambda: tree.GetAllNodeKeys(), "GetAllNodeKeys")
    if keys is None:
        return []
    out = []
    try:
        for k in keys:
            out.append(str(k))
    except TypeError:
        for i in range(int(_attr(keys, "Count", 0) or 0)):
            out.append(str(_try(lambda: keys.Item(i), "key", "")))
    return out


def _node_text(tree, key: str) -> str:
    return str(_try(lambda: tree.GetNodeTextByKey(key), f"text {key}", "") or "")


def _all_node_texts(tree) -> list[str]:
    return [_node_text(tree, k) for k in _node_keys(tree)]


def _expand_all(tree, rounds: int = 2):
    for _ in range(rounds):
        for k in _node_keys(tree):
            _try(lambda k=k: tree.ExpandNode(k), f"expand {k}")


def _find_node(tree, *needles: str, exclude: tuple = (), after: str | None = None) -> str | None:
    """First node whose text contains all needles; optionally only keys after `after` in tree order."""
    ns, ex = [n.lower() for n in needles], [e.lower() for e in exclude]
    keys = _node_keys(tree)
    if after in keys:
        keys = keys[keys.index(after) + 1:]
    for k in keys:
        t = _node_text(tree, k).lower()
        if all(n in t for n in ns) and not any(e in t for e in ex):
            return k
    return None


def _children(tree, parent: str) -> list[str]:
    """Children by GetParent where the API has it; else the keys that follow the parent in order
    until the next node the API reports as a sibling-or-higher (unknown -> next 12 keys)."""
    kids = [k for k in _node_keys(tree) if str(_try(lambda k=k: tree.GetParent(k), "parent", "")) == parent]
    if kids:
        return kids
    keys = _node_keys(tree)
    if parent not in keys:
        return []
    return keys[keys.index(parent) + 1: keys.index(parent) + 13]


def _open_node(session, tree, key: str) -> bool:
    ok = _try(lambda: (setattr(tree, "SelectedNode", key), tree.DoubleClickNode(key), True)[-1],
              f"open {key}", False)
    if ok:
        wait_until_not_busy(session)
        _dismiss_popup(session)
    return bool(ok)


# ==========================================================================
# SM50 / SM66 -- PRIV, long-running, program + user
# ==========================================================================

_SM50 = {"wp": "WP_INDEX", "type": "WP_TYPE_DISP", "pid": "PID", "state": "STATE_DISP",
         "reason": "STATE_INFO_DISP", "cpu": "CPU", "elapsed": "ELAPSED_TIME",
         "program": "WP_PROGRAM", "client": "TENANT_DISP", "user": "USER_NAME",
         "action": "CURRENT_ACTION_DISP"}

_WP_KEEP = ("wp", "type", "state", "reason", "user", "program", "elapsed", "cpu", "action", "instance")


def _wp_summary(rows: list[dict], top: int = 6) -> dict:
    """The lines a consultant reads off SM50/SM66: how many in use, who is running long, anything in PRIV."""
    for r in rows:
        r["elapsed_s"] = _secs(r.get("elapsed"))
        r["cpu_s"] = _secs(r.get("cpu"))
    active = [r for r in rows if r.get("state") and "wait" not in r["state"].lower()]
    priv = [r for r in rows if any("PRIV" in str(r.get(k, "")).upper() for k in ("state", "reason", "action"))]
    by_type: dict[str, list] = {}
    for r in rows:
        by_type.setdefault((r.get("type") or "?").upper()[:3], []).append(r)
    type_line = ", ".join(f"{t}: {sum(1 for x in v if x.get('state') and 'wait' not in x['state'].lower())} of {len(v)} busy"
                          for t, v in sorted(by_type.items()) if t in ("DIA", "BTC", "UPD", "SPO", "UP2"))
    # "long-running" = most time consumed: elapsed on the current step, else accumulated CPU
    ranked = sorted(active, key=lambda r: (r["elapsed_s"] >= 60, r["elapsed_s"], r["cpu_s"]), reverse=True)[:top]
    pick = lambda r: {k: r.get(k, "") for k in _WP_KEEP if r.get(k, "") != ""}  # noqa: E731
    lr_lines = []
    for r in ranked:
        if r["elapsed_s"] < 60 and r["cpu_s"] < 300:
            continue
        where = f" on {r['instance']}" if r.get("instance") else ""
        dur = (f"running {r['elapsed']} s on the current step" if r["elapsed_s"] >= 60 else f"{r.get('cpu')} CPU time accumulated")
        lr_lines.append(f"WP {r.get('wp')} ({r.get('type')}{where}) -- user {r.get('user')}, program {r.get('program')}, {dur}"
                        + (f", {r['action']}" if r.get("action") else "") + (f", status {r['state']} / {r['reason']}" if r.get("reason") else ""))
    focus = [f"Work processes: {len(rows)} configured, {len(active)} in use ({type_line})",
             ("Long-running: " + "; ".join(lr_lines)) if lr_lines else "Long-running: none over 60 s on a step or over 5 min CPU",
             f"PRIV mode: {len(priv)} work process(es)" + (" -- " + "; ".join(f"WP {r.get('wp')} {r.get('user')} {r.get('program')}" for r in priv[:5]) if priv else " (none)")]
    return {"total_processes": len(rows), "active_count": len(active),
            "priv_count": len(priv), "priv": [pick(r) for r in priv[:10]],
            "top_by_cpu": [pick(r) for r in sorted(active, key=lambda r: r["cpu_s"], reverse=True)[:top]],
            "long_running": [pick(r) for r in ranked], "tables": [], "focus": focus}


def rca_sm50(session, capture):
    goto_tcode(session, "SM50")
    wait_until_not_busy(session)
    facts: dict = {"rca": "sm50", "screens": []}
    grid = _grid(session)
    if grid is None:
        capture("as_found"); facts["note"] = "SM50 grid not found"; facts["focus"] = ["SM50 grid not readable"]; return facts
    rows = _rows(grid, _SM50)
    facts.update(_wp_summary(rows))
    pressed = _press_grid_toolbar(grid, "active work")
    if not pressed:
        btn = _button_by_text(session, "active work")
        pressed = "tbar" if btn is not None and _try(lambda: (btn.press(), True)[-1], "active btn", False) else None
    wait_until_not_busy(session)
    grid = _grid(session) or grid
    facts["active_filter"] = "on-screen" if pressed else "not found; full list shown"
    if _sort_desc(grid, "CPU"):
        wait_until_not_busy(session)
        facts["sorted_by"] = "CPU time, highest first"
    capture("active_by_cpu"); facts["screens"].append("active_by_cpu")
    return facts


def rca_sm66(session, capture):
    goto_tcode(session, "SM66")
    wait_until_not_busy(session)
    facts: dict = {"rca": "sm66", "screens": []}
    grid = _grid(session)
    if grid is None:
        # Older SM66 is a table control, not an ALV: the routine action knows it.
        from sap_gui.tcode_actions import ACTIONS
        base = _try(lambda: ACTIONS["sm66"](session, capture), "sm66 base", {}) or {}
        facts.update({k: v for k, v in base.items() if k in ("visible_process_rows", "running_processes")})
        facts["focus"] = [f"Running processes: {base.get('running_processes', '?')} (table control; see screenshot)"]
        return facts
    h = _grid_headers(grid)
    cols = {"instance": _col(h, "as instance", "instance"), "wp": _col(h, "numb", "no."),
            "type": _col(h, "type"), "state": _col(h, "wp status", "status"), "reason": _col(h, "on hold"),
            "cpu": _col(h, "cpu"), "elapsed": _col(h, "time"), "program": _col(h, "program"),
            "user": _col(h, "user"), "client": _col(h, "clie"), "action": _col(h, "action")}
    rows = _rows(grid, cols)
    facts.update(_wp_summary(rows))
    facts["instances"] = sorted({r.get("instance") for r in rows if r.get("instance")})
    # SM66 opens on all work processes: switch to active ones, then sort by CPU.
    _press_grid_toolbar(grid, "active work") or (lambda b: b is not None and _try(lambda: (b.press(), True)[-1], "active", False))(_button_by_text(session, "active work"))
    wait_until_not_busy(session)
    grid = _grid(session) or grid
    if _sort_desc(grid, cols["cpu"]):
        wait_until_not_busy(session); facts["sorted_by"] = "CPU time, highest first"
    capture("all_instances_active_by_cpu"); facts["screens"].append("all_instances_active_by_cpu")
    return facts


# ==========================================================================
# ST03N -- time breakdown, task types, users, transactions
# ==========================================================================

_ST03N = {"task_type": "TASKTYPE", "steps": "DIASTEPCNT", "avg_resp_ms": "MRESPTIME",
          "avg_proc_ms": "MPROCTI", "avg_cpu_ms": "MCPUTI", "avg_db_ms": "MDBTI", "avg_wait_ms": "MWAITTI",
          "avg_roll_in_ms": "MROLLINTI", "avg_roll_wait_ms": "MROLWAITI", "avg_load_ms": "MLOADGENTI",
          "avg_lock_ms": "MLOCKTI", "avg_rfc_ms": "MCPICTI", "avg_gui_ms": "FGUIMT"}


def _task_rows(grid) -> list[dict]:
    rows = _rows(grid, _ST03N, limit=60)
    for r in rows:
        for k in list(r):
            if k.startswith("avg_") or k == "steps":
                r[k + "_n"] = _num(r[k])
    return rows


def _breakdown(d: dict) -> dict:
    """Where the dialog time goes, with the consultant's thresholds applied."""
    resp = d.get("avg_resp_ms_n", 0.0) or 0.0
    db, cpu, wait = d.get("avg_db_ms_n", 0.0), d.get("avg_cpu_ms_n", 0.0), d.get("avg_wait_ms_n", 0.0)
    roll = (d.get("avg_roll_in_ms_n", 0.0) or 0.0) + (d.get("avg_roll_wait_ms_n", 0.0) or 0.0)
    lock, gui, rfc = d.get("avg_lock_ms_n", 0.0), d.get("avg_gui_ms_n", 0.0), d.get("avg_rfc_ms_n", 0.0)
    share = lambda x: (100.0 * x / resp) if resp else 0.0  # noqa: E731
    verdicts = []
    if resp and share(db) > 40:
        verdicts.append(f"DB time {db:.0f} ms is {share(db):.0f}% of response -- bottleneck is SQL-side (indexes, locks, scans)")
    if resp and share(cpu) > 40:
        verdicts.append(f"CPU time {cpu:.0f} ms is {share(cpu):.0f}% of response -- heavy ABAP or CPU-starved host")
    if wait > 50:
        verdicts.append(f"Wait time {wait:.0f} ms > 50 ms -- work processes exhausted, requests queue (see SM50)")
    if roll > 50:
        verdicts.append(f"Roll in/wait {roll:.0f} ms -- memory swapping / roll area (ztta/roll_extension)")
    if lock > 50:
        verdicts.append(f"Enqueue time {lock:.0f} ms > 50 ms -- lock waits (see SM12)")
    if gui and share(gui) > 25:
        verdicts.append(f"GUI time {gui:.0f} ms is {share(gui):.0f}% -- network/frontend, not the server")
    return {"resp_ms": resp, "db_ms": db, "cpu_ms": cpu, "wait_ms": wait, "roll_ms": roll, "lock_ms": lock,
            "gui_ms": gui, "rfc_ms": rfc, "db_pct": round(share(db)), "cpu_pct": round(share(cpu)),
            "verdicts": verdicts or ([f"Dialog {resp:.0f} ms: no single component over threshold"] if resp else ["no dialog steps in window"])}


def _task_compare(rows: list[dict]) -> dict:
    by = {r["task_type"].upper(): r for r in rows if r.get("task_type")}
    d, rfc, upd, btc = by.get("DIALOG"), by.get("RFC"), by.get("UPDATE"), by.get("BACKGROUND")
    out = {t: {"steps": by[t]["steps_n"], "avg_resp_ms": by[t]["avg_resp_ms_n"]} for t in ("DIALOG", "RFC", "UPDATE", "BACKGROUND") if t in by}
    verdict = []
    if d and rfc:
        if d["avg_resp_ms_n"] > 1000 and rfc["avg_resp_ms_n"] < 500:
            verdict.append("Dialog slow while RFC normal -- frontend users affected, not integrations")
        if rfc["avg_resp_ms_n"] > 2000 and rfc["steps_n"] > d["steps_n"]:
            verdict.append(f"RFC avg {rfc['avg_resp_ms_n']:.0f} ms over {rfc['steps_n']:.0f} steps -- system-to-system calls are loading the instance")
    if upd and upd["avg_resp_ms_n"] > 1000:
        verdict.append(f"UPDATE avg {upd['avg_resp_ms_n']:.0f} ms -- posting is slow, check SM13/DB")
    out["verdicts"] = verdict or ["task types: no abnormal split"]
    return out


def _busiest_instance(tree) -> str | None:
    inst = (RUN_CONTEXT.get("instances") or {})
    ranked = sorted(((v, k) for k, v in inst.items() if isinstance(v, (int, float))), reverse=True)
    texts = _all_node_texts(tree)
    for _, n in ranked:
        if any(n.lower() in t.lower() for t in texts):
            return n
    for t in texts:
        if re.search(r"\w+_\w{3}_\d{2}", t):
            return t.strip()
    return None


def _ranked_grid(session, capture, tag: str, sort_needles: tuple, keep: dict, top: int = 10) -> list[dict]:
    """Open state already reached: sort the grid desc on the first matching column, capture, return top rows."""
    grid = _grid(session)
    if grid is None:
        return []
    h = _grid_headers(grid)
    sort_col = _col(h, *sort_needles)
    if _sort_desc(grid, sort_col):
        wait_until_not_busy(session)
    capture(tag)
    cols = {k: _col(h, *v) for k, v in keep.items()}
    rows = _rows(grid, cols, limit=top)
    for r in rows:
        for k in list(r):
            if k.endswith("_ms") or k in ("steps",):
                r[k + "_n"] = _num(r[k])
    return rows


def rca_st03n(session, capture):
    goto_tcode(session, "ST03N")
    wait_until_not_busy(session)
    facts: dict = RUN_CONTEXT.get("_partial") if isinstance(RUN_CONTEXT.get("_partial"), dict) else {}
    facts.update({"rca": "st03n", "screens": [], "focus": []})
    tree = _tree(session)
    if tree is None:
        capture("as_found"); facts["note"] = "ST03N tree not found"; facts["focus"] = ["ST03N tree not readable"]; return facts
    _expand_all(tree, rounds=1)
    inst = _busiest_instance(tree)
    facts["instance"] = inst

    # Detailed Analysis -> Last Minutes' Load -> instance -> last 15 minutes
    da = _find_node(tree, "detailed analysis")
    if da: _try(lambda: tree.ExpandNode(da), "expand DA")
    lm = _find_node(tree, "last minute")
    opened = False
    if lm:
        _try(lambda: tree.ExpandNode(lm), "expand LM")
        target = None
        if inst:
            for k in _children(tree, lm):
                if inst.lower() in _node_text(tree, k).lower():
                    target = k; break
            target = target or _find_node(tree, inst, after=lm)
        target = target or _find_node(tree, "total", after=lm) or lm
        if _open_node(session, tree, target):
            now = datetime.now()
            f_from, f_to = _field_by_label(session, "from"), _field_by_label(session, "to")
            if f_from is not None: _try(lambda: setattr(f_from, "Text", (now - timedelta(minutes=15)).strftime("%H:%M:%S")), "from")
            if f_to is not None: _try(lambda: setattr(f_to, "Text", now.strftime("%H:%M:%S")), "to")
            facts["window"] = f"{(now - timedelta(minutes=15)):%H:%M}-{now:%H:%M}"
            btn = _button_by_text(session, "continue", "execute", "enter", root="wnd[0]/usr") \
                or next((o for o in _walk(_root(session, "wnd[0]/usr")) if _attr(o, "Type") == "GuiButton"), None)
            if btn is not None and _try(lambda: (btn.press(), True)[-1], "confirm", False):
                wait_until_not_busy(session)
            else:
                _enter(session)
            _dismiss_popup(session)
            opened = True
    if not opened:
        key = _find_node(tree, inst) if inst else _find_node(tree, "total")
        opened = bool(key and _open_node(session, tree, key))
        facts["window"] = "today (last-minutes load not reachable)"
    if not opened:
        capture("as_found"); facts["focus"] = ["ST03N workload not opened"]; return facts

    # 1. Workload overview: time breakdown + task-type comparison
    capture("workload_time_breakdown"); facts["screens"].append("workload_time_breakdown")
    grid = _grid(session)
    rows = _task_rows(grid) if grid is not None else []
    d = next((r for r in rows if r.get("task_type", "").upper() == "DIALOG"), None)
    if d:
        facts["dialog"] = {k: v for k, v in d.items()}
        facts["breakdown"] = _breakdown(d)
        facts["dialog_avg_resp_ms"] = d["avg_resp_ms_n"]; facts["dialog_steps"] = d["steps_n"]
        facts["focus"].append(f"{inst} {facts['window']}: dialog avg {d['avg_resp_ms_n']:.0f} ms over {d['steps_n']:.0f} steps "
                              f"(DB {facts['breakdown']['db_pct']}%, CPU {facts['breakdown']['cpu_pct']}%, wait {facts['breakdown']['wait_ms']:.0f} ms, "
                              f"roll {facts['breakdown']['roll_ms']:.0f} ms, enqueue {facts['breakdown']['lock_ms']:.0f} ms)")
        facts["focus"] += facts["breakdown"]["verdicts"]
    if rows:
        facts["task_types"] = _task_compare(rows)
        facts["focus"] += facts["task_types"]["verdicts"]

    # 2. Analysis Views -> User Profile (culprit users), sorted by response time
    tree = _tree(session) or tree
    av = _find_node(tree, "analysis views")
    if av: _try(lambda: tree.ExpandNode(av), "expand AV")
    uk = _find_node(tree, "user", "profile", after=av) or _find_node(tree, "user and settlement", after=av)
    if uk and _open_node(session, tree, uk):
        sub = _find_node(_tree(session) or tree, "user profile", after=uk)
        if sub: _open_node(session, _tree(session) or tree, sub)
        users = _ranked_grid(session, capture, "users_by_response", ("total resp", "response time", "resp"),
                             {"user": ("user",), "steps": ("steps", "# steps"), "total_resp_ms": ("total resp", "response time"),
                              "avg_resp_ms": ("avg. resp", "avg resp", "ø resp"), "db_ms": ("db time", "total db"), "cpu_ms": ("cpu",)})
        if users:
            facts["screens"].append("users_by_response"); facts["top_users"] = users
            facts["focus"].append("Top users by response time: " + "; ".join(
                f"{u.get('user')} {u.get('total_resp_ms')} ms/{u.get('steps')} steps" for u in users[:5]))
    else:
        facts.setdefault("missing", []).append("user_profile")

    # 3. Analysis Views -> Transaction Profile (culprit reports), sorted by response time
    tree = _tree(session) or tree
    tk = _find_node(tree, "transaction profile", after=av)
    if tk and _open_node(session, tree, tk):
        tx = _ranked_grid(session, capture, "transactions_by_response", ("total resp", "response time", "resp"),
                          {"tcode": ("tcode", "transaction", "report"), "steps": ("steps",), "total_resp_ms": ("total resp", "response time"),
                           "avg_resp_ms": ("avg. resp", "avg resp", "ø resp"), "db_ms": ("db time", "total db"), "cpu_ms": ("cpu",)})
        if tx:
            facts["screens"].append("transactions_by_response"); facts["top_transactions"] = tx
            custom = [t for t in tx if str(t.get("tcode", ""))[:1].upper() in ("Z", "Y")]
            facts["focus"].append("Top transactions/reports: " + "; ".join(
                f"{t.get('tcode')} {t.get('total_resp_ms')} ms/{t.get('steps')} steps" for t in tx[:5])
                + (f" -- custom code in top list: {', '.join(str(t.get('tcode')) for t in custom[:3])}" if custom else ""))
    else:
        facts.setdefault("missing", []).append("transaction_profile")
    return facts


# ==========================================================================
# SM12 -- Enqueue Administration + Top Capacity Used
# ==========================================================================

_ENQ = "wnd[0]/usr/subAREA_TOP:RS_ENQ_ADMIN:0111/"


def _sm12_search(session, client: str, user: str) -> dict:
    f_client = _try(lambda: session.findById(_ENQ + "ctxtENQ_LOCK_FILTER-CLIENT"), "enq client") or _field_by_label(session, "client")
    f_user = _try(lambda: session.findById(_ENQ + "ctxtENQ_LOCK_FILTER-USERNAME"), "enq user") or _field_by_label(session, "user name")
    f_limit = _try(lambda: session.findById(_ENQ + "txtENQ_LOCK_FILTER-LIMIT"), "enq limit") or _field_by_label(session, "number of locks")
    if f_user is None:
        return {"variant": "unknown", "note": "Enqueue Administration fields not found"}
    if f_client is not None: _try(lambda: setattr(f_client, "Text", client), "client")
    _try(lambda: setattr(f_user, "Text", user), "user")
    if f_limit is not None: _try(lambda: setattr(f_limit, "Text", ""), "limit")     # blank = no cap, as action_sm12 does
    # Search: the LOAD button, else the button labelled Search, else F8, else Enter.
    btn = _try(lambda: session.findById(_ENQ + "btnLOAD"), "btnLOAD") or _button_by_text(session, "search", "load")
    pressed = btn is not None and _try(lambda: (btn.press(), True)[-1], "search", False)
    wait_until_not_busy(session)
    if not pressed or _grid(session) is None:
        _f8(session)
    if _grid(session) is None:
        _enter(session)
    out: dict = {"variant": "enqueue_administration", "client": client, "user": user, "status": _statusbar(session)}
    # "Lock Table (141)" on screen / "141 locks have been displayed" in the status bar: the count even if the grid read fails
    m = re.search(r"lock table\s*\((\d+)\)", " ".join(_screen_texts(session)), re.I) or re.search(r"(\d+)\s+locks?\s+have been displayed", out["status"], re.I)
    if m:
        out["lock_count_on_screen"] = int(m.group(1))
    grid = _grid(session)
    if grid is None or int(_attr(grid, "RowCount", 0) or 0) == 0:
        out["lock_count"] = out.get("lock_count_on_screen")
        out["note"] = "lock grid not readable as ALV; count taken from screen title"
        return out
    h = _grid_headers(grid)
    cols = {"client": _col(h, "client", "cli"), "user": _col(h, "user"), "table": _col(h, "table"),
            "arg": _col(h, "lock argument", "argument"), "mode": _col(h, "mode"), "time": _col(h, "time"),
            "date": _col(h, "date"), "owner": _col(h, "owner", "transaction", "tcode")}
    rows = _rows(grid, cols)
    out["lock_count"] = len(rows)
    by_user: dict[str, int] = {}; by_key: dict[str, set] = {}; stale = []
    now = datetime.now()
    for r in rows:
        u = r.get("user") or "?"
        by_user[u] = by_user.get(u, 0) + 1
        by_key.setdefault(f"{r.get('table','')}|{r.get('arg','')}", set()).add(u)
        age = _lock_age_min(r.get("date"), r.get("time"), now)
        if age is not None and age >= 60:
            stale.append({"user": u, "table": r.get("table"), "argument": r.get("arg"), "age_min": age, "owner": r.get("owner")})
    out["users"] = sorted(by_user); out["user_count"] = len(by_user)
    out["top_users"] = [{"user": u, "locks": n} for u, n in sorted(by_user.items(), key=lambda kv: kv[1], reverse=True)[:10]]
    contended = [{"table": k.split("|")[0], "argument": k.split("|", 1)[1], "users": sorted(v)} for k, v in by_key.items() if len(v) > 1]
    out["contended_keys"], out["contended_key_count"] = contended[:20], len(contended)
    out["stale_locks"] = sorted(stale, key=lambda x: x["age_min"], reverse=True)[:10]; out["stale_count"] = len(stale)
    return out


def _lock_age_min(date_s, time_s, now: datetime):
    for fmt in ("%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return round((now - datetime.strptime(f"{date_s} {time_s}".strip(), fmt)).total_seconds() / 60)
        except Exception:
            continue
    try:
        t = datetime.strptime(str(time_s), "%H:%M:%S").replace(year=now.year, month=now.month, day=now.day)
        return round((now - t).total_seconds() / 60) if t <= now else None
    except Exception:
        return None


def rca_sm12(session, capture):
    goto_tcode(session, "SM12"); wait_until_not_busy(session)
    facts: dict = {"rca": "sm12", "screens": [], "focus": []}
    login_client = str(RUN_CONTEXT.get("client") or "")

    a = _sm12_search(session, "*", "*")
    capture("all_clients_all_users"); facts["screens"].append("all_clients_all_users"); facts["all_clients"] = a
    goto_tcode(session, "SM12"); wait_until_not_busy(session)
    b = _sm12_search(session, login_client or "*", "*")
    capture(f"client_{login_client or 'all'}_all_users"); facts["screens"].append(f"client_{login_client or 'all'}_all_users"); facts["login_client"] = b

    # Extras -> Top Capacity Used -> Current: who holds the most, straight from the enqueue server.
    goto_tcode(session, "SM12"); wait_until_not_busy(session)
    if _menu(session, "extras", "top capacity", "current") or _menu(session, "extras", "top capacity"):
        capture("top_capacity_current"); facts["screens"].append("top_capacity_current")
        tc = _list_table(session, ["user"]) or []
        if not tc:
            g = _grid(session)
            if g is not None:
                h = _grid_headers(g)
                tc = _rows(g, {"user": _col(h, "user"), "locks": _col(h, "lock", "number", "count")}, limit=20)
        facts["top_capacity"] = tc[:10]
    else:
        facts.setdefault("missing", []).append("top_capacity_used")

    src = a if a.get("lock_count") is not None else b
    for k in ("lock_count", "user_count", "top_users", "contended_keys", "contended_key_count", "stale_locks", "stale_count"):
        facts[k] = src.get(k)
    facts["lock_count_login_client"] = b.get("lock_count")

    # RFC view of the same table (from the live read taken seconds earlier)
    rfc_rows = {c.get("label", ""): c for c in (RUN_CONTEXT.get("checks") or []) if c.get("tcode") == "SM12"}
    rfc = {"lock_count": (rfc_rows.get("Lock entries") or {}).get("value"),
           "most_locks_per_user": (rfc_rows.get("Most locks per user") or {}).get("value"),
           "most_locks_detail": (rfc_rows.get("Most locks per user") or {}).get("detail", ""),
           "users_with_many_locks": (rfc_rows.get("Users with many locks") or {}).get("value"),
           "users_with_many_locks_detail": (rfc_rows.get("Users with many locks") or {}).get("detail", ""),
           "oldest_lock_minutes": (rfc_rows.get("Oldest lock age") or {}).get("value")}
    facts["rfc"] = rfc

    facts["tables"] = []
    lc = facts.get("lock_count")
    top_users = [u for u in (facts.get("top_users") or []) if u.get("user") and u["user"] != "?"]
    unknown = sum(u["locks"] for u in (facts.get("top_users") or []) if u.get("user") == "?")
    focus = [f"Locks in all clients: {lc if lc is not None else '?'}" + (f" (RFC read a minute earlier: {rfc['lock_count']})" if rfc.get("lock_count") is not None else ""),
             f"Locks in client {login_client or '*'}: {b.get('lock_count', '?')}"]
    if top_users:
        focus.append("Same user, many locks: " + "; ".join(f"{u['user']} holds {u['locks']}" for u in top_users[:5])
                     + (f" (plus {unknown} rows whose user was not readable)" if unknown else ""))
    elif rfc.get("most_locks_detail"):
        focus.append(f"Same user, many locks (from RFC): {rfc['most_locks_detail']}")
    else:
        focus.append("Same user, many locks: not readable")
    if facts.get("contended_key_count"):
        k = facts["contended_keys"][0]
        focus.append(f"Many users, same lock: {facts['contended_key_count']} lock key(s) held by more than one user -- "
                     f"{len(k['users'])} users on {k['table']} {k['argument']} ({', '.join(k['users'])})")
    else:
        focus.append("Many users, same lock: none -- no table + lock argument is held by more than one user")
    if facts.get("stale_count"):
        s0 = facts["stale_locks"][0]
        focus.append(f"Oldest lock: {s0['age_min']} min, {s0['user']} on {s0['table']} ({facts['stale_count']} older than 60 min) -- check SM13 / hung sessions")
    facts["focus"] = focus
    return facts


# ==========================================================================
# STAD -- last 30 minutes, classic list read as labels
# ==========================================================================

def rca_stad(session, capture, lookback_minutes: int = 30, min_response_ms: int = 0):
    goto_tcode(session, "STAD"); wait_until_not_busy(session)
    now = datetime.now(); start = now - timedelta(minutes=lookback_minutes)
    facts: dict = {"rca": "stad", "lookback_minutes": lookback_minutes, "screens": [], "focus": []}
    f_time, f_len, f_resp = _field_by_label(session, "time"), _field_by_label(session, "length"), _field_by_label(session, "resp. time")
    if f_time is not None: _try(lambda: setattr(f_time, "Text", start.strftime("%H:%M:%S")), "time")
    if f_len is not None: _try(lambda: setattr(f_len, "Text", f"{lookback_minutes // 60:02d}:{lookback_minutes % 60:02d}:00"), "length")
    if min_response_ms and f_resp is not None: _try(lambda: setattr(f_resp, "Text", str(min_response_ms)), "resp")
    _f8(session); _dismiss_popup(session)
    capture("records"); facts["screens"].append("records")
    facts["status"] = _statusbar(session)

    rows = []
    grid = _grid(session)
    if grid is not None:
        h = _grid_headers(grid)
        rows = _rows(grid, {"user": _col(h, "user"), "tcode": _col(h, "transaction", "tcode"), "program": _col(h, "program", "report"),
                            "resp": _col(h, "response"), "wait": _col(h, "wait"), "cpu": _col(h, "cpu"), "db": _col(h, "db req", "db time"),
                            "lock": _col(h, "enqueue", "lock"), "roll": _col(h, "roll"), "memory": _col(h, "memory")}, limit=2000)
        facts["source"] = "alv"
    else:
        lst = _list_table(session, ["user", "response"])
        facts["source"] = "classic list"
        for r in lst:
            g = lambda *ns: next((v for k, v in r.items() if any(n in k for n in ns)), "")  # noqa: E731
            rows.append({"user": g("user"), "tcode": g("transaction"), "program": g("program"), "resp": g("response"),
                         "wait": g("wait"), "cpu": g("cpu"), "db": g("db req", "db"), "lock": g("enqueue", "lock"),
                         "roll": g("roll"), "memory": g("memo")})
    rows = [r for r in rows if r.get("user")]
    facts["record_count"] = len(rows)
    if not rows:
        facts["note"] = "no statistical records readable; see screenshot"
        facts["focus"].append("STAD: no records readable in the window")
        return facts

    for r in rows:
        for k in ("resp", "wait", "cpu", "db", "lock", "roll", "memory"):
            r[k + "_n"] = _num(r.get(k))
    by_user: dict[str, dict] = {}; by_prog: dict[str, dict] = {}
    for r in rows:
        for key, bucket, name in (("user", by_user, r["user"]), ("program", by_prog, r.get("program") or r.get("tcode") or "?")):
            e = bucket.setdefault(name, {key: name, "steps": 0, "total_resp_ms": 0.0, "worst_ms": 0.0, "db_ms": 0.0,
                                         "cpu_ms": 0.0, "wait_ms": 0.0, "lock_ms": 0.0, "max_memory": 0.0, "programs": set()})
            e["steps"] += 1; e["total_resp_ms"] += r["resp_n"]; e["worst_ms"] = max(e["worst_ms"], r["resp_n"])
            e["db_ms"] += r["db_n"]; e["cpu_ms"] += r["cpu_n"]; e["wait_ms"] = max(e["wait_ms"], r["wait_n"])
            e["lock_ms"] = max(e["lock_ms"], r["lock_n"]); e["max_memory"] = max(e["max_memory"], r["memory_n"])
            if r.get("program"): e["programs"].add(r["program"])
    fin = lambda b: [{**e, "programs": sorted(e["programs"])[:4]} for e in b.values()]  # noqa: E731
    facts["top_by_response"] = sorted(fin(by_user), key=lambda e: e["total_resp_ms"], reverse=True)[:10]
    facts["top_by_memory"] = sorted(fin(by_user), key=lambda e: e["max_memory"], reverse=True)[:10] if any(r["memory_n"] for r in rows) else []
    facts["top_programs"] = sorted(fin(by_prog), key=lambda e: e["total_resp_ms"], reverse=True)[:10]
    total_resp = sum(r["resp_n"] for r in rows) or 1.0
    facts["components"] = {"db_pct": round(100 * sum(r["db_n"] for r in rows) / total_resp),
                           "cpu_pct": round(100 * sum(r["cpu_n"] for r in rows) / total_resp),
                           "max_wait_ms": max(r["wait_n"] for r in rows), "max_lock_ms": max(r["lock_n"] for r in rows)}
    top = facts["top_by_response"][:8]
    facts["tables"] = [{"title": f"Users by total response time, last {lookback_minutes} minutes",
                        "columns": ["User", "Program", "Total response", "DB time", "CPU time", "Peak memory"],
                        "rows": [[u["user"], ", ".join(u["programs"]), _ms_h(u["total_resp_ms"]), _ms_h(u["db_ms"]), _ms_h(u["cpu_ms"]),
                                  (f"{u['max_memory']:,.0f} KB" if u["max_memory"] else "")] for u in top]}]
    c = facts["components"]
    lead = f"In the last {lookback_minutes} minutes {len(rows)} steps were recorded. "
    lead += f"{c['db_pct']}% of all response time was spent in the database and {c['cpu_pct']}% in CPU"
    lead += ("; no step waited for a work process" if c["max_wait_ms"] <= 50 else f"; the longest wait for a work process was {c['max_wait_ms']:.0f} ms")
    lead += ("." if c["max_lock_ms"] <= 50 else f", and the longest enqueue wait was {c['max_lock_ms']:.0f} ms.")
    facts["focus"] = [lead]
    if top:
        u0 = top[0]
        facts["focus"].append(f"The heaviest user was {u0['user']}: {u0['steps']} step(s) of {', '.join(u0['programs'])} totalling {_ms_h(u0['total_resp_ms'])}"
                              f" (worst single step {_ms_h(u0['worst_ms'])}), of which {_ms_h(u0['db_ms'])} database and {_ms_h(u0['cpu_ms'])} CPU.")
        others = [f"{u['user']} ({', '.join(u['programs'])}) {_ms_h(u['total_resp_ms'])}" for u in top[1:4]]
        if others:
            facts["focus"].append("Next: " + "; ".join(others) + ".")
    if facts["top_by_memory"]:
        m = facts["top_by_memory"][0]
        facts["focus"].append(f"Peak memory in one step: {m['user']} running {', '.join(m['programs'])}, {m['max_memory']:,.0f} KB.")
    if c["max_wait_ms"] > 50: facts["focus"].append("Wait time over 50 ms means work processes were exhausted -- see SM50.")
    if c["max_lock_ms"] > 50: facts["focus"].append("Enqueue time over 50 ms means lock waits -- see SM12.")
    return facts


def _ms_h(ms: float) -> str:
    """725995 -> '12 min 6 s'; 6736 -> '6.7 s'; 402 -> '402 ms'."""
    ms = float(ms or 0)
    if ms >= 60000:
        return f"{int(ms // 60000)} min {int((ms % 60000) // 1000)} s"
    if ms >= 1000:
        return f"{ms / 1000:.1f} s"
    return f"{ms:.0f} ms"


# ==========================================================================
# ST04 -- DB-aware: overview/alerts, memory, expensive statements, active
# ==========================================================================

def detect_database(title: str, status: str = "", texts: list[str] | None = None) -> str:
    t = " ".join([title or "", status or "", *(texts or [])]).lower()
    if "hana" in t: return "hana"
    if " ase" in f" {t}" or "sybase" in t: return "ase"
    if "oracle" in t: return "oracle"
    if "sql server" in t or "mssql" in t: return "mssql"
    if "db2" in t or "db6" in t: return "db2"
    if "maxdb" in t: return "maxdb"
    return "generic"


# (screen name, tree-node needles, needs Execute first)
_ST04_SCREENS = {
    "hana":   [("expensive_statements", ("expensive",), True), ("active_statements", ("active", "statement"), False),
               ("blocked_transactions", ("blocked",), False), ("alerts", ("alert",), False)],
    "ase":    [("expensive_statements", ("expensive",), False), ("active_statements", ("active", "statement"), False), ("alerts", ("alert",), False)],
    "oracle": [("expensive_statements", ("shared", "cursor"), False), ("active_statements", ("session",), False), ("alerts", ("alert",), False)],
    "mssql":  [("expensive_statements", ("expensive",), False), ("active_statements", ("active",), False), ("alerts", ("alert",), False)],
    "db2":    [("expensive_statements", ("sql", "cache"), False), ("active_statements", ("application",), False), ("alerts", ("alert",), False)],
}
_ST04_OVERVIEW = [("memory_used_of_limit", "Allocation Limit"), ("memory_used_of_total", "Memory Used/Total"),
                  ("cpu_usage", "CPU Usage"), ("data_volume", "Data Volume"), ("log_volume", "Log Volume"), ("trace_files", "Trace Files")]


def _read_grid_table(grid, top: int = 10, prefer: tuple = ()) -> dict | None:
    """A grid as {columns, rows}, preferring columns whose titles match `prefer`, else the first 8."""
    order = _try(lambda: list(grid.ColumnOrder), "cols", []) or []
    titles = {c: str(_try(lambda c=c: grid.GetDisplayedColumnTitle(c), "t", c) or c) for c in order}
    if prefer:
        chosen = [c for c in order if any(p in titles[c].lower() for p in prefer)]
        chosen += [c for c in order if c not in chosen][: max(0, 8 - len(chosen))]
    else:
        chosen = order[:8]
    n = min(int(_attr(grid, "RowCount", 0) or 0), top)
    if not chosen or n == 0:
        return None
    return {"columns": [titles[c] for c in chosen],
            "rows": [[str(_try(lambda r=r, c=c: grid.GetCellValue(r, c), "v", "") or "").strip() for c in chosen] for r in range(n)]}


def rca_st04(session, capture, db_hint: str = "detect"):
    goto_tcode(session, "ST04"); wait_until_not_busy(session); _dismiss_popup(session)
    tree = _tree(session)
    texts = _screen_texts(session) + (_all_node_texts(tree) if tree is not None else [])
    db = db_hint if db_hint and db_hint != "detect" else detect_database(_title(session), _statusbar(session), texts)
    facts: dict = {"rca": "st04", "database": db, "title": _title(session), "screens": ["overview"], "focus": [], "tables": []}
    capture("overview")

    # Overview: memory / CPU / disk, by the label next to each figure
    ov = {}
    for key, label in _ST04_OVERVIEW:
        v = _value_near(session, label, r"\d")
        if v:
            ov[key] = v
    facts["overview"] = ov
    alerts_line = next((t for t in texts if re.search(r"\d+ errors?, \d+ high", t.lower()) or "no alert" in t.lower()), "")
    facts["overview_alerts"] = alerts_line
    if ov:
        facts["tables"].append({"title": "Database overview", "columns": ["Measure", "Value"],
                                "rows": [[label, ov[key]] for key, label in _ST04_OVERVIEW if key in ov]})
    facts["focus"].append("Memory: " + (f"{ov.get('memory_used_of_limit')} of allocation limit" if ov.get("memory_used_of_limit") else "not read")
                          + (f"; CPU {ov['cpu_usage']}" if ov.get("cpu_usage") else "")
                          + (f"; data volume {ov['data_volume']}" if ov.get("data_volume") else "")
                          + (f"; log volume {ov['log_volume']}" if ov.get("log_volume") else ""))
    if alerts_line:
        facts["focus"].append(f"Alerts on overview: {alerts_line}")
    if tree is None:
        facts["note"] = "DBA Cockpit tree not found; overview only"; return facts
    _expand_all(tree, rounds=2)

    for name, needles, needs_execute in _ST04_SCREENS.get(db, []):
        tree = _tree(session) or tree
        key = _find_node(tree, *needles)
        if not (key and _open_node(session, tree, key)):
            facts.setdefault("missing", []).append(name); continue
        if needs_execute:
            # Expensive Statements / SQL Plan Cache open on a selection block; the list is empty until Execute.
            f_hits = _field_by_label(session, "max. no. of hits") or _field_by_label(session, "no. of hits")
            if f_hits is not None: _try(lambda: setattr(f_hits, "Text", "20"), "hits")
            btn = _button_by_text(session, "execute", root="wnd[0]")
            if btn is not None and _try(lambda: (btn.press(), True)[-1], "execute", False):
                wait_until_not_busy(session)
            else:
                _f8(session)
        grid = _grid(session)
        prefer = {"expensive_statements": ("statement", "duration", "records", "user", "object"),
                  "sql_plan_cache": ("statement", "total execution", "execution count", "avg", "user"),
                  "active_statements": ("statement", "duration", "user", "status", "blocked"),
                  "blocked_transactions": ("blocked", "blocking", "user", "duration", "statement"),
                  "alerts": ("priority", "timestamp", "category", "description", "user action")}[name]
        if grid is not None and name != "alerts":
            h = _grid_headers(grid)
            _sort_desc(grid, _col(h, "total execution", "duration", "elapsed", "execution time"))
            wait_until_not_busy(session)
        capture(name); facts["screens"].append(name)
        tbl = _read_grid_table(grid, top=8, prefer=prefer) if grid is not None else None
        label = name.replace("_", " ")
        if name == "alerts":
            head = next((t for t in _screen_texts(session) if re.search(r"current alerts:", t, re.I)), "")
            facts["alerts_header"] = head
            if tbl:
                pcol = next((i for i, c in enumerate(tbl["columns"]) if "priority" in c.lower()), None)
                by_p: dict[str, int] = {}
                for r in tbl["rows"]:
                    pr = (r[pcol] if pcol is not None else "?") or "?"
                    by_p[pr] = by_p.get(pr, 0) + 1
                facts["alerts"] = tbl["rows"]
                facts["tables"].append({"title": "Alerts (with priority)" + (f" -- {head}" if head else ""), "columns": tbl["columns"], "rows": tbl["rows"]})
                facts["focus"].append(("Alerts: " + head + "; " if head else "Alerts: ") + "listed: " + ", ".join(f"{k}: {v}" for k, v in by_p.items()))
            else:
                facts["focus"].append("Alerts: " + (head or "none listed"))
        elif tbl:
            facts[name] = tbl["rows"]
            cols_l = [c.lower() for c in tbl["columns"]]
            def cell(row, *needles):
                for n in needles:
                    for i, c in enumerate(cols_l):
                        if n in c and row[i]:
                            return row[i]
                return ""
            lines = []
            for row in tbl["rows"][:3]:
                stmt = cell(row, "statement string", "statement")
                stmt = re.sub(r"\s+", " ", stmt)[:110] + ("..." if len(stmt) > 110 else "")
                dur = cell(row, "duration", "total execution", "elapsed")
                who = cell(row, "app user", "db user", "user")
                extra = cell(row, "records", "status", "blocked")
                lines.append(stmt + (f" -- {dur} µs" if dur and "duration" in " ".join(cols_l) else (f" -- {dur}" if dur else ""))
                             + (f", user {who}" if who else "") + (f", {extra}" if extra and name == "active_statements" else ""))
            facts["focus"].append(f"{label.capitalize()} ({len(tbl['rows'])} listed): " + " | ".join(lines))
        else:
            facts["focus"].append(f"{label.capitalize()}: none listed")
    if facts.get("missing"):
        facts["focus"].append("Not reached: " + ", ".join(facts["missing"]))
    return facts


# ==========================================================================
# Registration
# ==========================================================================

def _guarded(name: str, fn):
    """
    Run an RCA action; on any exception log the FULL traceback (the collector
    only logs the type) and return what was gathered, so the section and its
    screenshots still reach the PDF. The collector's retry loop would otherwise
    re-run the whole T-code three times and then drop it.
    """
    def run(session, capture):
        import traceback
        holder: dict = {}
        RUN_CONTEXT["_partial"] = holder
        try:
            return fn(session, capture)
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error(f"RCA {name} failed: {type(e).__name__}: {e}\n{tb}")
            facts = dict(holder)
            facts.setdefault("rca", name)
            facts.setdefault("focus", [])
            facts["error"] = f"{type(e).__name__}: {e}"
            facts["focus"].append(f"{name.upper()} capture stopped early: {type(e).__name__}: {e} (see application.log for the traceback)")
            return facts
    return run


def register(actions: dict, stad_min_ms: int = 0, stad_lookback: int = 30, db_hint: str = "detect") -> dict:
    actions["rca_sm50"] = _guarded("sm50", rca_sm50)
    actions["rca_sm66"] = _guarded("sm66", rca_sm66)
    actions["rca_st03n"] = _guarded("st03n", rca_st03n)
    actions["rca_sm12"] = _guarded("sm12", rca_sm12)
    actions["rca_stad"] = _guarded("stad", lambda s, c: rca_stad(s, c, stad_lookback, stad_min_ms))
    actions["rca_st04"] = _guarded("st04", lambda s, c: rca_st04(s, c, db_hint))
    return actions


try:
    from sap_gui.tcode_actions import ACTIONS as _ACTIONS
    register(_ACTIONS)
except Exception as _e:  # noqa: BLE001
    log.debug(f"rca_actions: registry not extended at import ({type(_e).__name__}: {_e})")
