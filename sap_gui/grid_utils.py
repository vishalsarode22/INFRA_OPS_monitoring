"""
ALV grid discovery and paged reading for SAP GUI scripting.

Two problems this solves, both of which cost this project a full
extraction each:

1. HARDCODED CONTROL IDS DO NOT TRANSFER BETWEEN SYSTEMS.
   action_al08 looked for its GridView at three guessed paths
   ("wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[0]/shell/...").
   On PS4 none of them exist, so the read returned zero rows and the
   report said "0 sessions" for a screen showing 172. The SAP GUI object
   model is the authority on where a control lives; walk it rather than
   guessing. (SM51 hits the same wall and falls back to OCR.)

2. SAP ONLY RETURNS CELL VALUES FOR RENDERED ROWS.
   GetCellValue() on a row that is not currently painted returns an empty
   string, silently. A grid with 172 rows and 30 visible reads as 30 rows
   at best -- and as zero if the loop stops at the first blank. Rows must
   be paged into view with firstVisibleRow before being read.
"""

from __future__ import annotations

from utils.logger import get_logger

log = get_logger(__name__, "application")


# A GuiShell can nest arbitrarily deep inside containers; 12 is well past
# anything SAP produces in practice and stops a malformed tree looping.
_MAX_DEPTH = 12


def find_grids(session, root_id: str = "wnd[0]", max_depth: int = _MAX_DEPTH) -> list:
    """
    Every GridView shell reachable from root_id, in discovery order.

    Returns [] rather than raising: a screen with no ALV is a normal
    outcome (SM58 "Nothing was selected", an empty SMQ1), not an error.
    """
    found: list = []

    def walk(obj, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = obj.Children
            count = int(children.Count)
        except Exception:
            return
        for i in range(count):
            try:
                child = children.ElementAt(i)
            except Exception:
                continue
            try:
                if child.Type == "GuiShell" and child.SubType == "GridView":
                    found.append(child)
            except Exception:
                pass
            walk(child, depth + 1)

    try:
        walk(session.findById(root_id), 0)
    except Exception as exc:
        log.debug(f"Grid discovery failed under {root_id}: {exc}")

    return found


def _grid_size(grid) -> tuple[int, int]:
    """(rows, columns) for a grid, zeros when either cannot be read."""
    try:
        rows = int(grid.RowCount)
    except Exception:
        rows = 0
    try:
        cols = int(grid.ColumnCount)
    except Exception:
        cols = 0
    return rows, cols


def best_grid(session, root_id: str = "wnd[0]", min_columns: int = 2):
    """
    The grid most likely to hold the data, or None.

    A screen with several ALVs (a selection grid, a message log and the
    result) will hand back whichever comes first in the tree unless you
    choose. Ranking by row count then column count picks the data grid;
    an empty message log loses to a populated result every time.
    """
    grids = find_grids(session, root_id)
    if not grids:
        return None

    scored = []
    for g in grids:
        rows, cols = _grid_size(g)
        if cols < min_columns:
            continue
        scored.append((rows, cols, g))

    if not scored:
        return None

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    rows, cols, grid = scored[0]
    log.info(f"Grid selected: {rows} row(s) x {cols} column(s) "
             f"from {len(grids)} grid(s) on screen.")
    return grid


def grid_columns(grid) -> list[str]:
    """Column technical names, in display order."""
    _, count = _grid_size(grid)
    columns = []
    for i in range(count):
        try:
            name = str(grid.ColumnOrder(i)).strip()
        except Exception:
            continue
        if name:
            columns.append(name)
    return columns


def read_grid_rows(grid, max_rows: int = 5000) -> list[dict]:
    """
    Every row of an ALV as {column_lowercase: value}, paging as it goes.

    RowCount is the authority on how many rows exist. firstVisibleRow is
    what makes them readable: without it, GetCellValue returns "" for any
    row below the fold and the caller concludes the grid is short or
    empty. Reads that come back blank for a whole page stop the loop, so
    a grid that refuses to scroll costs one wasted page, not a timeout.
    """
    total, _ = _grid_size(grid)
    columns = grid_columns(grid)

    if not columns:
        log.warning("Grid has no readable columns; returning no rows.")
        return []

    if total <= 0:
        return []

    try:
        page = int(grid.VisibleRowCount)
    except Exception:
        page = 0
    if page <= 0:
        page = 20

    rows: list[dict] = []
    start = 0

    while start < total and len(rows) < max_rows:
        try:
            grid.firstVisibleRow = start
        except Exception:
            # Some grids render everything at once and reject the scroll.
            # Carry on: the reads below either work or end the loop.
            pass

        page_rows = 0
        for index in range(start, min(start + page, total)):
            row = {}
            has_value = False
            for column in columns:
                try:
                    value = str(grid.GetCellValue(index, column) or "").strip()
                except Exception:
                    value = ""
                row[column.lower()] = value
                if value:
                    has_value = True
            if has_value:
                rows.append(row)
                page_rows += 1

        if page_rows == 0:
            log.debug(f"Grid page at row {start} read blank; stopping at "
                      f"{len(rows)} of {total} row(s).")
            break

        start += page

    if len(rows) < total:
        log.info(f"Grid read {len(rows)} of {total} row(s) reported by RowCount.")

    return rows