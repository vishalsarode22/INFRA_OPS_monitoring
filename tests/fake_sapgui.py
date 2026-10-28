"""
A stand-in for SAP GUI scripting, built from screens recorded by the
read-only probe (scripts/probe_gui_tcodes.py, tests/fixtures/gui_screens_s4q.json).

A screen is the probe's layout dict: objects (id/type/name/text/tooltip),
grids (columns + rows), trees (nodes with key/text/parent/folder), menu and
toolbar. The test decides what each user action leads to with `on(...)`.
Every action is recorded in `session.log` so a test can prove that nothing
but reading happened.
"""
from __future__ import annotations

import copy
import json
import os
import re

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "gui_screens_s4q.json")


def screens() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class _Coll:
    def __init__(self, items):
        self._items = list(items)

    @property
    def Count(self):
        return len(self._items)

    def Item(self, i):
        return self._items[i]

    def ElementAt(self, i):
        return self._items[i]

    def __iter__(self):
        return iter(self._items)


class Obj:
    ContainerType = False

    def __init__(self, session, rec: dict):
        self._s = session
        self.Id = "/app/con[0]/ses[0]/wnd[0]" + rec["id"]
        self.Type = rec.get("type", "")
        self.Name = rec.get("name", "")
        self.Text = rec.get("text", "")
        self.Tooltip = rec.get("tooltip", "")
        self.SubType = rec.get("subtype", "")
        self.IconName = rec.get("icon", "")
        self.Selected = rec.get("selected", False)
        self._rec = rec

    # --- actions (recorded)
    def press(self):
        self._s.event("press", self.Tooltip.split("   ")[0].strip() or self.Text.strip() or self.Name)

    def select(self):
        self._s.event("tab", self.Text)

    def setFocus(self):
        self._s.focus = self

    def Select(self):          # menu entry
        self._s.event("menu", self._rec.get("path"))


class Container(Obj):
    ContainerType = True

    def __init__(self, session, rec, children):
        super().__init__(session, rec)
        self._children = children

    @property
    def Children(self):
        return _Coll(self._children)


class Grid(Obj):
    def __init__(self, session, rec, g):
        super().__init__(session, rec)
        self.Type, self.SubType = "GuiShell", "GridView"
        self._g = g
        self.FirstVisibleRow = 0
        self.SelectedRows = ""
        self.current = None
        self.Title = g.get("title", "")

    @property
    def ColumnOrder(self):
        return _Coll([c["id"] for c in self._g["columns"]])

    @property
    def RowCount(self):
        return len(self._g["rows"])

    def GetCellValue(self, r, c):
        self._s.cell_reads += 1
        return self._g["rows"][r].get(c, "")

    def GetDisplayedColumnTitle(self, c):
        return next((x["title"] for x in self._g["columns"] if x["id"] == c), "")

    def setCurrentCell(self, r, c):
        self.current = (r, c)


class Tree(Obj):
    def __init__(self, session, rec, t):
        super().__init__(session, rec)
        self.Type, self.SubType = "GuiShell", "Tree"
        self._nodes = {n["key"]: n for n in t["nodes"]}
        self._order = [n["key"] for n in t["nodes"]]
        self.expanded = session.expanded.setdefault(rec["id"], set())
        self.SelectedNode = None

    def _visible(self, k):
        p = self._nodes[k]["parent"]
        while p:
            if p not in self.expanded:
                return False
            p = self._nodes[p]["parent"]
        return True

    def GetAllNodeKeys(self):
        return _Coll([k for k in self._order if self._visible(k)])

    def GetNodeTextByKey(self, k):
        return self._nodes[k]["text"]

    def GetParent(self, k):
        return self._nodes[k]["parent"]

    def IsFolder(self, k):
        return self._nodes[k]["folder"]

    def ExpandNode(self, k):
        self.expanded.add(k)

    def DoubleClickNode(self, k):
        self._s.event("open", (self._nodes[k]["text"], k))


class Window(Container):
    def __init__(self, session, children):
        super().__init__(session, {"id": "", "type": "GuiMainWindow"}, children)
        self.Busy = False

    @property
    def Text(self):
        return self._s.screen.get("title", "")

    @Text.setter
    def Text(self, v):
        pass

    def sendVKey(self, n):
        self._s.event("vkey", n)


class Popup(Container):
    def __init__(self, session, title, n):
        super().__init__(session, {"id": f"/../wnd[{n}]", "type": "GuiModalWindow", "text": title}, [])

    def sendVKey(self, n):
        self._s.event("popup_vkey", n)
        self._s.popups.pop()

    def close(self):
        self._s.popups.pop()


