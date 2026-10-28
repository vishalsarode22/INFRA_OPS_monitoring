"""
RFC_READ_TABLE, read correctly.

One helper for every table read in the collectors. It fixes four ways the
older per-call-site code went wrong:

1. NO DELIMITER SPLITTING. Rows were split on "|", so a value containing a
   "|" (a lock argument, a job name, a SAProuter string) shifted every later
   column. Each work area is now sliced by the OFFSET/LENGTH that SAP returns
   for every field in FIELDS, with no delimiter at all.

2. THE 512-BYTE ROW LIMIT. RFC_READ_TABLE returns rows in TAB512; a field
   list wider than that raises DATA_BUFFER_EXCEEDED, which used to be
   swallowed and reported as "table not readable". The helper now asks for
   the field lengths (NO_DATA='X'), splits the field list into chunks that
   fit, reads each chunk and joins the rows back together -- by key fields
   when the caller names them, by row position otherwise.

3. THE WHERE CLAUSE. OPTIONS is a table of 72-character lines. The old
   splitter collapsed all whitespace, including spaces INSIDE quoted
   literals (so "WHERE TEXT = 'A  B'" silently became 'A B'), and could put
   half of a quoted literal on one line and half on the next. split_options()
   tokenises quote-aware: a literal is one token, its spaces are preserved,
   and a line never breaks inside one.

4. SILENT ROW CAPS. A read capped at N rows returned N rows and looked
   complete. The helper asks for N+1 so it can tell, and returns
   ``partial: True`` with the cap when there was more. Callers that need a
   true count can page (``page_size``) with ROWSKIPS.

The helper accepts either a SapSession (whose .call never raises and keeps
the reason in .last_error) or a raw pyrfc Connection (whose .call raises).
"""

from __future__ import annotations

from typing import Any, Iterable

from utils.logger import get_logger

log = get_logger(__name__)

OPTIONS_LINE_LENGTH = 72
ROW_BYTES = 512

# A line must never END on a dangling operator: how SAP rejoins the lines is
# not something to rely on ("... AND" + "FIELD = 'x'" could become ANDFIELD).
_DANGLING = ("AND", "OR", "NOT", "(", "=", "<>", "<", ">", "<=", ">=", "LIKE", "IN")


# ---------------------------------------------------------------------------
# WHERE -> OPTIONS
# ---------------------------------------------------------------------------

def _tokenize_where(where: str) -> list[str]:
    """
    Split a WHERE clause into tokens. Whitespace outside quotes separates
    tokens and is collapsed; a quoted literal ('...' with '' as an escaped
    quote) is ONE token and keeps its inner spaces exactly. Text glued to a
    literal (e.g. "FIELD='A B'") stays in the same token.
    """
    tokens: list[str] = []
    cur: list[str] = []
    in_quote = False
    i, n = 0, len(where)
    while i < n:
        ch = where[i]
        if in_quote:
            cur.append(ch)
            if ch == "'":
                if i + 1 < n and where[i + 1] == "'":     # escaped quote
                    cur.append("'")
                    i += 1
                else:
                    in_quote = False
        elif ch == "'":
            in_quote = True
            cur.append(ch)
        elif ch.isspace():
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        tokens.append("".join(cur))
    return tokens


def split_options(where: str, width: int = OPTIONS_LINE_LENGTH) -> list[dict]:
    """
    WHERE clause -> RFC_READ_TABLE OPTIONS rows of at most `width` chars.

    Never breaks inside a quoted literal and preserves spaces inside it.
    A single token longer than `width` cannot be expressed safely; it is
    returned whole (SAP will reject it loudly) and a warning is logged, rather
    than being cut into something that parses as a different query.
    """
    tokens = _tokenize_where(str(where or ""))
    if not tokens:
        return []

    lines: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for tok in tokens:
        add = len(tok) + (1 if cur else 0)
        if cur and cur_len + add > width:
            lines.append(cur)
            cur, cur_len = [tok], len(tok)
        else:
            cur.append(tok)
            cur_len += add
    if cur:
        lines.append(cur)

    # Push trailing operators onto the next line.
    fixed: list[list[str]] = []
    carry: list[str] = []
    for line in lines:
        parts = carry + line
        carry = []
        while len(parts) > 1 and parts[-1].upper() in _DANGLING:
            carry.insert(0, parts.pop())
        fixed.append(parts)
    if carry:
        if fixed and len(" ".join(fixed[-1] + carry)) <= width:
            fixed[-1].extend(carry)
        else:
            fixed.append(carry)

    # Carrying can push a line over the width; re-pack those lines.
    out: list[str] = []
    for parts in fixed:
        text = " ".join(parts)
        if len(text) <= width or len(parts) == 1:
            out.append(text)
            continue
        cur, cur_len = [], 0
        for tok in parts:
            add = len(tok) + (1 if cur else 0)
            if cur and cur_len + add > width:
                out.append(" ".join(cur))
                cur, cur_len = [tok], len(tok)
            else:
                cur.append(tok)
                cur_len += add
        if cur:
            out.append(" ".join(cur))

    if any(len(line) > width for line in out):
        log.warning(f"WHERE clause has a token longer than {width} chars and "
                    f"cannot be split safely: {str(where)[:120]}")
    return [{"TEXT": line} for line in out]


