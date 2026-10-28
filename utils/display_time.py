"""
Times shown on the dashboard pages, in Indian Standard Time.

Stamps are stored as the VM's local clock ("2026-10-23 16:25:24", no zone).
Pages show them converted to IST and labelled, e.g. "23 Oct 2026, 04:25:24 PM IST",
whatever time zone the VM itself is set to. IST is a fixed +05:30 with no
daylight saving, so no time-zone database is needed (Windows Python has none).

    IBO_DISPLAY_TZ_OFFSET   other fixed offset, e.g. "+04:00" [+05:30]
    IBO_DISPLAY_TZ_NAME     its label [IST]
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M")


def display_zone() -> timezone:
    raw = (os.environ.get("IBO_DISPLAY_TZ_OFFSET") or "+05:30").strip()
    name = (os.environ.get("IBO_DISPLAY_TZ_NAME") or ("IST" if raw == "+05:30" else "UTC" + raw)).strip()
    try:
        sign = -1 if raw.startswith("-") else 1
        hh, _, mm = raw.lstrip("+-").partition(":")
        return timezone(sign * timedelta(hours=int(hh), minutes=int(mm or 0)), name)
    except (ValueError, TypeError):
        return timezone(timedelta(hours=5, minutes=30), "IST")


def parse_stamp(stamp) -> datetime | None:
    if isinstance(stamp, datetime):
        return stamp
    s = str(stamp or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in _FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def to_display(stamp, with_date: bool = True) -> str:
    """A stored stamp as "23 Oct 2026, 04:25:24 PM IST" ('' for empty, the text itself if unparsable)."""
    dt = parse_stamp(stamp)
    if dt is None:
        return str(stamp or "")
    if dt.tzinfo is None:
        dt = dt.astimezone()            # naive = the VM's local clock
    dt = dt.astimezone(display_zone())
    t = dt.strftime("%I:%M:%S %p ") + (dt.tzname() or "")
    return (dt.strftime("%d %b %Y, ") + t) if with_date else t
