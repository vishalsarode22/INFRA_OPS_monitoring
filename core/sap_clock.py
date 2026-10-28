"""
The SAP server's clock, for "today" and per-day keys.

A VM in India and a SAP system on UTC (or the other way round) disagree on
the date for several hours a day, so "once per job per day" and "dumps
today" must use the SAP server's date, not the VM's.

Where the time comes from, first that answers:
  1. a provider set with set_provider(fn) -- fn(system) -> datetime or None
     (the RFC collector's clock helper plugs in here; tests inject one);
  2. collectors.sap_clock.sap_now(system), when that module has learned
     the system's offset (is_learned) -- otherwise it would just return the
     VM clock;
  3. the offset SAP time minus VM time last stored by the custom checks
     (core/custom_alerts._note_sap_clock, from the SAP time each read returns);
  4. the VM's own clock.
Never raises.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Optional

_provider: Optional[Callable[[str], Optional[datetime]]] = None


def set_provider(fn: Optional[Callable[[str], Optional[datetime]]]) -> None:
    global _provider
    _provider = fn


def sap_now(system: str | None) -> datetime:
    system = str(system or "")
    if _provider is not None:
        try:
            got = _provider(system)
            if isinstance(got, datetime):
                return got
        except Exception:  # noqa: BLE001
            pass
    try:
        from collectors import sap_clock as _rfc_clock  # type: ignore[attr-defined]
        # Only when that module actually LEARNED this system's offset: its
        # sap_now() falls back to the VM clock otherwise, which would hide
        # the stored offset below.
        if system and _rfc_clock.is_learned(system):
            got = _rfc_clock.sap_now(system)
            if isinstance(got, datetime):
                return got
    except Exception:  # noqa: BLE001
        pass
    try:
        from core import custom_alerts
        offset = custom_alerts._sap_clock(system) if system else 0
        return datetime.now() + timedelta(seconds=offset)
    except Exception:  # noqa: BLE001
        return datetime.now()


def sap_today(system: str | None) -> str:
    """The SAP server's date, 'YYYY-MM-DD'."""
    return sap_now(system).strftime("%Y-%m-%d")
