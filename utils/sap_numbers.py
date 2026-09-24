"""
Locale-safe parsing of numbers as SAP GUI prints them.

SAP formats numbers with the logged-on user's decimal notation
(SU01 > Defaults > Decimal Notation):

    1.234.567,89    X  -- '.' groups thousands, ',' marks decimals
    1,234,567.89    blank (English)
    1 234 567,89    Y

A parser that simply deletes one of the two characters is right for one
notation and wrong for the other by a factor of 100 or 1000. DB02 on PS4
printed "795,52 GB /1,13 TB"; deleting the comma gave 79552 GB used of
113 TB and a usage figure of 70,400 %.
"""

from __future__ import annotations

import re
from typing import Any

# SAP HANA / DBACOCKPIT report sizes in binary units.
_UNIT_TO_GB = {
    "B": 1.0 / 1024 ** 3,
    "KB": 1.0 / 1024 ** 2,
    "MB": 1.0 / 1024,
    "GB": 1.0,
    "TB": 1024.0,
    "PB": 1024.0 ** 2,
}


def parse_sap_decimal(raw: Any) -> float | None:
    """
    Parse a number that may carry thousands and decimal separators.

    - '.' and ',' both present: whichever occurs LAST is the decimal mark.
    - only one kind present, more than once: thousands separator.
    - only one kind present, once, followed by exactly three digits:
      thousands separator ("1.149" -> 1149). Everything else: decimal mark
      ("1,13" -> 1.13, "898.53" -> 898.53).
    - spaces, apostrophes and non-breaking spaces are digit grouping.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = re.sub(r"[\s'\u00a0]", "", str(raw))
    match = re.search(r"-?\d[\d.,]*", text)
    if not match:
        return None
    token = match.group(0).rstrip(".,")

    has_dot, has_comma = "." in token, "," in token
    if has_dot and has_comma:
        decimal = "." if token.rfind(".") > token.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        token = token.replace(thousands, "").replace(decimal, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        head, _, tail = token.rpartition(sep)
        if token.count(sep) > 1 or len(tail) == 3:
            token = token.replace(sep, "")
        else:
            token = head.replace(sep, "") + "." + tail

    try:
        return float(token)
    except ValueError:
        return None


def to_gb(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    factor = _UNIT_TO_GB.get(str(unit or "").strip().upper())
    return None if factor is None else value * factor


def parse_usage_ratio(raw: Any) -> dict:
    """
    Parse a DB02-style "used / limit" reading such as "795,52 GB /1,13 TB".

    Returns the figures in their printed units plus both converted to GB,
    and a usage percentage computed on the converted values -- the two
    sides of the ratio are not always in the same unit.
    """
    text = str(raw or "").strip()
    parsed = {
        "raw": text,
        "used": None,
        "limit": None,
        "unit": "",
        "limit_unit": "",
        "used_gb": None,
        "limit_gb": None,
        "usage_percent": None,
    }
    match = re.search(
        r"([\d.,\s]*\d)\s*([KMGTP]?B)\s*/\s*([\d.,\s]*\d)\s*([KMGTP]?B)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return parsed

    used = parse_sap_decimal(match.group(1))
    limit = parse_sap_decimal(match.group(3))
    unit = match.group(2).upper()
    limit_unit = match.group(4).upper()
    used_gb = to_gb(used, unit)
    limit_gb = to_gb(limit, limit_unit)

    parsed.update(
        used=used,
        limit=limit,
        unit=unit,
        limit_unit=limit_unit,
        used_gb=round(used_gb, 3) if used_gb is not None else None,
        limit_gb=round(limit_gb, 3) if limit_gb is not None else None,
    )
    if used_gb is not None and limit_gb:
        parsed["usage_percent"] = round(used_gb / limit_gb * 100.0, 2)
    return parsed
