"""
Screen probe for the new monitoring T-codes -- READ-ONLY.

    .venv\\Scripts\\python.exe scripts\\probe_gui_tcodes.py "CEAT S4P"
    .venv\\Scripts\\python.exe scripts\\probe_gui_tcodes.py "CEAT S4P" ST04 STRUST

Logs on to the system in SAP GUI exactly like a sweep, then for each T-code
(default: ST03N SMICM ST02 ST04 ST05 STMS STRUST) opens the screens the
monitoring sheet will need, takes a screenshot of each, and writes down the
screen's layout: every field and label, grid columns and the first rows,
tree nodes, menu entries and toolbar buttons. Then it logs off.

The readers for the sheet, PDF and wall are built from this output, so they
match YOUR release instead of a guess.

WHAT IT NEVER DOES
  * It presses no button that changes anything: no Save, no Activate /
    Deactivate trace (ST05), no Import / Check / Distribute (STMS), no
    certificate change (STRUST). It only opens T-codes, menu entries and
    tree nodes that DISPLAY something, and reads them.
  * A popup it did not expect is written down and closed with Cancel (F12),
    never with Yes / OK.
  * It does not run while a sweep holds the SAP GUI desktop (it stops and
    says so); pause automatic monitoring first.

OUTPUT
  logs\\probe_gui\\<system>_<date>_<time>\\  screenshots, probe.json, probe.txt
  logs\\probe_gui\\<system>_<date>_<time>.zip  -- send this file.
It contains what the screens show (user names, table names, certificate
subjects, host names). It contains no password.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(".env")

DEFAULT_TCODES = ["ST03N", "SMICM", "ST02", "ST04", "ST05", "STMS", "STRUST"]
MAX_GRID_ROWS = 40
MAX_TREE_NODES = 400
MAX_OBJECTS = 1500

# Menu entries / tree nodes / buttons with these words are never chosen.
UNSAFE = ("save", "activate", "deactivate", "switch on", "switch off", "trace on", "trace off",
          "import", "delete", "change", "create", "distribute", "release", "check", "reset",
          "restart", "stop", "start", "kill", "cancel", "execute", "generate", "upload",
          "replace", "remove", "edit", "sign", "renew", "configur", "maintain", "approve",
          "lock", "unlock", "schedule", "repair", "set ", "new ", "add ", "load", "unload",
          "merge", "reorg", "compress", "refresh", "update", "run", "collect", "clear", "drop",
          "truncate", "backup", "recover", "terminate", "cleanup", "file", "export", "download")
STEP_TIMEOUT_S = 180          # one T-code; then the probe closes its own connection and stops

_out: dict = {}
_watch = {"deadline": None, "stop": False, "step": ""}


def _watchdog():
    """A step that hangs (a blocked SAP GUI call) must not leave a production session open."""
    while not _watch["stop"]:
        dl = _watch["deadline"]
        if dl and time.time() > dl:
            print(f"\n!! {_watch['step']} did not finish within {STEP_TIMEOUT_S} s -- closing the probe's own "
                  f"SAP connection and stopping. If an SAP window stays open, close the probe's session by hand.",
                  flush=True)
            try:
                from sap_gui.scripting_connection import close_pinned_connection
                close_pinned_connection()
            except Exception:  # noqa: BLE001
                pass
            try:
                with open(os.path.join(_dir, "probe_TIMEOUT.txt"), "w", encoding="utf-8") as fh:
                    fh.write(f"timed out in {_watch['step']}\n")
            except Exception:  # noqa: BLE001
                pass
            os._exit(4)
        time.sleep(1)
_dir = ""
_shot_no = [0]


def _safe(text: str) -> bool:
    t = str(text or "").lower()
    return not any(w in t for w in UNSAFE)


def _try(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 -- a probe reads what it can
        return default


def _attr(obj, name, default=""):
    return _try(lambda: getattr(obj, name), default)


def _children(obj):
    if not _attr(obj, "ContainerType", False):
        return []
    ch = _attr(obj, "Children", None)
    n = int(_attr(ch, "Count", 0) or 0) if ch is not None else 0
    out = []
    for i in range(n):
        c = _try(lambda i=i: ch.Item(i)) or _try(lambda i=i: ch.ElementAt(i))
        if c is not None:
            out.append(c)
    return out


def _walk(obj, depth=0, max_depth=16):
    if obj is None or depth > max_depth:
        return
    yield obj
    for c in _children(obj):
        yield from _walk(c, depth + 1, max_depth)


def _wait(session):
    from sap_gui.tcode_navigator import wait_until_not_busy
    wait_until_not_busy(session)


def _popup_guard(session, where: str, rec: dict):
    """Write down any popup and close it with Cancel (F12) -- never Yes/OK."""
    for n in (2, 1):
        w = _try(lambda n=n: session.findById(f"wnd[{n}]"))
        if w is None:
            continue
        info = {"where": where, "window": f"wnd[{n}]", "title": str(_attr(w, "Text", "")),
                "texts": [str(_attr(o, "Text", ""))[:160] for o in _walk(w, max_depth=8)
                          if _attr(o, "Type") in ("GuiLabel", "GuiTextField", "GuiButton", "GuiTitlebar")
                          and str(_attr(o, "Text", "")).strip()][:40]}
        rec.setdefault("popups", []).append(info)
        _try(lambda w=w: w.sendVKey(12))
        _wait(session)
        if _try(lambda n=n: session.findById(f"wnd[{n}]")) is not None:
            _try(lambda w=w: w.close())
            _wait(session)


def _shot(session, rec: dict, tag: str):
    _shot_no[0] += 1
    name = f"{_shot_no[0]:02d}_{rec['tcode']}_{re.sub(r'[^A-Za-z0-9]+', '_', tag)[:40]}.png"
    from sap_gui.screenshot import capture_screenshot
    got = _try(lambda: capture_screenshot(session, rec["tcode"], output_dir=_dir), "")
    ok = bool(got and os.path.isfile(got))
    if ok:
        final = os.path.splitext(name)[0] + os.path.splitext(got)[1]
        _try(lambda: os.replace(got, os.path.join(_dir, final)))
        name = final
    rec.setdefault("screens", []).append({"tag": tag, "file": name if ok else None,
                                          "layout": _layout(session)})


def _grid_dump(g) -> dict:
    cols = []
    order = _try(lambda: g.ColumnOrder)
    names = []
    if order is not None:
        try:
            names = [str(c) for c in order]
        except TypeError:
            names = [str(_try(lambda i=i: order.Item(i), "")) for i in range(int(_attr(order, "Count", 0) or 0))]
    for c in names:
        cols.append({"id": c, "title": str(_try(lambda c=c: g.GetDisplayedColumnTitle(c), "") or ""),
                     "tooltip": str(_try(lambda c=c: g.GetColumnTooltip(c), "") or "")})
    nrows = int(_attr(g, "RowCount", 0) or 0)
    rows = []
    for r in range(min(nrows, MAX_GRID_ROWS)):
        rows.append({c: str(_try(lambda r=r, c=c: g.GetCellValue(r, c), "") or "")[:120] for c in names})
    tb = []
    for i in range(int(_try(lambda: g.ToolbarButtonCount, 0) or 0)):
        tb.append({"id": str(_try(lambda i=i: g.GetToolbarButtonId(i), "")),
                   "text": str(_try(lambda i=i: g.GetToolbarButtonText(i), "")),
                   "tooltip": str(_try(lambda i=i: g.GetToolbarButtonTooltip(i), ""))})
    return {"columns": cols, "row_count": nrows, "rows": rows, "toolbar": tb,
            "title": str(_attr(g, "Title", ""))}


def _tree_dump(t) -> dict:
    keys = _try(lambda: t.GetAllNodeKeys())
    out = []
    try:
        ks = [str(k) for k in keys] if keys is not None else []
    except TypeError:
        ks = [str(_try(lambda i=i: keys.Item(i), "")) for i in range(int(_attr(keys, "Count", 0) or 0))]
    cols = _try(lambda: t.GetColumnNames())
    colnames = []
    if cols is not None:
        try:
            colnames = [str(c) for c in cols]
        except TypeError:
            colnames = [str(_try(lambda i=i: cols.Item(i), "")) for i in range(int(_attr(cols, "Count", 0) or 0))]
    for k in ks[:MAX_TREE_NODES]:
        node = {"key": k, "text": str(_try(lambda k=k: t.GetNodeTextByKey(k), "") or ""),
                "parent": str(_try(lambda k=k: t.GetParent(k), "") or ""),
                "folder": bool(_try(lambda k=k: t.IsFolder(k), False))}
        if colnames:
            node["items"] = {c: str(_try(lambda k=k, c=c: t.GetItemText(k, c), "") or "")[:120] for c in colnames[:12]}
        out.append(node)
    return {"node_count": len(ks), "columns": colnames, "nodes": out}


def _layout(session) -> dict:
    root = _try(lambda: session.findById("wnd[0]"))
    lay = {"title": str(_attr(root, "Text", "")),
           "status": str(_try(lambda: session.findById("wnd[0]/sbar").Text, "") or ""),
           "transaction": str(_try(lambda: session.Info.Transaction, "") or ""),
           "program": str(_try(lambda: session.Info.Program, "") or ""),
           "screen": str(_try(lambda: session.Info.ScreenNumber, "") or ""),
           "objects": [], "grids": [], "trees": [], "menu": [], "toolbar": []}
    n = 0
    # The work area AND the docking area at the side (ST03N, DBA Cockpit and
    # STRUST keep their navigation tree in wnd[0]/shellcont, outside /usr).
    def _areas():
        for area in _area_roots(session):
            yield from _walk(area, max_depth=20)
    for o in _areas():
        n += 1
        if n > MAX_OBJECTS:
            lay["objects"].append({"note": f"more than {MAX_OBJECTS} objects, rest not listed"})
            break
        typ = str(_attr(o, "Type", ""))
        if typ in ("GuiUserArea", "GuiSimpleContainer", "GuiScrollContainer", "GuiCustomControl",
                   "GuiContainerShell", "GuiSplitterShell", "GuiDockShell"):
            continue
        rec = {"id": str(_attr(o, "Id", "")).split("wnd[0]", 1)[-1], "type": typ,
               "name": str(_attr(o, "Name", "")), "text": str(_attr(o, "Text", ""))[:200]}
        tip = str(_attr(o, "Tooltip", "") or "")
        if tip:
            rec["tooltip"] = tip[:120]
        if typ == "GuiShell":
            sub = str(_attr(o, "SubType", ""))
            rec["subtype"] = sub
            if sub == "GridView":
                lay["grids"].append({"id": rec["id"], **_grid_dump(o)})
            elif sub == "Tree":
                lay["trees"].append({"id": rec["id"], **_tree_dump(o)})
            elif sub in ("TextEdit", "HTMLViewer", "Toolbar"):
                if sub == "Toolbar":
                    rec["buttons"] = [{"id": str(_try(lambda i=i: o.GetButtonId(i), "")),
                                       "text": str(_try(lambda i=i: o.GetButtonText(i), "")),
                                       "tooltip": str(_try(lambda i=i: o.GetButtonTooltip(i), ""))}
                                      for i in range(int(_try(lambda: o.ButtonCount, 0) or 0))]
        if typ == "GuiTableControl":
            rec["rows"] = int(_attr(o, "RowCount", 0) or 0)
        if rec["text"].strip() or typ in ("GuiShell", "GuiTableControl", "GuiTab", "GuiTabStrip") or tip:
            lay["objects"].append(rec)
    mbar = _try(lambda: session.findById("wnd[0]/mbar"))
    for m in _children(mbar):
        lay["menu"].append({"text": str(_attr(m, "Text", "")),
                            "items": [str(_attr(i, "Text", "")) for i in _children(m)][:40]})
    for tb in ("wnd[0]/tbar[1]", "wnd[0]/tbar[0]"):
        for b in _children(_try(lambda tb=tb: session.findById(tb))):
            if _attr(b, "Type") == "GuiButton":
                lay["toolbar"].append({"id": str(_attr(b, "Id", "")).split("wnd[0]/", 1)[-1],
                                       "text": str(_attr(b, "Text", "")),
                                       "tooltip": str(_attr(b, "Tooltip", ""))})
    return lay


def _goto(session, tcode: str):
    from sap_gui.tcode_navigator import goto_tcode
    goto_tcode(session, tcode)


def _menu(session, rec, *path) -> bool:
    """Choose a DISPLAY menu entry by its text, level by level."""
    node = _try(lambda: session.findById("wnd[0]/mbar"))
    for needle in path:
        nxt = next((c for c in _children(node) if needle.lower() in str(_attr(c, "Text", "")).lower().replace("&", "")), None)
        if nxt is None:
            rec.setdefault("notes", []).append(f"menu entry '{' > '.join(path)}' not found")
            return False
        node = nxt
    if not _safe(_attr(node, "Text", "")):
        rec.setdefault("notes", []).append(f"menu entry '{_attr(node, 'Text', '')}' skipped (not display-only)")
        return False
    ok = _try(lambda: (node.Select(), True)[-1], False)
    _wait(session)
    _popup_guard(session, "menu " + " > ".join(path), rec)
    return bool(ok)


def _area_roots(session) -> list:
    """wnd[0]/usr plus every docking area of the main window (shellcont, shellcont[0], shellcont[1] ...)."""
    roots = []
    usr = _try(lambda: session.findById("wnd[0]/usr"))
    if usr is not None:
        roots.append(usr)
    for c in _children(_try(lambda: session.findById("wnd[0]"))):
        cid = str(_attr(c, "Id", ""))
        if "shellcont" in cid.rsplit("/", 1)[-1]:
            roots.append(c)
    return roots


def _first(session, subtype, prefer: tuple = ()):
    """The first control of this subtype; with `prefer`, the one whose node texts contain one of those words."""
    found = [o for area in _area_roots(session)
             for o in _walk(area, max_depth=20)
             if _attr(o, "Type") == "GuiShell" and _attr(o, "SubType") == subtype]
    if prefer and subtype == "Tree":
        for o in found:
            texts = " ".join(str(_try(lambda k=k: o.GetNodeTextByKey(k), "") or "") for k in _tree_keys(o)[:200]).lower()
            if any(w in texts for w in prefer):
                return o
    return found[0] if found else None


def _tree_keys(t):
    keys = _try(lambda: t.GetAllNodeKeys())
    try:
        return [str(k) for k in keys] if keys is not None else []
    except TypeError:
        return [str(_try(lambda i=i: keys.Item(i), "")) for i in range(int(_attr(keys, "Count", 0) or 0))]


def _expand(session, t, rounds, max_nodes=150, skip_under=()):
    """Expand folders (display only), at most `max_nodes` per round, not below `skip_under` branches."""
    done = set()
    for _ in range(rounds):
        n = 0
        for k in _tree_keys(t):
            if k in done or n >= max_nodes:
                continue
            txt = str(_try(lambda k=k: t.GetNodeTextByKey(k), "") or "").lower()
            if any(w in txt for w in skip_under) or not _try(lambda k=k: t.IsFolder(k), True):
                continue
            _try(lambda k=k: t.ExpandNode(k))
            done.add(k)
            n += 1
        _wait(session)


def _ancestors_text(t, k) -> str:
    out, cur = [], k
    for _ in range(8):
        cur = str(_try(lambda c=cur: t.GetParent(c), "") or "")
        if not cur:
            break
        out.append(str(_try(lambda c=cur: t.GetNodeTextByKey(c), "") or "").lower())
    return " / ".join(out)


def _open_nodes(session, rec, needles, limit=12, expand_rounds=2, leaf_only=False, exact=False,
                prefer=(), skip_under=(), first_child=False):
    """
    Open (double-click) tree nodes whose text matches `needles` (contains, or equals with exact=True);
    screenshot each. Nothing below a `skip_under` branch is expanded or opened.
    """
    t = _first(session, "Tree", prefer)
    if t is None:
        rec.setdefault("notes", []).append("no tree on this screen")
        return
    _expand(session, t, expand_rounds, skip_under=skip_under)
    opened = 0
    seen = set()
    keys = _tree_keys(t)
    for k in keys:
        txt = str(_try(lambda k=k: t.GetNodeTextByKey(k), "") or "").strip()
        low = txt.lower()
        hit = (low in needles) if exact else any(n in low for n in needles)
        if not txt or not hit or not _safe(txt) or low in seen:
            continue
        if skip_under and any(w in _ancestors_text(t, k) for w in skip_under):
            continue
        if first_child:
            kids = [c for c in keys if str(_try(lambda c=c: t.GetParent(c), "") or "") == k]
            if kids:
                k = kids[0]
                txt = f"{txt} / {str(_try(lambda: t.GetNodeTextByKey(k), '') or '')}"
        if leaf_only and not first_child and _try(lambda k=k: t.IsFolder(k), False):
            continue
        seen.add(low)
        ok = _try(lambda k=k: (setattr(t, "SelectedNode", k), t.DoubleClickNode(k), True)[-1], False)
        _wait(session)
        _popup_guard(session, f"node {txt}", rec)
        _shot(session, rec, f"node_{txt}")
        rec["screens"][-1]["opened_node"] = {"key": k, "text": txt, "ok": bool(ok)}
        opened += 1
        if opened >= limit:
            break
        t = _first(session, "Tree", prefer) or t
    if not opened:
        rec.setdefault("notes", []).append(f"no tree node matched {needles}")


# ---------------------------------------------------------------- T-codes

def p_st03n(session, rec):
    _goto(session, "ST03N"); _popup_guard(session, "start", rec)
    _shot(session, rec, "start")
    # Workload > Total > Day > today (the period chosen for the sheet)
    _open_nodes(session, rec, ("total",), limit=1, expand_rounds=1, exact=True)
    _open_nodes(session, rec, ("day",), limit=1, expand_rounds=2, exact=True)
    today = datetime.now()
    _open_nodes(session, rec, (today.strftime("%d.%m.%Y"), today.strftime("%m/%d/%Y"),
                               today.strftime("%Y-%m-%d"), today.strftime("%d/%m/%Y")), limit=1, expand_rounds=2)
    _shot(session, rec, "workload_overview_today_total")


def p_smicm(session, rec):
    _goto(session, "SMICM"); _popup_guard(session, "start", rec)
    _shot(session, rec, "start")
    if _menu(session, rec, "goto", "services"):
        _shot(session, rec, "services")


def p_st02(session, rec):
    _goto(session, "ST02"); _popup_guard(session, "start", rec)
    _shot(session, rec, "tune_summary")


def p_st04(session, rec):
    _goto(session, "ST04"); _popup_guard(session, "start", rec)
    _shot(session, rec, "start")
    # Only display screens with these exact names, never below Configuration / Jobs / Backup /
    # Diagnostics; the navigation tree is the one with these branch names (not the system list).
    rec["window_children"] = [{"id": str(_attr(c, "Id", "")), "type": str(_attr(c, "Type", ""))}
                              for c in _children(_try(lambda: session.findById("wnd[0]")))]
    _open_nodes(session, rec, ("large tables", "memory overview", "volumes", "tenants",
                                "column tables", "memory usage", "table usage"),
                limit=6, expand_rounds=2, exact=True,
                prefer=("current status", "performance", "space", "diagnostics", "system information"),
                skip_under=("configuration", "jobs", "backup", "diagnostics", "alerts", "landscape",
                            "documentation", "wizard", "sql editor", "sql console"))


def p_st05(session, rec):
    _goto(session, "ST05"); _popup_guard(session, "start", rec)
    _shot(session, rec, "trace_status")     # nothing is pressed on ST05


def p_stms(session, rec):
    _goto(session, "STMS"); _popup_guard(session, "start", rec)
    _shot(session, rec, "start")
    if _menu(session, rec, "overview", "systems"):
        _shot(session, rec, "system_overview")
        # Connection Test (F5): pings each system's TMS connection; changes nothing.
        btn = _try(lambda: session.findById("wnd[0]/tbar[1]/btn[5]"))
        if btn is not None and "connection test" in str(_attr(btn, "Tooltip", "")).lower():
            _try(lambda: btn.press())
            _wait(session)
            _popup_guard(session, "connection test", rec)
            _shot(session, rec, "connection_test")
        else:
            rec.setdefault("notes", []).append("Connection Test button not found")


def p_strust(session, rec):
    _goto(session, "STRUST"); _popup_guard(session, "start", rec)
    _shot(session, rec, "start")
    title = str(_attr(_try(lambda: session.findById("wnd[0]")), "Text", "")).lower()
    if "display" not in title:
        rec.setdefault("notes", []).append(
            f"STRUST opened as '{title}', not in display mode: no PSE opened (opening one in change mode "
            f"would lock it for colleagues). Tree and start screen recorded only.")
        return
    t = _first(session, "Tree")
    if t is None:
        rec.setdefault("notes", []).append("STRUST tree not found")
        return
    for k in _tree_keys(t):
        _try(lambda k=k: t.ExpandNode(k))
    _wait(session)
    keys = _tree_keys(t)
    leaves = []
    for k in keys:
        if _try(lambda k=k: t.IsFolder(k), True):
            continue
        txt = str(_try(lambda k=k: t.GetNodeTextByKey(k), "") or "")
        parent = str(_try(lambda k=k: t.GetParent(k), "") or "")
        ptxt = _ancestors_text(t, k)
        if not _safe(txt) or not _safe(ptxt):
            continue
        leaves.append((k, f"{ptxt.split(' / ')[-1] if ptxt else ''} / {txt}".strip(" /")))
    for k, label in leaves[:14]:
        t = _first(session, "Tree") or t
        _try(lambda k=k: (setattr(t, "SelectedNode", k), t.DoubleClickNode(k)))
        _wait(session)
        _popup_guard(session, f"PSE {label}", rec)
        subj = _try(lambda: session.findById("wnd[0]/usr/txtPSE-OWNCERT-SUBJECT"))
        if subj is not None and str(_attr(subj, "Text", "")).strip():
            _try(lambda: subj.setFocus())
            _try(lambda: session.findById("wnd[0]").sendVKey(2))     # F2 = show details (display only)
            _wait(session)
            _popup_guard(session, f"own certificate {label}", rec)
        _shot(session, rec, f"pse_{label}")
        rec["screens"][-1]["pse"] = {"key": k, "label": label}


PROBES = {"ST03N": p_st03n, "SMICM": p_smicm, "ST02": p_st02, "ST04": p_st04,
          "ST05": p_st05, "STMS": p_stms, "STRUST": p_strust}


def _summary(out: dict) -> str:
    lines = [f"Screen probe {out['system']} at {out['started']} (read-only)", ""]
    for rec in out["tcodes"]:
        lines.append(f"== {rec['tcode']}: {rec.get('result', '?')}")
        for n in rec.get("notes", []):
            lines.append(f"   note: {n}")
        for p in rec.get("popups", []):
            lines.append(f"   popup closed with Cancel: {p.get('title')} {p.get('texts', [])[:3]}")
        for s in rec.get("screens", []):
            lay = s["layout"]
            lines.append(f"   [{s['tag']}] {s.get('file') or 'NO SCREENSHOT'} -- title '{lay['title']}', "
                         f"{lay['program']}/{lay['screen']}, {len(lay['objects'])} fields, "
                         f"{len(lay['grids'])} grid(s), {len(lay['trees'])} tree(s); status '{lay['status']}'")
            for g in lay["grids"]:
                lines.append(f"      grid {g['row_count']} rows: " + ", ".join(c["title"] or c["id"] for c in g["columns"][:20]))
        if rec.get("error"):
            lines.append(f"   ERROR: {rec['error']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    global _dir
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    wanted = [t.upper() for t in sys.argv[2:]] or DEFAULT_TCODES
    bad = [t for t in wanted if t not in PROBES]
    if bad:
        print(f"unknown T-code(s) {bad}; choose from {list(PROBES)}")
        return 2
    from core.config_loader import get_all_systems, get_launch_config
    cfg = next((c for c in get_all_systems() if c.get("name") == name), None)
    if cfg is None:
        print(f"unknown system {name}; known: {', '.join(c.get('name') for c in get_all_systems())}")
        return 2
    if cfg.get("has_gui_access") is False:
        print(f"{name} has has_gui_access: false -- no SAP GUI here.")
        return 2

    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8000/api/scheduler")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        st = json.loads(opener.open(req, timeout=4).read().decode("utf-8"))
        if st.get("running"):
            print(f"The dashboard is running {st.get('current_system') or 'a sweep'} "
                  f"({st.get('current_profile') or 'sweep'}) right now. Wait until it finishes, "
                  "pause automatic monitoring on the Scheduler page, then run the probe again.")
            return 3
        if st.get("enabled"):
            print("Note: automatic monitoring is ON. Pause it on the Scheduler page while the probe runs, "
                  "so no sweep or RCA starts on this desktop at the same time.")
            if input("Continue anyway? (y/N) ").strip().lower() != "y":
                return 3
    except Exception:  # noqa: BLE001 -- dashboard not reachable: the file lock below still applies
        pass
    from core.run_lock import sweep_lock
    with sweep_lock(f"probe {name}") as got:
        if not got:
            print("A sweep is using SAP GUI right now. Pause automatic monitoring on the Scheduler page, "
                  "wait for the running sweep to finish, then run the probe again.")
            return 3
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
        _dir = os.path.join(ROOT, "logs", "probe_gui", f"{safe_name}_{stamp}")
        os.makedirs(_dir, exist_ok=True)
        out = {"system": name, "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "client": str(cfg.get("client") or ""), "tcodes": []}

        from sap_gui.launcher import kill_sap_processes, launch_saplogon, select_connection
        from sap_gui.connection import login
        from sap_gui.scripting_connection import get_scripting_session
        try:
            print(f"Logging on to {name} in SAP GUI ...")
            kill_sap_processes(force_all=False)      # never other people's SAP GUI
            launch = get_launch_config(connection_name=cfg["connection_name"])
            launch_saplogon(launch["exe_path"])
            select_connection(launch["connection_name"])
            time.sleep(2)
            res = login(client=str(cfg.get("client") or ""), username=cfg["username"], password=cfg["password"],
                        language=cfg.get("language", "EN"), submit=True, verify=True)
            if not res.get("success"):
                print("SAP GUI logon failed -- nothing probed. Check the user and password of this system.")
                out["logon"] = "failed"
                _watch["stop"] = True
                return 1
            time.sleep(2)
            session = get_scripting_session()
            import threading
            threading.Thread(target=_watchdog, daemon=True).start()
            for t in wanted:
                rec = {"tcode": t}
                out["tcodes"].append(rec)
                print(f"  {t} ...", flush=True)
                _watch.update(step=t, deadline=time.time() + STEP_TIMEOUT_S)
                try:
                    PROBES[t](session, rec)
                    rec["result"] = f"{len(rec.get('screens', []))} screen(s)"
                except Exception as exc:  # noqa: BLE001
                    rec["result"] = "error"
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                    rec["trace"] = traceback.format_exc()[-1500:]
                    _try(lambda: _shot(session, rec, "after_error"))
                _popup_guard(session, f"end of {t}", rec)
                _try(lambda: _goto(session, ""))          # back to the start screen before the next one
                _popup_guard(session, f"after leaving {t}", rec)
                _watch["deadline"] = None
        finally:
            _watch["stop"] = True
            try:
                kill_sap_processes(force_all=False)
            except Exception:  # noqa: BLE001
                pass
            try:
                with open(os.path.join(_dir, "probe.json"), "w", encoding="utf-8") as fh:
                    json.dump(out, fh, indent=1, ensure_ascii=False, default=str)
            except Exception as exc:  # noqa: BLE001
                print(f"probe.json not written: {exc}")
            try:
                with open(os.path.join(_dir, "probe.txt"), "w", encoding="utf-8") as fh:
                    fh.write(_summary(out))
            except Exception as exc:  # noqa: BLE001
                print(f"probe.txt not written: {exc}")
            if out.get("logon") == "failed":
                print("Nothing to send: the logon failed.")
            else:
                zpath = _dir + ".zip"
                try:
                    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                        for f in sorted(os.listdir(_dir)):
                            z.write(os.path.join(_dir, f), arcname=os.path.join(os.path.basename(_dir), f))
                    print("\n" + _summary(out))
                    print(f"\nDone. Send this file: {zpath}")
                except Exception as exc:  # noqa: BLE001
                    print(f"zip not written ({exc}); send the folder {_dir} instead")
    return 0


if __name__ == "__main__":
    sys.exit(main())