class Session:
    def __init__(self, first_screen: dict):
        self.screen = first_screen
        self.status = ("", "")
        self.log: list = []
        self.rules: list = []
        self.popups: list = []
        self.expanded: dict = {}
        self.focus = None
        self.cell_reads = 0
        self._build()

    # ---- test wiring
    def on(self, kind, match, then):
        """When an event `kind` whose value matches `match` happens, call then(session, value)."""
        self.rules.append((kind, match, then))
        return self

    def show(self, screen, status=("", "")):
        self.screen = screen
        self.status = status
        self._build()

    def event(self, kind, value):
        self.log.append((kind, value))
        for k, m, then in self.rules:
            if k == kind and (m is None or m == value or (callable(m) and m(value))):
                then(self, value)
                return

    # ---- the scripting surface
    def _norm(self, rid):
        if rid.startswith("/usr") or rid.startswith("/shellcont"):
            return rid
        return "/usr" + rid

    def _build(self):
        sc = self.screen
        self.by_id = {}
        usr, shell = [], {}
        grids = {g["id"]: g for g in sc.get("grids", [])}
        trees = {t["id"]: t for t in sc.get("trees", [])}
        for rec in sc.get("objects", []):
            if "id" not in rec:
                continue
            rec = dict(rec, id=self._norm(rec["id"]))
            if rec["id"] in grids:
                o = Grid(self, rec, grids[rec["id"]])
            elif rec["id"] in trees:
                o = Tree(self, rec, trees[rec["id"]])
            else:
                o = Obj(self, rec)
            self.by_id[rec["id"]] = o
            if rec["id"].startswith("/shellcont"):
                shell.setdefault(rec["id"].split("/")[1], []).append(o)
            else:
                usr.append(o)
        for gid, g in grids.items():          # grids the object list did not name
            gid = self._norm(gid)
            if gid not in self.by_id:
                o = Grid(self, {"id": gid, "type": "GuiShell", "subtype": "GridView"}, g)
                self.by_id[gid] = o
                (shell.setdefault(gid.split("/")[1], []) if gid.startswith("/shellcont") else usr).append(o)
        for tid, t in trees.items():
            tid = self._norm(tid)
            if tid not in self.by_id:
                o = Tree(self, {"id": tid, "type": "GuiShell", "subtype": "Tree"}, t)
                self.by_id[tid] = o
                (shell.setdefault(tid.split("/")[1], []) if tid.startswith("/shellcont") else usr).append(o)
        self.usr = Container(self, {"id": "/usr", "type": "GuiUserArea"}, usr)
        docks = [Container(self, {"id": "/" + name, "type": "GuiDockShell"}, kids) for name, kids in shell.items()]
        menus = []
        for m in sc.get("menu", []):
            items = [Obj(self, {"id": "/mbar/x", "type": "GuiMenu", "text": i, "path": (m["text"], i)})
                     for i in m.get("items", [])]
            menus.append(Container(self, {"id": "/mbar/m", "type": "GuiMenu", "text": m["text"]}, items))
        self.mbar = Container(self, {"id": "/mbar", "type": "GuiMenubar"}, menus)
        btns = [Obj(self, {"id": "/" + b["id"], "type": "GuiButton", "text": b.get("text", ""),
                           "tooltip": b.get("tooltip", "")})
                for b in sc.get("toolbar", []) if b["id"].startswith("tbar[1]")]
        self.tbar1 = Container(self, {"id": "/tbar[1]", "type": "GuiToolbar"}, btns)
        self.okcd = Obj(self, {"id": "/tbar[0]/okcd", "type": "GuiOkCodeField"})
        self.sbar = Obj(self, {"id": "/sbar", "type": "GuiStatusbar", "text": self.status[0]})
        self.sbar.MessageType = self.status[1]
        self.wnd0 = Window(self, [self.mbar, self.tbar1, self.usr, *docks, self.sbar])

    def findById(self, path):
        if path.startswith("wnd[1]") or path.startswith("wnd[2]"):
            n = int(path[4])
            if len(self.popups) >= n:
                return Popup(self, self.popups[n - 1], n)
            raise RuntimeError("no popup")
        fixed = {"wnd[0]": self.wnd0, "wnd[0]/usr": self.usr, "wnd[0]/sbar": self.sbar,
                 "wnd[0]/mbar": self.mbar, "wnd[0]/tbar[1]": self.tbar1, "wnd[0]/tbar[0]/okcd": self.okcd}
        if path in fixed:
            return fixed[path]
        if path.startswith("wnd[0]/tbar[1]/"):
            for b in self.tbar1._children:
                if b.Id.endswith(path[len("wnd[0]"):]):
                    return b
        rid = path[len("wnd[0]"):]
        if rid in self.by_id:
            return self.by_id[rid]
        raise RuntimeError(f"control not found: {path}")


def install(module, session_holder: dict):
    """Point the reader module's navigation at the fake (goto = an event; no waiting)."""
    def goto(session, tcode):
        session.event("goto", tcode)
    module.goto_tcode = goto
    module.wait_until_not_busy = lambda session: None


def clone(screen):
    return copy.deepcopy(screen)


def label(x, y, text="", icon=""):
    return {"id": f"/usr/lbl[{x},{y}]", "type": "GuiLabel", "name": "", "text": text, "icon": icon}


def set_obj(screen, name, **attrs):
    for o in screen["objects"]:
        if o.get("name") == name:
            o.update(attrs)
            return o
    raise KeyError(name)


def lbl_xy(rec):
    m = re.search(r"lbl\[(\d+),(\d+)\]", rec.get("id", ""))
    return (int(m.group(1)), int(m.group(2))) if m else None
