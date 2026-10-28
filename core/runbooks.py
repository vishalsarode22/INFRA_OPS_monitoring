"""
Runbooks: the checklist for an alert type (config/runbooks.yaml), and who
ticked which step, when.

A "run" is one runbook worked for one system on one day
(run key "<runbook id>|<system>|<YYYY-MM-DD>"), so a problem that comes
back tomorrow starts a fresh checklist while today's ticks stay visible to
whoever takes over the shift.
"""
from __future__ import annotations

import os
from datetime import datetime

from core import ops_db, timeline
from utils.logger import get_logger

log = get_logger(__name__)

_cache: dict = {"stamp": None, "data": []}


def _path() -> str:
    from core.config_loader import CONFIG_DIR
    return os.path.join(CONFIG_DIR, "runbooks.yaml")


def all_runbooks() -> list[dict]:
    path = _path()
    try:
        st = os.stat(path)
    except OSError:
        return []
    stamp = (st.st_mtime, st.st_size)
    if _cache["stamp"] == stamp:
        return _cache["data"]
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            raw = (yaml.safe_load(fh) or {}).get("runbooks") or []
    except Exception as exc:  # noqa: BLE001
        log.warning(f"config/runbooks.yaml unreadable: {exc}")
        return _cache["data"]
    out = []
    for rb in raw:
        if not isinstance(rb, dict) or not rb.get("id"):
            continue
        steps = []
        for st_ in rb.get("steps") or []:
            if isinstance(st_, str):
                st_ = {"text": st_}
            if isinstance(st_, dict) and st_.get("text"):
                steps.append({"text": str(st_["text"]), "detail": str(st_.get("detail") or "")})
        out.append({"id": str(rb["id"]), "title": str(rb.get("title") or rb["id"]),
                    "applies_to": [str(x) for x in (rb.get("applies_to") or [])], "steps": steps})
    _cache.update(stamp=stamp, data=out)
    return out


def get(runbook_id: str) -> dict | None:
    return next((r for r in all_runbooks() if r["id"] == runbook_id), None)


def for_alert(kind: str, metric: str | None = None) -> dict | None:
    """The runbook for an alert kind or metric; the generic one ('*') as a fallback."""
    books = all_runbooks()
    for key in (metric, kind):
        if key:
            hit = next((r for r in books if key in r["applies_to"]), None)
            if hit:
                return hit
    if kind == "critical" and metric is None:
        # A critical alert names its metrics in the key; the page picks per metric.
        pass
    return next((r for r in books if "*" in r["applies_to"]), None)


def run_key(runbook_id: str, system: str, day: str | None = None) -> str:
    return f"{runbook_id}|{system}|{day or datetime.now().strftime('%Y-%m-%d')}"


def state(key: str) -> dict[int, dict]:
    """Latest tick per step for one run."""
    out: dict[int, dict] = {}
    for r in ops_db.rows("SELECT * FROM runbook_ticks WHERE run_key=? ORDER BY id", (key,)):
        out[int(r["step"])] = r
    return out


def tick(key: str, step: int, done: bool = True, note: str = "", by: str = "") -> dict:
    rb_id, system, _day = (key.split("|") + ["", "", ""])[:3]
    rb = get(rb_id)
    if rb is None:
        raise ValueError("Unknown runbook.")
    if not 0 <= int(step) < len(rb["steps"]):
        raise ValueError("No such step.")
    by = by or timeline.actor_name()
    ops_db.execute("INSERT INTO runbook_ticks(run_key, step, done, note, actor, at) VALUES (?,?,?,?,?,?)",
                   (key, int(step), int(bool(done)), str(note or "")[:500], by, ops_db.now()))
    text = rb["steps"][int(step)]["text"]
    timeline.add(system or "?", "runbook",
                 f"{rb['title'][:60]}: step {int(step) + 1} {'done' if done else 'unticked'}",
                 f"{text[:200]}{(' -- ' + note) if note else ''}", actor=by, ref=f"runbook:{key}")
    return state(key)


def view(runbook_id: str, system: str, day: str | None = None) -> dict:
    rb = get(runbook_id)
    if rb is None:
        raise ValueError("Unknown runbook.")
    key = run_key(runbook_id, system, day)
    st = state(key)
    steps = []
    for i, s in enumerate(rb["steps"]):
        t = st.get(i)
        steps.append({**s, "index": i, "done": bool(t and t["done"]), "by": t["actor"] if t else "",
                      "at": t["at"] if t else "", "note": t["note"] if t else ""})
    done = sum(1 for s in steps if s["done"])
    return {"runbook": {k: rb[k] for k in ("id", "title", "applies_to")}, "system": system, "run_key": key,
            "steps": steps, "done": done, "total": len(steps)}
