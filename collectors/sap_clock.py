"""
SAP server clock, as an OFFSET from the monitoring host's clock.

WHY
---
SAP stores dates and times in the application server's own clock: SNAP
DATUM/UZEIT, TBTCO start/end, STAT STARTDATE/STARTTIME, ST03N periods, the
SM12 lock stamps. The monitoring VM may run in a different time zone or simply
be a few minutes off. Asking SNAP for "today" with the VM's date reads the
wrong day around midnight; a STAT window built from the VM clock reads the
wrong five minutes; a lock age measured against the VM clock goes negative.

An earlier version cached the SAP time itself (a datetime learned from the
last STAT read) and returned that frozen value as "now" until the next
learning -- so "now" stopped moving between polls and every age computed
against it drifted.

WHAT THIS DOES
--------------
Learns ``offset = SAP server time - VM time`` once per system and caches the
OFFSET (with a TTL). ``sap_now(system)`` is then ``VM now + offset``: it
moves with the real clock and only the difference is remembered.

Sources, most precise first:
  * a SAP timestamp the caller already has (``learn(system, sap_dt)``) --
    rfc_perf feeds the STAT frame's ENDTIMESTAMP + local offset here;
  * RFC_SYSTEM_INFO (every system has it): RFCSI_EXPORT-RFCTZONE is the
    application server's offset from UTC in seconds, so SAP local time is
    ``utcnow() + RFCTZONE``. Corrects time-zone differences; it assumes the
    VM's UTC clock is NTP-synced (it cannot see a skewed SAP host clock --
    the STAT source does).

Public API (import from here; the alerts engineer uses these):
    sap_now(system) -> datetime        VM clock when nothing was learned
    sap_today(system) -> "YYYYMMDD"
    ensure_offset(session, system)     learn via RFC if the cached offset expired
    learn(system, sap_dt, source)      record an observed SAP timestamp
    offset_seconds(system) / is_learned(system) / clock_source(system)
"""

from __future__ import annotations

import os as _os
import threading
import time
from collections.abc import MutableMapping
from datetime import datetime, timedelta, timezone

from utils.logger import get_logger

log = get_logger(__name__)

# How long a learned offset is trusted before ensure_offset() asks again.
# A stale offset is still used by sap_now() -- a time zone does not change
# between polls -- it is just refreshed at the next opportunity.
OFFSET_TTL_SECONDS = int(_os.environ.get("IBO_SAP_CLOCK_TTL_SECONDS", "1800") or 1800)

# Precision ranking: a better source is not overwritten by a worse one
# while it is still fresh.
_RANK = {"stat": 3, "server": 3, "rfc_system_info": 1}

_lock = threading.RLock()
# system -> (offset_seconds, learned_monotonic, source)
_offsets: dict[str, tuple[float, float, str]] = {}


def _vm_now() -> datetime:
    return datetime.now()


def learn(system: str, sap_dt: datetime, source: str = "server") -> None:
    """Record an observed SAP server local time (taken just now)."""
    if not system or not isinstance(sap_dt, datetime):
        return
    offset = (sap_dt - _vm_now()).total_seconds()
    with _lock:
        prev = _offsets.get(system)
        if prev and _RANK.get(prev[2], 2) > _RANK.get(source, 2) \
                and time.monotonic() - prev[1] < OFFSET_TTL_SECONDS:
            return                      # keep the more precise, still-fresh value
        _offsets[system] = (offset, time.monotonic(), source)
    if abs(offset) >= 120 and (not prev or abs(prev[0] - offset) >= 60):
        log.info(f"[{system}] SAP server clock differs from this host by "
                 f"{offset / 60:+.0f} min (source: {source}); SAP time is used "
                 f"for every date/time window on this system")
    _share(system, offset)


# Last offset written to the shared store per system: (monotonic, offset).
_shared: dict[str, tuple[float, float]] = {}


def _share(system: str, offset: float) -> None:
    """
    Publish SAP-minus-host to core.ops_db (table sap_clock) for processes
    that never hold an RFC session to this system (the checks worker reads
    it in collectors/extra_checks). Throttled; never raises.
    """
    try:
        last = _shared.get(system)
        if last and time.monotonic() - last[0] < 600 and abs(last[1] - offset) < 30:
            return
        _shared[system] = (time.monotonic(), offset)
        from core import ops_db
        ops_db.execute("INSERT INTO sap_clock(system, offset_s, at) VALUES (?,?,?) ON CONFLICT(system) "
                       "DO UPDATE SET offset_s=excluded.offset_s, at=excluded.at",
                       (system, offset, ops_db.now()))
    except Exception:  # noqa: BLE001 - sharing is best effort
        pass


def learn_offset(system: str, offset_seconds: float, source: str = "server") -> None:
    learn(system, _vm_now() + timedelta(seconds=float(offset_seconds)), source)