# ---------------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------------

def _invoke(conn, kwargs: dict) -> tuple[dict | None, str]:
    """(result, error). Works for SapSession (never raises) and pyrfc Connection."""
    is_session = hasattr(conn, "last_error") and hasattr(conn, "system")
    if is_session:
        res = conn.call("RFC_READ_TABLE", **kwargs)
        return res, ("" if res is not None else str(getattr(conn, "last_error", "") or "failed"))
    try:
        return conn.call("RFC_READ_TABLE", **kwargs), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"RFC_READ_TABLE({kwargs.get('QUERY_TABLE')}): {type(exc).__name__}: {exc}"


def _int(raw, default=0) -> int:
    try:
        return int(str(raw).strip() or default)
    except (TypeError, ValueError):
        return default


def _layout(res: dict, asked: list[str]) -> list[tuple[str, int, int]]:
    """[(FIELDNAME, OFFSET, LENGTH)] from the echoed FIELDS table."""
    out = []
    for f in res.get("FIELDS", []) or []:
        name = str(f.get("FIELDNAME", "")).strip()
        if name:
            out.append((name, _int(f.get("OFFSET")), _int(f.get("LENGTH"))))
    if not out:
        return []
    # Offsets of 0 for every field but the first mean the reply carried no
    # layout (some fakes / very old releases). Rebuild it from the lengths.
    if len(out) > 1 and all(o == 0 for _, o, _l in out) and any(l for _, _o, l in out):
        pos, rebuilt = 0, []
        for name, _o, length in out:
            rebuilt.append((name, pos, length))
            pos += length
        out = rebuilt
    return out


def _slice(rows: Iterable, layout: list[tuple[str, int, int]]) -> list[dict]:
    # A reply with no LENGTH at all carries no layout (seen only from
    # RFC_READ_TABLE look-alikes and test doubles that answer with a
    # delimited WA). Fall back to the '|' split in the reply's own field
    # order rather than returning empty values.
    no_layout = bool(layout) and not any(length for _n, _o, length in layout)
    out = []
    for r in rows or []:
        wa = r.get("WA", "") if isinstance(r, dict) else str(r)
        wa = "" if wa is None else str(wa)
        if no_layout:
            parts = wa.split("|") if len(layout) > 1 else [wa]
            out.append({name: (parts[i] if i < len(parts) else "")
                        for i, (name, _o, _l) in enumerate(layout)})
        else:
            out.append({name: wa[off:off + length] for name, off, length in layout})
    return out


def _is_buffer_exceeded(err: str) -> bool:
    return "DATA_BUFFER_EXCEEDED" in (err or "").upper()


def _read_paged(conn, base: dict, want: int, page_size: int | None) -> tuple[list, dict | None, str]:
    """Rows (raw DATA dicts) up to `want`, paging with ROWSKIPS. (rows, last_res, error)."""
    rows: list = []
    last = None
    page = max(1, int(page_size)) if page_size else want
    skips = 0
    while len(rows) < want:
        n = min(page, want - len(rows))
        kw = dict(base, ROWCOUNT=n)
        if skips:
            kw["ROWSKIPS"] = skips
        res, err = _invoke(conn, kw)
        if res is None:
            return rows, last, err
        last = res
        data = res.get("DATA", []) or []
        rows.extend(data)
        if len(data) < n:
            break
        skips += len(data)
    return rows, last, ""


def _chunk_fields(layout: list[tuple[str, int, int]], keys: list[str]) -> list[list[str]]:
    """Field chunks each fitting ROW_BYTES, every chunk carrying the key fields."""
    lengths = {n: l for n, _o, l in layout}
    key_len = sum(lengths.get(k, 0) for k in keys)
    chunks: list[list[str]] = []
    cur, cur_len = list(keys), key_len
    for name, _o, length in layout:
        if name in keys:
            continue
        if cur_len + length > ROW_BYTES and len(cur) > len(keys):
            chunks.append(cur)
            cur, cur_len = list(keys), key_len
        cur.append(name)
        cur_len += length
    if len(cur) > len(keys) or not chunks:
        chunks.append(cur)
    return chunks


