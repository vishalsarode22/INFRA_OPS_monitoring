"""
On-call rotation: Basis alerts go to whoever is on shift, not a fixed list.

config/oncall.yaml (copy config/oncall.yaml.example):

    enabled: true
    rotation:
      start: 2026-10-05        # first day of the first shift
      handover: "09:00"        # the next person takes over at this time
      length_days: 7           # weekly rotation
      per_shift: 1             # people on call at the same time
      members:
        - {name: Vik Phalke, email: vik.phalke@infrabeat.com}
        - {name: Om Dighe,       email: staff.g@infrabeat.com}
    always_copy: []            # e.g. a team mailbox, copied on every Basis alert
    shift_source:              # optional: ask your shift tool instead
      url: http://127.0.0.1:8100/api/oncall/current
      token: ${ONCALL_TOKEN}

Who is on call, in order of precedence:
  1. an override set on the Operations page ("Om covers 10-12 Oct");
  2. shift_source, when configured and answering -- it must return JSON
     {"emails": ["a@x.com", ...]} (optionally "names"); on any failure the
     rotation below is used and the Health page shows the error;
  3. the rotation.

Where it applies: core/recipients.basis() -- every Basis-level alert (VPN,
credentials, first-breach, T-code and parser problems) and the Basis copy
on manager alerts. A system with its own `basis_emails:` in systems.yaml
keeps that list. With no oncall.yaml (or enabled: false) nothing changes:
IBO_BASIS_EMAILS is used as before.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

_cache_lock = threading.Lock()
_cfg_cache: dict = {"stamp": None, "data": None}
_src_cache: dict = {"at": 0.0, "value": None, "error": ""}


def _path() -> str:
    from core.config_loader import CONFIG_DIR
    return os.path.join(CONFIG_DIR, "oncall.yaml")


def load_config() -> dict:
    """Parsed oncall.yaml with ${VAR} resolved; {} when absent or invalid."""
    path = _path()
    try:
        st = os.stat(path)
    except OSError:
        return {}
    stamp = (st.st_mtime, st.st_size)
    with _cache_lock:
        if _cfg_cache["stamp"] == stamp:
            return _cfg_cache["data"] or {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError("top level must be a mapping")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"config/oncall.yaml unreadable ({exc}); using IBO_BASIS_EMAILS")
        data = {}
    data = _resolve(data)
    with _cache_lock:
        _cfg_cache.update(stamp=stamp, data=data)
    return data


def _resolve(value):
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.getenv(m.group(1), ""), value)
    return value


def enabled(cfg: dict | None = None) -> bool:
    cfg = load_config() if cfg is None else cfg
    return bool(cfg) and cfg.get("enabled", True) is not False and bool(
        (cfg.get("rotation") or {}).get("members") or (cfg.get("shift_source") or {}).get("url"))


def _members(cfg: dict) -> list[dict]:
    out = []
    for m in (cfg.get("rotation") or {}).get("members") or []:
        if isinstance(m, str):
            m = {"name": m.split("@")[0], "email": m}
        if isinstance(m, dict) and "@" in str(m.get("email", "")):
            out.append({"name": str(m.get("name") or m["email"].split("@")[0]), "email": str(m["email"]).strip(),
                        "phone": str(m.get("phone") or "").strip()})
    return out


def phones_for(emails: list[str]) -> list[str]:
    """Phone numbers of the rotation members with these e-mail addresses."""
    want = {e.lower() for e in emails or []}
    return [m["phone"] for m in _members(load_config()) if m["email"].lower() in want and m.get("phone")]


def _rotation_anchor(cfg: dict) -> tuple[datetime, float]:
    rot = cfg.get("rotation") or {}
    start = rot.get("start") or "2026-01-05"
    hand = rot.get("handover") or "09:00"
    try:
        start_d = datetime.strptime(str(start)[:10], "%Y-%m-%d")
    except ValueError:
        start_d = datetime(2026, 1, 5)
    try:
        if isinstance(hand, int) and not isinstance(hand, bool):
            hh, mm = divmod(hand, 60)       # YAML reads an unquoted 10:00 as 600 (base 60)
        else:
            hh, mm = [int(x) for x in str(hand).split(":")[:2]]
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except ValueError:
        hh, mm = 9, 0
    try:
        length = float(rot.get("length_days") or 7)
    except (TypeError, ValueError):
        length = 7.0
    return start_d.replace(hour=hh, minute=mm), max(0.25, length)


def rotation_at(at: datetime, cfg: dict | None = None) -> dict:
    """Who the ROTATION puts on call at `at` (ignoring overrides and shift_source)."""
    cfg = load_config() if cfg is None else cfg
    members = _members(cfg)
    if not members:
        return {"emails": [], "names": [], "source": "rotation", "from": None, "until": None}
    anchor, length = _rotation_anchor(cfg)
    try:
        per = max(1, min(int((cfg.get("rotation") or {}).get("per_shift") or 1), len(members)))
    except (TypeError, ValueError):
        per = 1
    span = timedelta(days=length)
    idx = int((at - anchor) // span)
    shift_start = anchor + idx * span
    picks = [members[(idx * per + i) % len(members)] for i in range(per)]
    return {"emails": [p["email"] for p in picks], "names": [p["name"] for p in picks],
            "source": "rotation", "from": shift_start.strftime("%Y-%m-%d %H:%M"),
            "until": (shift_start + span).strftime("%Y-%m-%d %H:%M")}


def _override_at(at: datetime) -> dict | None:
    try:
        from core import ops_db
        stamp = ops_db.fmt(at)
        row = ops_db.one("SELECT * FROM oncall_overrides WHERE cancelled_at IS NULL AND starts_at<=? "
                         "AND ends_at>? ORDER BY id DESC LIMIT 1", (stamp, stamp))
    except Exception:
        return None
    if not row:
        return None
    emails = [e.strip() for e in re.split(r"[,;\s]+", row["emails"]) if "@" in e]
    return {"emails": emails, "names": [e.split("@")[0] for e in emails], "source": "override",
            "from": row["starts_at"][:16], "until": row["ends_at"][:16], "reason": row.get("reason", ""),
            "id": row["id"]}


def _from_shift_source(cfg: dict) -> dict | None:
    src = cfg.get("shift_source") or {}
    url = str(src.get("url") or "").strip()
    if not url:
        return None
    with _cache_lock:
        if time.monotonic() - _src_cache["at"] < 300 and _src_cache["at"]:
            return _src_cache["value"]          # None after a failure: rotation is used for 5 min
    try:
        import requests
        headers = {"Authorization": f"Bearer {src['token']}"} if src.get("token") else {}
        r = requests.get(url, headers=headers, timeout=float(src.get("timeout_seconds") or 5))
        r.raise_for_status()
        body = r.json()
        emails = [e for e in (body.get("emails") or []) if "@" in str(e)]
        if not emails:
            raise ValueError("answer had no 'emails'")
        value = {"emails": emails, "names": list(body.get("names") or [e.split("@")[0] for e in emails]),
                 "source": "shift tool", "from": body.get("from"), "until": body.get("until")}
        with _cache_lock:
            _src_cache.update(at=time.monotonic(), value=value, error="")
        return value
    except Exception as exc:  # noqa: BLE001
        with _cache_lock:
            _src_cache.update(at=time.monotonic(), value=None, error=f"{type(exc).__name__}: {exc}")
        log.warning(f"on-call shift source {url} failed ({exc}); using the rotation")
        return None


def current(at: datetime | None = None) -> dict:
    """{emails, names, source, from, until, always_copy}; emails empty when not configured."""
    cfg = load_config()
    at = at or datetime.now()
    out = {"emails": [], "names": [], "source": "not configured", "from": None, "until": None}
    if enabled(cfg):
        out = _override_at(at) or (_from_shift_source(cfg) if at <= datetime.now() + timedelta(minutes=1)
                                   else None) or rotation_at(at, cfg)
    out["always_copy"] = [e for e in (cfg.get("always_copy") or []) if "@" in str(e)] if cfg else []
    out["shift_source_error"] = _src_cache.get("error") or ""
    return out


def basis_emails() -> list[str]:
    """On-call addresses plus always_copy, or [] when no rotation is configured."""
    try:
        cur = current()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"on-call lookup failed: {exc}")
        return []
    if not cur["emails"]:
        return []
    seen, out = set(), []
    for e in list(cur["emails"]) + list(cur.get("always_copy") or []):
        if e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def schedule(weeks: int = 8) -> list[dict]:
    """The next shifts, overrides included, for the Operations page."""
    cfg = load_config()
    if not enabled(cfg):
        return []
    out = []
    anchor, length = _rotation_anchor(cfg)
    span = timedelta(days=length)
    now = datetime.now()
    idx = int((now - anchor) // span)
    count = max(1, int(weeks * 7 / length))
    for i in range(count):
        start = anchor + (idx + i) * span
        r = rotation_at(start + timedelta(seconds=1), cfg)
        ov = _override_at(start + timedelta(seconds=1))
        out.append({"from": start.strftime("%Y-%m-%d %H:%M"),
                    "until": (start + span).strftime("%Y-%m-%d %H:%M"),
                    "names": r["names"], "emails": r["emails"],
                    "override": ov})
    return out


def add_override(starts_at: str, ends_at: str, emails: str, reason: str = "", by: str = "") -> dict:
    from core import ops_db, timeline
    start, end = ops_db.parse(starts_at), ops_db.parse(ends_at)
    if not start or not end or end <= start:
        raise ValueError("Give a start and an end, end after start.")
    if isinstance(emails, (list, tuple)):
        emails = ",".join(str(e) for e in emails)
    addrs = [e.strip() for e in re.split(r"[,;\s]+", str(emails or "")) if e.strip()]
    bad = [e for e in addrs if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)]
    if not addrs or bad:
        raise ValueError(f"Give one or more valid e-mail addresses{(': ' + ', '.join(bad)) if bad else ''}.")
    by = by or timeline.actor_name()
    oid = ops_db.execute("INSERT INTO oncall_overrides(starts_at, ends_at, emails, reason, created_by, created_at) "
                         "VALUES (?,?,?,?,?,?)", (ops_db.fmt(start), ops_db.fmt(end), ",".join(addrs),
                                                  str(reason or "")[:200], by, ops_db.now()))
    timeline.add("IBOPS", "config", f"On-call override: {', '.join(addrs)}",
                 f"{start:%d %b %H:%M} - {end:%d %b %H:%M} {reason}", actor=by)
    return ops_db.one("SELECT * FROM oncall_overrides WHERE id=?", (oid,))


def cancel_override(oid: int, by: str = "") -> None:
    from core import ops_db
    ops_db.execute("UPDATE oncall_overrides SET cancelled_at=? WHERE id=?", (ops_db.now(), int(oid)))


def overrides() -> list[dict]:
    from core import ops_db
    return ops_db.rows("SELECT * FROM oncall_overrides WHERE cancelled_at IS NULL AND ends_at>? "
                       "ORDER BY starts_at", (ops_db.now(),))
