"""
Locale-safe parsing of numbers as SAP GUI prints them.

SAP formats numbers with the logged-on user's decimal notation
(SU01 > Defaults > Decimal Notation, stored in USR01-DCPFM):

    DCPFM ' ' (blank)   1.234.567,89   '.' groups thousands, ',' marks decimals
    DCPFM 'X'           1,234,567.89   ',' groups thousands, '.' marks decimals
    DCPFM 'Y'           1 234 567,89   ' ' groups thousands, ',' marks decimals

When the notation is known, pass it as ``notation=`` -- it is the only way to
read "1.234" correctly in both notations (1.234 for 'X', 1234 for blank).
Without it the parser falls back to a heuristic (see parse_sap_decimal).

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


# DCPFM value -> (thousands separators, decimal mark)
_NOTATIONS = {
    "": ((".",), ","),
    "X": ((",",), "."),
    "Y": ((" ", "\u00a0", "."), ","),
}


def normalize_notation(notation: Any) -> str | None:
    """USR01-DCPFM value -> '', 'X' or 'Y'; None when unknown/absent."""
    if notation is None:
        return None
    text = str(notation).strip().upper()
    if text in ("", "BLANK", "DE", "EU", "COMMA"):
        return ""
    if text in ("X", "EN", "US", "DOT"):
        return "X"
    if text in ("Y",):
        return "Y"
    return None


def _grouping_ok(int_part: str, sep: str) -> bool:
    """'1.234.567' with sep '.' -> True; '1.23' -> False."""
    groups = int_part.split(sep)
    if len(groups) == 1:
        return True
    first = groups[0].lstrip("-")
    # A first group of "0" ("0.125") is never digit grouping: SAP does not
    # print leading zero groups, so that is a decimal mark.
    if not first or len(first) > 3 or not first.isdigit() or first.startswith("0"):
        return False
    return all(len(g) == 3 and g.isdigit() for g in groups[1:])


def _parse_with_notation(text: str, notation: str) -> float | None:
    thousands, decimal = _NOTATIONS[notation]
    match = re.search(r"-?\d[\d.,\s\u00a0']*", text)
    if not match:
        return None
    token = match.group(0).strip().rstrip(".,'")
    token = token.replace("'", "")
    for t in thousands:
        if t.strip() == "":
            token = token.replace(t, "")
    if token.count(decimal) > 1:
        return None
    head, sep, tail = token.partition(decimal)
    for t in thousands:
        if t.strip() and t in head:
            if not _grouping_ok(head, t):
                return None
            head = head.replace(t, "")
    if any(c in tail for c in ".,"):
        return None
    try:
        return float(f"{head}.{tail}" if sep else head)
    except ValueError:
        return None


def parse_sap_decimal(raw: Any, notation: Any = None) -> float | None:
    """
    Parse a number that may carry thousands and decimal separators.

    Returns None when nothing numeric can be read -- never 0.0, because a
    failed parse that reads as zero looks like a healthy reading.

    ``notation`` is the SAP decimal notation (USR01-DCPFM: '' / 'X' / 'Y').
    When given, it decides which character is the decimal mark. When the
    text does not fit that notation (e.g. "0,09" under 'X'), the heuristic
    below is used instead of guessing a thousands separator.

    Heuristic (no notation):
    - '.' and ',' both present: whichever occurs LAST is the decimal mark.
    - only one kind present, more than once: thousands separator.
    - a leading "0." / "0," (or "-0.") is always a decimal mark
      ("0.125" -> 0.125, never 125).
    - only one kind present, once, followed by exactly three digits:
      thousands separator ("1.149" -> 1149) -- SAP GUI's German default.
      Pass notation='X' to read English "1.234" as 1.234.
    - everything else: decimal mark ("1,13" -> 1.13, "898.53" -> 898.53).
    - spaces, apostrophes and non-breaking spaces are digit grouping.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw)
    norm = normalize_notation(notation)
    if norm is not None:
        value = _parse_with_notation(text, norm)
        if value is not None:
            return value

    text = re.sub(r"[\s'\u00a0]", "", text)
    match = re.search(r"-?\d[\d.,]*", text)
    if not match:
        return None
    token = match.group(0).rstrip(".,")

    has_dot, has_comma = "." in token, "," in token
    if has_dot and has_comma:
        decimal = "." if token.rfind(".") > token.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        if token.count(decimal) > 1:
            return None
        token = token.replace(thousands, "").replace(decimal, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        head, _, tail = token.rpartition(sep)
        leading_zero = head.lstrip("-") == "0"
        if token.count(sep) > 1:
            if not _grouping_ok(token, sep):
                return None
            token = token.replace(sep, "")
        elif len(tail) == 3 and not leading_zero:
            token = token.replace(sep, "")
        else:
            token = head + "." + tail

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