def forget(system: str | None = None) -> None:
    with _lock:
        if system is None:
            _offsets.clear()
        else:
            _offsets.pop(system, None)


def is_learned(system: str) -> bool:
    with _lock:
        return system in _offsets


def is_fresh(system: str) -> bool:
    with _lock:
        hit = _offsets.get(system)
    return bool(hit) and time.monotonic() - hit[1] < OFFSET_TTL_SECONDS


def offset_seconds(system: str) -> float | None:
    with _lock:
        hit = _offsets.get(system)
    return hit[0] if hit else None


def clock_source(system: str) -> str:
    """'sap' when an offset is known, 'host' when the VM clock is used."""
    return "sap" if is_learned(system) else "host"


_SOURCE_LABELS = {
    "stat": "RFC · STAT timestamps",
    "rfc_system_info": "RFC · RFC_SYSTEM_INFO time zone",
    "server": "RFC · SAP server clock",
}


def source_label(system: str) -> str | None:
    """Human label of where the offset came from, None when not learned."""
    with _lock:
        hit = _offsets.get(system)
    return _SOURCE_LABELS.get(hit[2], f"RFC · {hit[2]}") if hit else None


def sap_now(system: str) -> datetime:
    """SAP server local time now: VM clock + learned offset (VM clock if none)."""
    off = offset_seconds(system)
    now = _vm_now()
    return now + timedelta(seconds=off) if off is not None else now


def sap_today(system: str) -> str:
    return sap_now(system).strftime("%Y%m%d")


def _rfc_system_info_offset(session) -> float | None:
    """SAP local time - VM local time, from RFCSI_EXPORT-RFCTZONE."""
    try:
        res = session.call("RFC_SYSTEM_INFO")
    except Exception:  # noqa: BLE001 - best effort
        return None
    rfcsi = (res or {}).get("RFCSI_EXPORT") or {}
    raw = str(rfcsi.get("RFCTZONE", "") or "").strip()
    if not raw:
        return None
    try:
        tz_seconds = int(float(raw.replace(" ", "")))
    except ValueError:
        return None
    if abs(tz_seconds) > 14 * 3600:
        return None
    sap_local = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=tz_seconds)
    return (sap_local - _vm_now()).total_seconds()


def ensure_offset(session, system: str | None = None) -> bool:
    """
    Make sure an offset is known for this system; learn it over the open
    RFC session when it is missing or older than OFFSET_TTL_SECONDS.
    Costs one cheap RFC (RFC_SYSTEM_INFO) per system per TTL. Never raises.
    Returns True when an offset is known afterwards.
    """
    system = system or getattr(session, "system", "") or ""
    if not system:
        return False
    if is_fresh(system):
        return True
    if session is None or not getattr(session, "ok", False):
        return is_learned(system)
    off = _rfc_system_info_offset(session)
    if off is not None:
        learn_offset(system, off, "rfc_system_info")
    return is_learned(system)


class _ClockView(MutableMapping):
    """
    Backwards-compatible view: ``view[system]`` is the CURRENT SAP time
    (VM now + offset), assigning a datetime learns the offset. Lets older
    code (and tests) that used rfc_perf._sap_clock as {system: datetime}
    keep working, without the old frozen-value behaviour.
    """

    def __getitem__(self, system):
        if not is_learned(system):
            raise KeyError(system)
        return sap_now(system)

    def __setitem__(self, system, value):
        # Historically this mapping was only ever written from STAT frames.
        learn(system, value, "stat")

    def __delitem__(self, system):
        with _lock:
            del _offsets[system]

    def __iter__(self):
        with _lock:
            return iter(list(_offsets))

    def __len__(self):
        with _lock:
            return len(_offsets)

    def __contains__(self, system):
        return is_learned(system)

    def clear(self):
        forget()


CLOCK_VIEW = _ClockView()


class _LearnedAtView(MutableMapping):
    """
    ``view[system]`` is the host time at which the offset was learned.
    Assigning T says "the SAP time last given for this system was observed
    at host time T" and re-anchors the offset accordingly.
    """

    def __getitem__(self, system):
        with _lock:
            hit = _offsets.get(system)
        if not hit:
            raise KeyError(system)
        return _vm_now() - timedelta(seconds=time.monotonic() - hit[1])

    def __setitem__(self, system, when):
        with _lock:
            hit = _offsets.get(system)
            if not hit or not isinstance(when, datetime):
                return
            shift = (_vm_now() - when).total_seconds() - (time.monotonic() - hit[1])
            _offsets[system] = (hit[0] + shift, hit[1], hit[2])

    def __delitem__(self, system):
        pass                      # the offset itself is removed via CLOCK_VIEW

    def __iter__(self):
        with _lock:
            return iter(list(_offsets))

    def __len__(self):
        with _lock:
            return len(_offsets)


LEARNED_AT_VIEW = _LearnedAtView()