def read_table(conn, table: str, fields: list[str], where: str = "",
               max_rows: int = 500, *, key_fields: list[str] | None = None,
               page_size: int | None = None) -> dict[str, Any]:
    """
    Read `fields` of `table` matching `where`, at most `max_rows` rows.

    Returns a dict, never raises:
        rows      list of {FIELDNAME: value} (values unstripped, as SAP sent them)
        fields    the field names, in request order
        partial   True when more rows exist than max_rows (or a later chunk
                  or page failed) -- the rows are a SUBSET, say so
        row_cap   max_rows
        chunks    how many RFC_READ_TABLE field chunks were needed
        joined_by "key" | "row_index" | None
        error     None, or why the read failed (rows is then [])
    """
    fields = [str(f).strip() for f in fields if str(f).strip()]
    max_rows = max(1, int(max_rows))
    out: dict[str, Any] = {"rows": [], "fields": list(fields), "partial": False,
                           "row_cap": max_rows, "chunks": 1, "joined_by": None,
                           "error": None, "table": table}
    options = split_options(where)
    base = {"QUERY_TABLE": table, "DELIMITER": "", "OPTIONS": options,
            "FIELDS": [{"FIELDNAME": f} for f in fields]}
    want = max_rows + 1                 # one extra row tells us the cap was hit

    data, res, err = _read_paged(conn, base, want, page_size)
    if res is not None and not err:
        layout = _layout(res, fields)
        if data and not layout:
            out["error"] = f"RFC_READ_TABLE({table}): reply carried no FIELDS layout"
            return out
        rows = _slice(data, layout)
        out["partial"] = len(rows) > max_rows
        out["rows"] = rows[:max_rows]
        return out
    if res is not None and err:
        # A later page failed: what was read is real but incomplete.
        layout = _layout(res, fields)
        out["rows"] = _slice(data, layout)[:max_rows]
        out["partial"] = True
        out["error"] = err
        return out
    if not _is_buffer_exceeded(err):
        out["error"] = err
        return out

    # ---- Wider than 512 bytes: learn the layout, then read in chunks ----
    meta, err = _invoke(conn, dict(base, NO_DATA="X", ROWCOUNT=1))
    if meta is None:
        out["error"] = err
        return out
    layout = _layout(meta, fields)
    if not layout:
        out["error"] = f"RFC_READ_TABLE({table}): no field layout returned"
        return out
    keys = [k for k in (key_fields or []) if k in {n for n, _o, _l in layout}]
    chunks = _chunk_fields(layout, keys)
    out["chunks"] = len(chunks)
    out["joined_by"] = "key" if keys else "row_index"

    merged: list[dict] = []
    index: dict[tuple, dict] = {}
    for ci, chunk in enumerate(chunks):
        kw = dict(base, FIELDS=[{"FIELDNAME": f} for f in chunk])
        data, res, err = _read_paged(conn, kw, want, page_size)
        if res is None:
            if ci == 0:
                out["error"] = err
                return out
            out["partial"] = True
            out["error"] = err
            break
        part = _slice(data, _layout(res, chunk))
        if ci == 0:
            merged = part
            if keys:
                index = {tuple(r.get(k, "") for k in keys): r for r in merged}
            continue
        if keys:
            for r in part:
                tgt = index.get(tuple(r.get(k, "") for k in keys))
                if tgt is not None:
                    tgt.update(r)
        else:
            if len(part) != len(merged):
                out["partial"] = True
            for tgt, r in zip(merged, part):
                tgt.update(r)
    out["partial"] = out["partial"] or len(merged) > max_rows
    out["rows"] = [{f: r.get(f, "") for f in fields} for r in merged[:max_rows]]
    return out


def as_lists(result: dict) -> list[list[str]] | None:
    """read_table() result -> list of value lists in request order (None on error)."""
    if result.get("error") and not result.get("rows"):
        return None
    fields = result.get("fields") or []
    return [[r.get(f, "") for f in fields] for r in result.get("rows", [])]


# ---------------------------------------------------------------------------
# Decimal notation of the RFC user (USR01-DCPFM)
# ---------------------------------------------------------------------------

_notation_cache: dict[str, str | None] = {}


def rfc_user_notation(session) -> str | None:
    """
    The RFC user's decimal notation ('' / 'X' / 'Y'), read once per system
    from USR01. RFC_READ_TABLE formats packed/float numbers with it, so this
    is the context parse_sap_decimal() needs. None when not readable.
    """
    system = getattr(session, "system", "") or ""
    if system in _notation_cache:
        return _notation_cache[system]
    user = ""
    try:
        from collectors.rfc_collector import build_rfc_params
        user = ((build_rfc_params(getattr(session, "cfg", {}) or {}) or {}).get("user") or "").upper()
    except Exception:  # noqa: BLE001
        user = ""
    value = None
    if user and "'" not in user:
        try:
            rows = session.read_table("USR01", ["DCPFM"], f"BNAME = '{user}'", 1)
        except Exception:  # noqa: BLE001
            rows = None
        if rows:
            value = str(rows[0][0] if rows[0] else "").strip().upper()
            value = value if value in ("", "X", "Y") else None
    if system:
        _notation_cache[system] = value
    return value
