"""
Generic helpers for extracting real data (not just screenshots) from
SAP GUI Scripting sessions -- grid row counts, label/field text.
Used by tcode_actions.py to populate the "Checks" column of the
monitoring Excel report with real values instead of just evidence images.
"""

from utils.logger import get_logger

log = get_logger(__name__, "application")


def get_grid_row_count(session, grid_id: str) -> int | None:
    """
    Returns the RowCount of a GuiGridView/ALV grid control, or None
    if the control isn't found or doesn't expose RowCount.
    """
    try:
        grid = session.findById(grid_id)
        return int(grid.RowCount)
    except Exception as e:
        log.debug(f"Could not read RowCount for {grid_id}: {e}")
        return None


def get_text(session, field_id: str) -> str | None:
    """Returns .text of a field, or None if not found."""
    try:
        return session.findById(field_id).text
    except Exception as e:
        log.debug(f"Could not read text for {field_id}: {e}")
        return None


def get_status_text(session) -> str | None:
    """Returns the SAP status bar message text, if present."""
    try:
        return session.findById("wnd[0]/sbar").text
    except Exception as e:
        log.debug(f"Could not read status bar: {e}")
        return None