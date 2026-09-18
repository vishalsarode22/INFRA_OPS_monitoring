"""
Per-T-code action sequences, translated from SAP GUI Script Recorder
recordings. Each function takes (session, capture) -- capture(suffix)
takes and saves a screenshot at that point, callable multiple times
for multi-checkpoint flows like ST22.
"""

import time
import re
from pathlib import Path

from sap_gui.field_extractor import get_grid_row_count, get_status_text
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy
from sap_gui.sm50_extractor import extract_sm50_rows
from utils.logger import get_logger

log = get_logger(__name__, "application")


def _read_list_status_records_passed(session, open_if_needed: bool = True):
    """
    Read the authoritative row count from SAP's "List Status" popup (Shift+F7).

    The popup's labels are NOT at a stable position. Reading a hard-coded
    "wnd[1]/usr/lbl[26,9]" worked on the machine the script was recorded on
    and returned "The control could not be found by id" here -- and because
    the caller defaulted the count to 0 rather than to None, that failure
    became a report stating "0 cancelled background jobs" while the screen
    behind it listed two. This walks the popup's labels instead, finds the
    caption mentioning "passed", and reads the number on the same line.

    Returns the count, or None when the popup could not be opened or read.
    None means UNKNOWN, which callers must never treat as zero.
    """
    def _labels():
        try:
            usr = session.findById("wnd[1]/usr")
            child_count = int(usr.Children.Count)
        except Exception:
            return []
        found = []
        for index in range(child_count):
            try:
                obj = usr.Children(index)
                text = str(getattr(obj, "Text", "") or "").strip()
                if not text:
                    continue
                top = int(getattr(obj, "CharTop", getattr(obj, "Top", -1)))
                left = int(getattr(obj, "CharLeft", getattr(obj, "Left", -1)))
                found.append((top, left, text))
            except Exception:
                continue
        return found

    labels = _labels()

    if not labels and open_if_needed:
        try:
            session.findById("wnd[0]").sendVKey(19)
            time.sleep(1)
        except Exception as exc:
            log.warning("List Status popup could not be opened: %s", exc)
            return None
        labels = _labels()

    if not labels:
        return None

    for needle in ("records passed", "passed", "number of records"):
        for top, left, text in labels:
            if needle not in text.lower():
                continue

            # Normal layout: caption on the left, figure to its right.
            same_line = sorted(
                (lf, tx) for (tp, lf, tx) in labels
                if tp == top and lf > left and re.fullmatch(r"[\d.,]+", tx)
            )
            if same_line:
                try:
                    return int(re.sub(r"[^\d]", "", same_line[0][1]))
                except ValueError:
                    pass

            # Some themes render caption and figure as a single label.
            match = re.search(r"(\d[\d.,]*)\s*$", text)
            if match:
                try:
                    return int(re.sub(r"[^\d]", "", match.group(1)))
                except ValueError:
                    pass

    return None


def _wildcard_field_holding_logon_user(session, replacement: str = "*"):
    """
    Replace the selection field that currently contains the logged-on user.

    SP01 pre-fills "Created by" with whoever is logged on. Two earlier
    approaches to finding that field both depend on something invisible or
    unstable -- the control's technical name, or the caption sitting at the
    same character row as its input. This one keys on the thing actually
    visible on screen and known for certain at runtime: the field's value
    equals session.Info.User.

    Returns True if a field was rewritten.
    """
    try:
        logon_user = str(session.Info.User or "").strip()
    except Exception as exc:
        log.debug("Could not read the logon user from the session: %s", exc)
        return False

    if not logon_user:
        return False

    inputs = []

    def walk(obj, depth=0):
        if depth > 10:
            return
        try:
            children = obj.Children
            count = int(children.Count)
        except Exception:
            return
        for index in range(count):
            try:
                child = children(index)
                kind = str(getattr(child, "Type", "") or "")
            except Exception:
                continue
            if kind in ("GuiTextField", "GuiCTextField"):
                inputs.append(child)
            walk(child, depth + 1)

    try:
        walk(session.findById("wnd[0]/usr"))
    except Exception as exc:
        log.debug("Selection screen walk failed: %s", exc)
        return False

    # Log the whole screen once. Whatever happens next, the next run's log
    # says exactly which fields exist and what they hold -- which is what
    # was missing while this was being diagnosed blind.
    inventory = []
    for field in inputs:
        try:
            inventory.append(
                f"{getattr(field, 'Name', '?')}={str(getattr(field, 'Text', '') or '')!r}"
            )
        except Exception:
            continue
    log.info("Selection screen fields: %s", "; ".join(inventory) or "(none)")

    for field in inputs:
        try:
            current = str(getattr(field, "Text", "") or "").strip()
        except Exception:
            continue

        if current.upper() != logon_user.upper():
            continue

        name = getattr(field, "Name", "?")
        for attempt in ("direct", "clear-then-set"):
            try:
                if attempt == "clear-then-set":
                    field.text = ""
                field.text = replacement
            except Exception as exc:
                log.debug("Field '%s' %s failed: %s", name, attempt, exc)
                continue
            try:
                after = str(getattr(field, "Text", "") or "").strip()
            except Exception:
                after = replacement
            if after == replacement:
                log.info(
                    "Field '%s' held the logon user %r and was widened to "
                    "%r (%s).", name, current, after, attempt,
                )
                return True

    log.debug("No selection field currently holds the logon user %r.",
              logon_user)
    return False


def _labelled_fields(session):
    """
    Pair each screen label with the input field to its right.

    Yields (label_text, field_object). Used both to write a field found by
    its caption and to report what a caption currently reads.
    """
    labels, fields = [], []

    def walk(obj, depth=0):
        if depth > 10:
            return
        try:
            children = obj.Children
            count = int(children.Count)
        except Exception:
            return
        for index in range(count):
            try:
                child = children(index)
                kind = str(getattr(child, "Type", "") or "")
                top = int(getattr(child, "CharTop", getattr(child, "Top", -1)))
                left = int(getattr(child, "CharLeft", getattr(child, "Left", -1)))
            except Exception:
                continue
            if kind == "GuiLabel":
                try:
                    text = str(getattr(child, "Text", "") or "").strip()
                except Exception:
                    text = ""
                if text:
                    labels.append((top, left, text))
            elif kind in ("GuiTextField", "GuiCTextField"):
                fields.append((top, left, child))
            walk(child, depth + 1)

    try:
        walk(session.findById("wnd[0]/usr"))
    except Exception as exc:
        log.debug("Label/field pairing failed: %s", exc)
        return

    for top, left, text in labels:
        candidates = sorted(
            (lf, obj) for (tp, lf, obj) in fields if tp == top and lf > left
        )
        if candidates:
            yield text, candidates[0][1]


def _set_field_beside_label(session, value, *label_needles):
    """
    Set the input field sitting immediately right of a screen label.

    Technical field names differ between releases and are not visible to
    anyone reading the screen, but the label is: SP01's user filter is
    simply captioned "Created by". Matching the caption and then taking
    the nearest input on the same line works regardless of what SAP calls
    the field underneath, and it is the same technique the List Status
    reader uses for the SM37 popup.

    Returns True if a field was written.
    """
    needles = tuple(n.lower() for n in label_needles)
    labels, fields = [], []

    def walk(obj, depth=0):
        if depth > 10:
            return
        try:
            children = obj.Children
            count = int(children.Count)
        except Exception:
            return
        for index in range(count):
            try:
                child = children(index)
                kind = str(getattr(child, "Type", "") or "")
                top = int(getattr(child, "CharTop", getattr(child, "Top", -1)))
                left = int(getattr(child, "CharLeft", getattr(child, "Left", -1)))
            except Exception:
                walk_child = None
                continue
            if kind == "GuiLabel":
                try:
                    text = str(getattr(child, "Text", "") or "").strip()
                except Exception:
                    text = ""
                if text:
                    labels.append((top, left, text))
            elif kind in ("GuiTextField", "GuiCTextField"):
                fields.append((top, left, child))
            walk(child, depth + 1)

    try:
        walk(session.findById("wnd[0]/usr"))
    except Exception as exc:
        log.debug("Label-based field lookup failed: %s", exc)
        return False

    for top, left, text in labels:
        if not any(needle in text.lower() for needle in needles):
            continue
        candidates = sorted(
            (lf, obj) for (tp, lf, obj) in fields if tp == top and lf > left
        )
        if not candidates:
            continue
        field = candidates[0][1]
        try:
            field.text = ""
            field.text = value
            log.info("Field beside label '%s' set to '%s'.", text, value)
            return True
        except Exception as exc:
            log.debug("Could not write field beside label '%s': %s", text, exc)

    return False


def _wildcard_selection_field(session, *name_needles):
    """
    Put "*" in a selection-screen field identified by its control name.

    SAP pre-fills several selection screens with the logged-on user, which
    silently narrows a system-wide check to one person's rows. Field IDs
    differ per screen and release, so the field is located by walking the
    selection screen and matching the control's Name, rather than by a
    hard-coded path. Returns True if at least one field was set.
    """
    needles = tuple(n.upper() for n in name_needles)
    matched = []
    seen_fields = []

    def walk(obj, depth=0):
        if depth > 10:
            return
        try:
            children = obj.Children
            count = int(children.Count)
        except Exception:
            return
        for index in range(count):
            try:
                child = children(index)
            except Exception:
                continue
            try:
                kind = str(getattr(child, "Type", "") or "")
                name = str(getattr(child, "Name", "") or "")
            except Exception:
                kind, name = "", ""
            if kind in ("GuiTextField", "GuiCTextField"):
                seen_fields.append(name)
                if any(needle in name.upper() for needle in needles):
                    matched.append(child)
            walk(child, depth + 1)

    try:
        walk(session.findById("wnd[0]/usr"))
    except Exception as exc:
        log.debug("Selection screen walk failed: %s", exc)

    changed = False

    for field in matched:
        name = getattr(field, "Name", "?")
        before = ""
        try:
            before = str(getattr(field, "Text", "") or "")
        except Exception:
            pass

        # Two strategies, tried independently. They used to sit in one try
        # block, so if clearing raised, the "*" was never attempted and the
        # whole thing failed silently.
        for attempt in ("direct", "clear-then-set"):
            try:
                if attempt == "clear-then-set":
                    field.text = ""
                field.text = "*"
            except Exception as exc:
                log.debug("Selection field '%s' %s failed: %s",
                          name, attempt, exc)
                continue

            # Read back. An assignment that raises nothing but leaves the
            # old value in place is the failure mode that would otherwise
            # look like success in the log.
            try:
                after = str(getattr(field, "Text", "") or "")
            except Exception:
                after = "*"

            if after.strip() == "*":
                changed = True
                log.info(
                    "Selection field '%s' widened: %r -> %r (%s).",
                    name, before, after, attempt,
                )
                break

            log.debug("Selection field '%s' still reads %r after %s.",
                      name, after, attempt)

        if changed:
            break

    if not changed:
        # Name-matching missed. Log every text field actually on the screen
        # so the next run says which one to target, instead of leaving the
        # check silently scoped to one user again.
        log.warning(
            "No selection field matched %s. Text fields present on this "
            "screen: %s",
            list(needles),
            ", ".join(sorted(set(f for f in seen_fields if f))) or "(none)",
        )

    return changed


def _press_labelled_toolbar_button(session, *needle_groups):
    """
    Press a toolbar button identified by its visible text/tooltip.

    SAP GUI toolbar button IDs (btn[5], btn[8] ...) shift between patch
    levels and themes, so matching on the label is far more durable than
    hard-coding an index -- action_sm50 already does this inline, and this
    is that idiom made reusable. Each needle group is a tuple of lowercase
    substrings that must ALL appear in one button's tooltip, text or name.
    Returns True if a button was pressed.
    """
    for toolbar_id in ("wnd[0]/tbar[1]", "wnd[0]/tbar[0]"):
        try:
            toolbar = session.findById(toolbar_id)
            child_count = int(toolbar.Children.Count)
        except Exception:
            continue

        for index in range(child_count):
            try:
                obj = toolbar.Children(index)
                label = " ".join([
                    str(getattr(obj, "Tooltip", "") or ""),
                    str(getattr(obj, "Text", "") or ""),
                    str(getattr(obj, "Name", "") or ""),
                ]).lower()
            except Exception:
                continue

            for needles in needle_groups:
                if all(n in label for n in needles):
                    try:
                        obj.press()
                        log.info("Pressed toolbar button '%s' in %s",
                                 label.strip(), toolbar_id)
                        return True
                    except Exception as exc:
                        log.debug("Toolbar button press failed (%s): %s",
                                  label.strip(), exc)
    return False


def _optional(session, wnd_id: str, fn, description: str = ""):
    try:
        obj = session.findById(wnd_id)
        fn(obj)
    except Exception as e:
        log.debug(f"Optional step skipped ({description or wnd_id}): {e}")


def action_al08(session, capture):
    """
    AL08 - Logged-On Users.

    Extracts the active AL08 sessions directly from the native
    SAP GUI GridView.

    Information returned:
        - Total active sessions
        - Unique users
        - GUI sessions
        - RFC sessions
        - Background sessions
        - High / Medium / Low priority sessions
        - Complete structured session rows

    Screenshot is retained for PDF evidence.
    """

    goto_tcode(session, "AL08")
    wait_until_not_busy(session)

    # ---------------------------------------------------------
    # Execute / refresh AL08
    # ---------------------------------------------------------
    try:
        session.findById("wnd[0]/tbar[1]/btn[8]").press()
        wait_until_not_busy(session)
    except Exception as e:
        log.warning(f"AL08 execute failed: {e}")

    # ---------------------------------------------------------
    # Screenshot
    # ---------------------------------------------------------
    screenshot = capture()

    result = {
        "logged_on_users": 0,
        "total_active_sessions": 0,
        "unique_users": 0,
        "gui_sessions": 0,
        "rfc_sessions": 0,
        "background_sessions": 0,
        "high_priority_sessions": 0,
        "medium_priority_sessions": 0,
        "low_priority_sessions": 0,
        "users": [],
        "extraction_method": "sap_gui_alv",
    }

    if screenshot:
        result["screenshot"] = screenshot

    # ---------------------------------------------------------
    # Locate actual AL08 GridView
    # ---------------------------------------------------------
    grid_ids = [
        "wnd[0]/usr/cntlGRID1/shellcont/shell/"
        "shellcont[0]/shell/shellcont[1]/shell",

        "wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell",

        "wnd[0]/usr/cntlGRID1/shellcont/shell",
    ]

    grid = None

    for grid_id in grid_ids:
        try:
            candidate = session.findById(grid_id)

            if (
                candidate.Type == "GuiShell"
                and candidate.SubType == "GridView"
            ):
                grid = candidate
                log.info(
                    f"AL08: GridView found at {grid_id}"
                )
                break

        except Exception:
            continue

    if grid is None:
        log.warning(
            "AL08: native GridView not found."
        )
        # Do NOT return `result` here: it is pre-populated with
        # total_active_sessions=0, unique_users=0, etc., and those zeros
        # are indistinguishable from a genuine "nobody is logged on"
        # reading once they reach the collector. That is what let a real
        # session count (this system: "15 user sessions with 19 ABAP
        # sessions") get reported as "Active Sessions: 0; Unique Users: 0"
        # -- the collector saw a non-empty dict, concluded scripting had
        # already produced real data, and skipped the OCR fallback that
        # would otherwise have read the true figures off the screenshot.
        # Returning {} instead (like the failure path for every other
        # T-code) lets that fallback run.
        return {}

    # ---------------------------------------------------------
    # Get column names
    # ---------------------------------------------------------
    try:
        column_count = int(grid.ColumnCount)
    except Exception:
        column_count = 0

    columns = []

    for i in range(column_count):
        try:
            column = str(
                grid.ColumnOrder(i)
            ).strip()

            if column:
                columns.append(column)

        except Exception:
            continue

    # ---------------------------------------------------------
    # Extract rows
    #
    # Important:
    # This SAP AL08 GridView reports RowCount=6 while
    # GetCellValue() exposes row index 6 as well.
    # Therefore RowCount is not used as the sole boundary.
    # ---------------------------------------------------------
    rows = []

    empty_row_limit = 2
    consecutive_empty = 0
    row_index = 0

    while consecutive_empty < empty_row_limit:

        row = {}
        valid_data = False

        for column in columns:
            try:
                value = grid.GetCellValue(
                    row_index,
                    column,
                )

                value = str(
                    value or ""
                ).strip()

                row[column.lower()] = value

                if value:
                    valid_data = True

            except Exception:
                row[column.lower()] = ""

        if valid_data:
            rows.append(row)
            consecutive_empty = 0
        else:
            consecutive_empty += 1

        row_index += 1

        # Safety limit
        if row_index > 10000:
            log.warning(
                "AL08: row extraction safety limit reached."
            )
            break

    # ---------------------------------------------------------
    # Calculate Information
    # ---------------------------------------------------------
    unique_users = set()

    gui_sessions = 0
    rfc_sessions = 0
    background_sessions = 0

    high_priority = 0
    medium_priority = 0
    low_priority = 0

    for row in rows:

        user_name = row.get(
            "user_name",
            ""
        ).strip()

        if user_name:
            unique_users.add(user_name)

        session_type = row.get(
            "session_type",
            ""
        ).strip().lower()

        if session_type == "gui":
            gui_sessions += 1

        elif session_type == "rfc":
            rfc_sessions += 1

        elif session_type == "background":
            background_sessions += 1

        priority = row.get(
            "priority",
            ""
        ).strip().lower()

        if priority == "high":
            high_priority += 1

        elif priority == "medium":
            medium_priority += 1

        elif priority == "low":
            low_priority += 1

    total_active_sessions = len(rows)

    # ---------------------------------------------------------
    # Final result
    # ---------------------------------------------------------
    result["logged_on_users"] = len(unique_users)
    result["total_active_sessions"] = total_active_sessions
    result["unique_users"] = len(unique_users)

    result["gui_sessions"] = gui_sessions
    result["rfc_sessions"] = rfc_sessions
    result["background_sessions"] = background_sessions

    result["high_priority_sessions"] = high_priority
    result["medium_priority_sessions"] = medium_priority
    result["low_priority_sessions"] = low_priority

    result["users"] = rows

    log.info(
        "AL08: sessions=%s, unique_users=%s, "
        "GUI=%s, RFC=%s, Background=%s, "
        "High=%s, Medium=%s, Low=%s",
        total_active_sessions,
        len(unique_users),
        gui_sessions,
        rfc_sessions,
        background_sessions,
        high_priority,
        medium_priority,
        low_priority,
    )

    return result

def action_db01(session, capture_screenshot):

    """
    DB01 - Database Locks / Deadlocks.

    Uses the actual DB01 blocked-transactions ALV exposed by SAP GUI
    scripting. Zero ALV rows is a valid successful result and must not
    trigger OCR fallback.
    """
    result = {
        "lock_count": 0,
        "deadlock_count": 0,
        "locks": [],
        "deadlocks": [],
        "blocked_transactions": [],
        "blocker_transactions": [],
        "extraction_method": "sap_gui_alv",
    }

    blocked_grid_id = (
        "wnd[0]/usr/"
        "cntlBLOCKED_TRANS_ALV_CONTAINER/"
        "shellcont/shell"
    )

    blocker_grid_id = (
        "wnd[0]/usr/"
        "cntlBLOCKER_TRANS_ALV_CONTAINER/"
        "shellcont/shell"
    )

    def read_alv(grid):
        """Read a SAP GUI ALV GuiShell into dictionaries."""
        rows = []

        try:
            row_count = int(grid.RowCount)
        except Exception:
            row_count = 0

        try:
            column_count = int(grid.ColumnCount)
        except Exception:
            column_count = 0

        columns = []

        for i in range(column_count):
            try:
                column_id = str(grid.ColumnOrder(i))
                if column_id:
                    columns.append(column_id)
            except Exception:
                continue

        for row_index in range(row_count):
            row = {}

            for column_id in columns:
                try:
                    value = grid.GetCellValue(row_index, column_id)
                    row[column_id] = (
                        "" if value is None else str(value).strip()
                    )
                except Exception:
                    row[column_id] = ""

            rows.append(row)

        return rows, columns

    try:
        # ---------------------------------------------------------
        # Navigate to DB01
        # ---------------------------------------------------------
        session.findById("wnd[0]/tbar[0]/okcd").text = "/NDB01"
        session.findById("wnd[0]").sendVKey(0)
        time.sleep(2)

        # ---------------------------------------------------------
        # Execute / refresh where available
        # ---------------------------------------------------------
        for control_id in (
            "wnd[0]/tbar[1]/btn[8]",
            "wnd[0]/tbar[1]/btn[5]",
        ):
            try:
                session.findById(control_id).press()
                time.sleep(1)
                break
            except Exception:
                continue

        # ---------------------------------------------------------
        # Always preserve PDF evidence
        # ---------------------------------------------------------
        try:
            result["screenshot"] = capture_screenshot()
        except Exception:
            result["screenshot"] = None

        # ---------------------------------------------------------
        # Read the actual DB01 blocked-transactions ALV
        # ---------------------------------------------------------
        blocked_grid = None

        try:
            blocked_grid = session.findById(blocked_grid_id)
        except Exception:
            blocked_grid = None

        if blocked_grid is None:
            # The expected DB01 ALV is not available.
            # Do not falsely report zero.
            result["extraction_method"] = "sap_gui_alv_unavailable"
            result["extraction_error"] = (
                "DB01 blocked-transactions ALV was not found"
            )
            return result

        blocked_rows, blocked_columns = read_alv(blocked_grid)

        result["blocked_transactions"] = blocked_rows
        result["locks"] = blocked_rows
        result["lock_count"] = len(blocked_rows)

        result["blocked_alv_columns"] = blocked_columns

        try:
            result["blocked_alv_row_count"] = int(
                blocked_grid.RowCount
            )
        except Exception:
            result["blocked_alv_row_count"] = len(blocked_rows)

        # ---------------------------------------------------------
        # Read blocker ALV if it is actually populated
        # ---------------------------------------------------------
        blocker_grid = None

        try:
            blocker_grid = session.findById(blocker_grid_id)
        except Exception:
            blocker_grid = None

        blocker_rows = []
        blocker_columns = []

        if blocker_grid is not None:
            try:
                blocker_rows, blocker_columns = read_alv(blocker_grid)
            except Exception:
                blocker_rows = []
                blocker_columns = []

        result["blocker_transactions"] = blocker_rows
        result["blocker_alv_columns"] = blocker_columns

        try:
            result["blocker_alv_row_count"] = int(
                blocker_grid.RowCount
            ) if blocker_grid is not None else 0
        except Exception:
            result["blocker_alv_row_count"] = len(blocker_rows)

        # ---------------------------------------------------------
        # Deadlock detection
        #
        # DB01's blocked-transactions ALV represents blocked
        # transactions. Treat explicit lock/deadlock indicators as
        # deadlocks, but do not classify every blocked transaction
        # as a deadlock.
        # ---------------------------------------------------------
        deadlocks = []

        deadlock_tokens = (
            "DEADLOCK",
            "DEAD LOCK",
        )

        for row in blocked_rows:
            row_text = " ".join(
                str(value)
                for value in row.values()
                if value is not None
            ).upper()

            if any(token in row_text for token in deadlock_tokens):
                deadlocks.append(row)

        result["deadlocks"] = deadlocks
        result["deadlock_count"] = len(deadlocks)

        # ---------------------------------------------------------
        # Successful structured extraction
        #
        # RowCount == 0 is VALID:
        #   blocked transactions = 0
        #   deadlocks = 0
        # ---------------------------------------------------------
        result["extraction_method"] = "sap_gui_alv"
        result["status"] = "OK"

        if result["lock_count"] == 0:
            result["summary"] = (
                "No blocked transactions found in DB01."
            )
        elif result["deadlock_count"] > 0:
            result["summary"] = (
                f"{result['lock_count']} blocked transaction(s); "
                f"{result['deadlock_count']} deadlock indicator(s)."
            )
        else:
            result["summary"] = (
                f"{result['lock_count']} blocked transaction(s); "
                "no explicit deadlock indicators."
            )

        return result

    except Exception as exc:
        # ---------------------------------------------------------
        # Preserve screenshot and return an explicit extraction
        # failure instead of pretending that zero rows means zero
        # locks.
        # ---------------------------------------------------------
        result["extraction_method"] = "sap_gui_alv_error"
        result["extraction_error"] = str(exc)

        try:
            if not result.get("screenshot"):
                result["screenshot"] = capture_screenshot()
        except Exception:
            result["screenshot"] = None

        return result

def action_db02(session, capture_screenshot):
    """
    DB02 - Database Overview.

    Navigation is based on the recorded SAP GUI VBS:
        /NDB02
        expand node 100
        select item 101 / Task
        double-click item 101 / Task

    This opens:
        Current Status -> Overview

    Structured values are then read directly from SAP GUI fields.
    """

    import re
    import time

    result = {
        "tablespaces": [],
        "tablespace_count": 0,
        "critical_count": 0,
        "warning_count": 0,

        "operational_state": "",

        "database_name": "",
        "db_user": "",
        "host": "",
        "instance_name": "",
        "refresh_date": "",
        "refresh_time": "",

        "memory": {},
        "storage": {},
        "cpu": {},
        "alerts": [],

        "extraction_method": "sap_gui_structured",
        "screenshot": None,
    }

    # -------------------------------------------------------------
    # Helper: safely read SAP GUI text
    # -------------------------------------------------------------
    def read_text(control_id):
        try:
            control = session.findById(control_id)
            value = getattr(control, "Text", "")
            return str(value).strip() if value is not None else ""
        except Exception:
            return ""

    # -------------------------------------------------------------
    # Helper: parse "used / limit"
    # Example:
    #   898.53 GB /976.56 GB
    #   3.26 TB /3.90 TB
    # -------------------------------------------------------------
    def parse_usage(raw):
        parsed = {
            "raw": str(raw or "").strip(),
            "used": None,
            "limit": None,
            "unit": "",
            "limit_unit": "",
            "usage_percent": None,
        }

        text = str(raw or "").strip()

        if not text:
            return parsed

        try:
            match = re.search(
                r"([\d.,]+)\s*([A-Za-z]+)\s*/\s*"
                r"([\d.,]+)\s*([A-Za-z]+)",
                text,
            )

            if not match:
                return parsed

            used = float(
                match.group(1).replace(",", "")
            )

            used_unit = match.group(2).upper()

            limit = float(
                match.group(3).replace(",", "")
            )

            limit_unit = match.group(4).upper()

            parsed["used"] = used
            parsed["limit"] = limit
            parsed["unit"] = used_unit
            parsed["limit_unit"] = limit_unit

            if limit > 0:
                parsed["usage_percent"] = round(
                    (used / limit) * 100,
                    2,
                )

        except Exception:
            pass

        return parsed

    try:
        # =========================================================
        # 1. Maximize SAP GUI
        # =========================================================
        try:
            session.findById("wnd[0]").maximize()
        except Exception:
            pass

        # =========================================================
        # 2. Navigate to DB02
        # =========================================================
        session.findById(
            "wnd[0]/tbar[0]/okcd"
        ).text = "/NDB02"

        session.findById("wnd[0]").sendVKey(0)

        time.sleep(3)

        # =========================================================
        # 3. Navigate exactly as recorded in the user's VBS
        #
        # This is the DB02 navigation tree.
        # =========================================================
        tree_id = (
            "wnd[0]/shellcont[1]/shell/"
            "shellcont[1]/shell"
        )

        tree = session.findById(tree_id)

        # Same as VBS:
        # hierarchyHeaderWidth = 258
        try:
            tree.hierarchyHeaderWidth = 258
        except Exception:
            pass

        # Expand Current Status
        tree.expandNode("        100")

        time.sleep(1)

        # Bring Current Status node into view
        try:
            tree.topNode = "        100"
        except Exception:
            pass

        # Select Overview / Task
        tree.selectItem(
            "        101",
            "Task",
        )

        try:
            tree.ensureVisibleHorizontalItem(
                "        101",
                "Task",
            )
        except Exception:
            pass

        # Open Overview
        tree.doubleClickItem(
            "        101",
            "Task",
        )

        time.sleep(3)

        # =========================================================
        # 4. Verify Overview page
        # =========================================================
        overview_id = (
            "wnd[0]/usr/"
            "txtHDB_OVERVIEW-DATABASE_NAME"
        )

        try:
            session.findById(overview_id)
        except Exception as exc:
            result["extraction_method"] = (
                "sap_gui_structured_error"
            )
            result["extraction_error"] = (
                "DB02 Overview page could not be opened: "
                + str(exc)
            )

            try:
                result["screenshot"] = capture_screenshot()
            except Exception:
                pass

            return result

        # =========================================================
        # 5. Capture the actual Overview evidence
        # =========================================================
        try:
            result["screenshot"] = capture_screenshot()
        except Exception:
            result["screenshot"] = None

        # =========================================================
        # 6. General DB information
        # =========================================================
        result["database_name"] = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-DATABASE_NAME"
        )

        result["db_user"] = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-DB_USER"
        )

        result["host"] = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-HOST"
        )

        result["instance_name"] = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-INSTANCE_NAME"
        )

        result["refresh_date"] = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-REFRESH_DATE"
        )

        result["refresh_time"] = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-REFRESH_TIME"
        )

        # =========================================================
        # 7. Operational State
        # =========================================================
        result["operational_state"] = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-STATE"
        )

        # =========================================================
        # 8. Memory
        # =========================================================
        memory_mdc_raw = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-DB_MEMORY"
        )

        memory_tenant_raw = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-DB_MEMORY_TEN"
        )

        result["memory"] = {
            "mdc": parse_usage(memory_mdc_raw),
            "tenant": parse_usage(memory_tenant_raw),
        }

        # =========================================================
        # 9. Storage
        # =========================================================
        storage_data_raw = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-DB_STORAGE_DATA"
        )

        storage_log_raw = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-DB_STORAGE_LOG"
        )

        storage_trace_raw = read_text(
            "wnd[0]/usr/txtHDB_OVERVIEW-DB_STORAGE_TRACE"
        )

        result["storage"] = {
            "data": parse_usage(storage_data_raw),
            "log": parse_usage(storage_log_raw),
            "trace": parse_usage(storage_trace_raw),
        }

        # =========================================================
        # 10. CPU cores / threads
        #
        # CPU percentage itself is graphical on this screen.
        # Do not fabricate a percentage.
        # =========================================================
        result["cpu"] = {
            "cores_threads": read_text(
                "wnd[0]/usr/txtHDB_OVERVIEW-CPU1_COPY"
            )
        }

        # =========================================================
        # 11. Current Alerts
        # =========================================================
        for suffix in ("1", "2", "3", "4"):
            alert = read_text(
                f"wnd[0]/usr/txtHDB_OVERVIEW-ALERTS_{suffix}"
            )

            if alert:
                result["alerts"].append(alert)

        # =========================================================
        # 12. Status
        #
        # We do not invent memory/storage thresholds here.
        # Operational state is directly evaluated.
        # =========================================================
        state = result["operational_state"].lower()

        if any(
            word in state
            for word in (
                "stopped",
                "failed",
                "error",
                "not operational",
            )
        ):
            result["status"] = "CRITICAL"

        elif any(
            word in state
            for word in (
                "partially",
                "degraded",
                "warning",
            )
        ):
            result["status"] = "WARNING"

        else:
            result["status"] = "OK"

        # =========================================================
        # 13. Summary
        # =========================================================
        result["summary"] = {
            "operational_state": result["operational_state"],

            "memory_mdc": memory_mdc_raw,
            "memory_tenant": memory_tenant_raw,

            "storage_data": storage_data_raw,
            "storage_log": storage_log_raw,
            "storage_trace": storage_trace_raw,

            "cpu_cores_threads": result["cpu"]["cores_threads"],

            "database_name": result["database_name"],
            "host": result["host"],
            "instance_name": result["instance_name"],

            "alerts": result["alerts"],
        }

        return result

    except Exception as exc:
        result["extraction_method"] = (
            "sap_gui_structured_error"
        )
        result["extraction_error"] = str(exc)

        try:
            if result["screenshot"] is None:
                result["screenshot"] = capture_screenshot()
        except Exception:
            pass

        return result
    
def action_db12(session, capture_screenshot):
    """
    DB12 - Backup Catalog.

    Reads the actual SAP GUI Backup Catalog ALV and determines
    the most recent backup execution using SYS_END_TIME.

    Multiple ALV rows can belong to the same backup execution
    because HANA reports individual topology/volume entries.
    Therefore rows are grouped by the backup execution timestamp.

    Primary fields:
        SYS_START_TIME
        SYS_END_TIME
        ENTRY_TYPE_NAME
        STATE_NAME
        DURATION
        MESSAGE
        BACKUP_SIZE
        COMPRESSED_SIZE
    """

    import datetime
    import time

    result = {
        "backup_count": 0,
        "successful_backups": 0,
        "failed_backups": 0,
        "backups": [],
        "latest_backup": None,
        "extraction_method": "sap_gui_alv",
        "screenshot": None,
    }

    grid_id = (
        "wnd[0]/usr/"
        "cntlBACKUPCAT_ALV_CONTAINER/"
        "shellcont/shell"
    )

    def parse_datetime(value):
        if not value:
            return None

        text = str(value).strip()

        for fmt in (
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.datetime.strptime(text, fmt)
            except Exception:
                continue

        return None

    def read_alv(grid):
        rows = []

        try:
            row_count = int(grid.RowCount)
        except Exception:
            row_count = 0

        try:
            column_count = int(grid.ColumnCount)
        except Exception:
            column_count = 0

        columns = []

        for i in range(column_count):
            try:
                column = str(grid.ColumnOrder(i))
                if column:
                    columns.append(column)
            except Exception:
                continue

        for row_index in range(row_count):
            row = {}

            for column in columns:
                try:
                    value = grid.GetCellValue(
                        row_index,
                        column,
                    )

                    row[column] = (
                        ""
                        if value is None
                        else str(value).strip()
                    )

                except Exception:
                    row[column] = ""

            if row:
                rows.append(row)

        return rows, columns

    try:
        # =========================================================
        # 1. Navigate to DB12
        # =========================================================
        try:
            session.findById("wnd[0]").maximize()
        except Exception:
            pass

        session.findById(
            "wnd[0]/tbar[0]/okcd"
        ).text = "/NDB12"

        session.findById("wnd[0]").sendVKey(0)

        time.sleep(3)

        # =========================================================
        # 2. Locate actual Backup Catalog ALV
        # =========================================================
        try:
            grid = session.findById(grid_id)
        except Exception as exc:
            result["extraction_method"] = (
                "sap_gui_alv_error"
            )
            result["extraction_error"] = (
                "DB12 Backup Catalog ALV not found: "
                + str(exc)
            )

            try:
                result["screenshot"] = capture_screenshot()
            except Exception:
                pass

            return result

        # =========================================================
        # 3. Focus on SYS_END_TIME as recorded by VBS
        # =========================================================
        try:
            grid.currentCellColumn = "SYS_END_TIME"
        except Exception:
            pass

        # =========================================================
        # 4. Capture evidence
        # =========================================================
        try:
            result["screenshot"] = capture_screenshot()
        except Exception:
            result["screenshot"] = None

        # =========================================================
        # 5. Read complete ALV
        # =========================================================
        rows, columns = read_alv(grid)

        result["alv_row_count"] = len(rows)
        result["alv_columns"] = columns

        if not rows:
            result["backup_count"] = 0
            result["successful_backups"] = 0
            result["failed_backups"] = 0
            result["extraction_method"] = "sap_gui_alv"
            result["status"] = "OK"
            result["summary"] = "No backup records found."
            return result

        # =========================================================
        # 6. Group rows belonging to the same backup execution
        #
        # Example:
        # rows 0,1,2:
        #   start = 11.09.2026 09:00:28
        #   end   = 11.09.2026 09:59:58
        #
        # They are one backup execution.
        # =========================================================
        groups = {}

        for row in rows:
            start = row.get("SYS_START_TIME", "")
            end = row.get("SYS_END_TIME", "")

            # Prefer the combination of start/end.
            key = (
                start,
                end,
            )

            if key not in groups:
                groups[key] = []

            groups[key].append(row)

        # =========================================================
        # 7. Build one logical backup record per execution
        # =========================================================
        backups = []

        for (start, end), group_rows in groups.items():

            start_dt = parse_datetime(start)
            end_dt = parse_datetime(end)

            states = [
                row.get("STATE_NAME", "").strip()
                for row in group_rows
                if row.get("STATE_NAME", "").strip()
            ]

            entry_types = [
                row.get("ENTRY_TYPE_NAME", "").strip()
                for row in group_rows
                if row.get("ENTRY_TYPE_NAME", "").strip()
            ]

            messages = [
                row.get("MESSAGE", "").strip()
                for row in group_rows
                if row.get("MESSAGE", "").strip()
            ]

            durations = [
                row.get("DURATION", "").strip()
                for row in group_rows
                if row.get("DURATION", "").strip()
            ]

            # Determine logical status.
            normalized_states = [
                state.lower()
                for state in states
            ]

            if any(
                state in (
                    "failed",
                    "error",
                    "cancelled",
                    "canceled",
                )
                for state in normalized_states
            ):
                status = "failed"

            elif states and all(
                state == "successful"
                for state in normalized_states
            ):
                status = "successful"

            elif states:
                status = states[0]

            else:
                status = ""

            backup = {
                "start_time": start,
                "end_time": end,
                "entry_type": (
                    entry_types[0]
                    if entry_types
                    else ""
                ),
                "status": status,
                "duration": (
                    durations[0]
                    if durations
                    else ""
                ),
                "message": (
                    messages[0]
                    if messages
                    else ""
                ),
                "row_count": len(group_rows),
                "rows": group_rows,
            }

            if start_dt is not None:
                backup["_start_dt"] = start_dt

            if end_dt is not None:
                backup["_end_dt"] = end_dt

            backups.append(backup)

        # =========================================================
        # 8. Sort by SYS_END_TIME descending
        # =========================================================
        backups.sort(
            key=lambda item: (
                item.get(
                    "_end_dt",
                    datetime.datetime.min,
                )
            ),
            reverse=True,
        )

        # Remove internal datetime values before returning.
        for backup in backups:
            backup.pop("_start_dt", None)
            backup.pop("_end_dt", None)

        result["backups"] = backups
        result["backup_count"] = len(backups)

        # =========================================================
        # 9. Count successful / failed backup executions
        # =========================================================
        result["successful_backups"] = sum(
            1
            for backup in backups
            if str(
                backup.get("status", "")
            ).lower()
            == "successful"
        )

        result["failed_backups"] = sum(
            1
            for backup in backups
            if str(
                backup.get("status", "")
            ).lower()
            in (
                "failed",
                "error",
                "cancelled",
                "canceled",
            )
        )

        # =========================================================
        # 10. Most recent backup
        # =========================================================
        if backups:
            latest = backups[0]

            result["latest_backup"] = {
                "start_time": latest.get(
                    "start_time",
                    "",
                ),
                "end_time": latest.get(
                    "end_time",
                    "",
                ),
                "status": latest.get(
                    "status",
                    "",
                ),
                "entry_type": latest.get(
                    "entry_type",
                    "",
                ),
                "duration": latest.get(
                    "duration",
                    "",
                ),
                "message": latest.get(
                    "message",
                    "",
                ),
                "row_count": latest.get(
                    "row_count",
                    0,
                ),
            }

        # =========================================================
        # 11. Final status
        # =========================================================
        if result["latest_backup"]:
            latest_status = str(
                result["latest_backup"].get(
                    "status",
                    "",
                )
            ).lower()

            if latest_status in (
                "failed",
                "error",
                "cancelled",
                "canceled",
            ):
                result["status"] = "CRITICAL"

            elif latest_status == "successful":
                result["status"] = "OK"

            else:
                result["status"] = "WARNING"

        else:
            result["status"] = "OK"

        # =========================================================
        # 12. Summary
        # =========================================================
        latest = result["latest_backup"] or {}

        result["summary"] = {
            "latest_backup_status": latest.get(
                "status",
                "",
            ),
            "latest_backup_start_time": latest.get(
                "start_time",
                "",
            ),
            "latest_backup_end_time": latest.get(
                "end_time",
                "",
            ),
            "latest_backup_type": latest.get(
                "entry_type",
                "",
            ),
            "latest_backup_duration": latest.get(
                "duration",
                "",
            ),
            "backup_count": result["backup_count"],
            "successful_backups": result[
                "successful_backups"
            ],
            "failed_backups": result[
                "failed_backups"
            ],
        }

        result["extraction_method"] = "sap_gui_alv"

        return result

    except Exception as exc:
        result["extraction_method"] = (
            "sap_gui_alv_error"
        )
        result["extraction_error"] = str(exc)

        try:
            if result["screenshot"] is None:
                result["screenshot"] = capture_screenshot()
        except Exception:
            pass

        return result


def action_scot(session, capture_screenshot):
    """
    SCOT - SMTP Mail Port

    Navigates to:
        SCOT -> SMTP -> Mail_Port

    Extracts Mail Port directly from SAP TableTree.
    Screenshot is always retained as evidence.
    """

    import time

    try:
        # ---------------------------------------------------------
        # 1. Open SCOT
        # ---------------------------------------------------------
        session.findById("wnd[0]/tbar[0]/okcd").text = "/NSCOT"
        session.findById("wnd[0]").sendVKey(0)
        time.sleep(2)

        # ---------------------------------------------------------
        # 2. Locate SMTP TableTree
        # ---------------------------------------------------------
        tree_id = (
            "wnd[0]/usr/subCONTENT:SAPLSBCS_ADM:0104/"
            "subSUB_CONTENT:SAPLSBCS_NODES:0100/"
            "cntlSMTP_NODES_COLUMN_TREE_CONT/"
            "shellcont/shell"
        )

        tree = session.findById(tree_id)

        # ---------------------------------------------------------
        # 3. Select SMTP -> Mail_Port
        # ---------------------------------------------------------
        tree.selectItem("SMTP", "Mail_Port")
        tree.ensureVisibleHorizontalItem("SMTP", "Mail_Port")
        time.sleep(0.5)

        # ---------------------------------------------------------
        # 4. Structured extraction
        # ---------------------------------------------------------
        raw_mail_port = tree.GetItemText(
            "SMTP",
            "Mail_Port"
        )

        mail_port = str(raw_mail_port or "").strip()

        # Convert numeric port to integer where possible
        try:
            mail_port_value = int(mail_port)
        except (TypeError, ValueError):
            mail_port_value = mail_port

        # ---------------------------------------------------------
        # 5. Screenshot evidence
        # ---------------------------------------------------------
        screenshot = None

        try:
            screenshot = capture_screenshot(session, "SCOT")
        except Exception as exc:
            print(f"SCOT: screenshot failed: {exc}")

        # ---------------------------------------------------------
        # 6. Status
        # ---------------------------------------------------------
        if mail_port:
            status = "OK"
            summary = f"SCOT SMTP Mail Port is {mail_port}."
        else:
            status = "WARNING"
            summary = "SCOT SMTP Mail Port could not be read."

        return {
            "status": status,
            "summary": summary,

            "mail_port": mail_port_value,
            "mail_port_raw": mail_port,

            "smtp": {
                "mail_port": mail_port_value,
                "mail_port_raw": mail_port,
            },

            "extraction_method": "sap_gui_tabletree",

            "screenshot": screenshot,
        }

    except Exception as exc:
        print(f"SCOT extraction failed: {exc}")

        return {
            "status": "CRITICAL",
            "summary": f"SCOT extraction failed: {exc}",

            "mail_port": None,
            "mail_port_raw": "",

            "smtp": {
                "mail_port": None,
                "mail_port_raw": "",
            },

            "extraction_method": "sap_gui_tabletree",

            "screenshot": None,
        }


def action_sm12(session, capture):
    """
    SM12 supports different SAP GUI variants depending on the
    SAP release/system configuration.

    Variant 1:
        Classic "Select Lock Entries"

    Variant 2:
        "Enqueue Administration"

    Try the classic recorded flow first. If its controls are not
    available, fall back to the Enqueue Administration flow.
    """

    from sap_gui.ocr_extractor import run_ocr
    import re

    # ============================================================
    # Variant 1: Classic "Select Lock Entries"
    # ============================================================
    try:
        log.info("SM12: trying classic 'Select Lock Entries' variant.")

        goto_tcode(session, "SM12")

        # Blank username = all users
        session.findById(
            "wnd[0]/usr/txtSEQG3-GUNAME"
        ).text = ""

        # Execute
        session.findById(
            "wnd[0]/tbar[1]/btn[8]"
        ).press()

        wait_until_not_busy(session)

        path = capture()

        row_count = get_grid_row_count(
            session,
            "wnd[0]/usr/cntlGRID1/shellcont/"
            "shell/shellcont[1]/shell",
        )

        log.info(
            "SM12: classic 'Select Lock Entries' variant "
            "executed successfully."
        )

        result = {
            "sm12_variant": "select_lock_entries",
        }

        if row_count is not None:
            result["lock_count"] = row_count

        return result

    except Exception as classic_error:
        log.warning(
            "SM12: classic variant unavailable/failed: %s: %s",
            type(classic_error).__name__,
            classic_error,
        )

    # ============================================================
    # Variant 2: Enqueue Administration
    # ============================================================
    log.info("SM12: trying 'Enqueue Administration' variant.")

    # SAP GUI command field
    okcd = session.findById(
        "wnd[0]/tbar[0]/okcd"
    )

    log.info("SM12: command field found for Enqueue Administration.")

    okcd.text = "/NSM12"

    log.info("SM12: entering /NSM12.")

    session.findById(
        "wnd[0]"
    ).sendVKey(0)

    wait_until_not_busy(session)

    log.info("SM12: /NSM12 submitted.")

    # ------------------------------------------------------------
    # Enqueue Administration fields
    # ------------------------------------------------------------

    username_field = session.findById(
        "wnd[0]/usr/"
        "subAREA_TOP:RS_ENQ_ADMIN:0111/"
        "ctxtENQ_LOCK_FILTER-USERNAME"
    )

    log.info(
        "SM12: Enqueue Administration USERNAME field found."
    )

    username_field.text = "*"

    log.info(
        "SM12: Enqueue Administration USERNAME set to '*'."
    )

    limit_field = session.findById(
        "wnd[0]/usr/"
        "subAREA_TOP:RS_ENQ_ADMIN:0111/"
        "txtENQ_LOCK_FILTER-LIMIT"
    )

    log.info(
        "SM12: Enqueue Administration LIMIT field found."
    )

    limit_field.text = ""

    log.info(
        "SM12: Enqueue Administration LIMIT cleared."
    )

    load_button = session.findById(
        "wnd[0]/usr/"
        "subAREA_TOP:RS_ENQ_ADMIN:0111/"
        "btnLOAD"
    )

    log.info(
        "SM12: Enqueue Administration LOAD button found."
    )

    load_button.press()

    log.info(
        "SM12: Enqueue Administration LOAD pressed."
    )

    wait_until_not_busy(session)

    path = capture()

    log.info(
        "SM12: Enqueue Administration variant executed "
        "successfully. Screenshot=%s",
        path,
    )

    result = {
        "sm12_variant": "enqueue_administration",
    }

    # ============================================================
    # Extract lock count from Enqueue Administration screenshot
    # ============================================================

    if path:
        try:
            text = run_ocr(path)

            # Enqueue Administration displays the result as:
            #
            #     Lock Table (0)
            #
            #     Lock Table (1)
            #
            #     Lock Table (25)
            #
            # The actual lock count is contained in the
            # "Lock Table (...)" result, not immediately after
            # the "Number Of Locks" search-criteria label.

            match = re.search(
                r"Lock\s+Table\s*\(\s*([0-9]+)\s*\)",
                text,
                re.IGNORECASE,
            )

            if match:
                result["lock_count"] = int(match.group(1))

                log.info(
                    "SM12: Lock Table count = %s",
                    result["lock_count"],
                )
            else:
                log.warning(
                    "SM12: Lock Table count was not detected by OCR."
                )

        except Exception as ocr_error:
            log.warning(
                "SM12: OCR extraction failed: %s: %s",
                type(ocr_error).__name__,
                ocr_error,
            )

    # IMPORTANT:
    # The Enqueue Administration branch must return the result.
    # Without this return, Python returns None and the collector
    # converts it to {}, losing lock_count from evidence.
    return result


def action_sm13(session, capture_screenshot):
    """
    SM13 - Update Requests.

    Structured extraction from the validated SAP GridView.

    Validated grid:
        wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell

    Columns:
        VBMANDT, VBUSR, DATUM, ZEIT, VBTCODE,
        INFO1 ... INFO8, STATUS
    """

    screenshot = None

    columns = [
        "VBMANDT",
        "VBUSR",
        "DATUM",
        "ZEIT",
        "VBTCODE",
        "INFO1",
        "INFO2",
        "INFO3",
        "INFO4",
        "INFO5",
        "INFO6",
        "INFO7",
        "INFO8",
        "STATUS",
    ]

    try:
        # ---------------------------------------------------------
        # 1. Open SM13
        # ---------------------------------------------------------
        session.findById("wnd[0]").maximize()

        session.findById(
            "wnd[0]/tbar[0]/okcd"
        ).text = "/NSM13"

        session.findById("wnd[0]").sendVKey(0)

        time.sleep(2)

        # ---------------------------------------------------------
        # 2. Execute SM13 selection
        # ---------------------------------------------------------
        try:
            session.findById("wnd[0]").sendVKey(8)
        except Exception:
            # Fallback to Execute toolbar button
            try:
                session.findById("wnd[0]/tbar[1]/btn[8]").press()
            except Exception:
                pass

        time.sleep(2)

        # ---------------------------------------------------------
        # 3. Wait for GRID1 container
        # ---------------------------------------------------------
        grid_container = None

        for _ in range(20):
            try:
                grid_container = session.findById(
                    "wnd[0]/usr/cntlGRID1"
                )

                if grid_container is not None:
                    break

            except Exception:
                time.sleep(0.5)

        if grid_container is None:
            raise RuntimeError(
                "SM13 GRID1 container was not created."
            )

        # ---------------------------------------------------------
        # 4. Locate the actual GridView dynamically
        #
        # Expected:
        # GRID1
        #   -> shellcont
        #      -> shell (GuiSplitterShell)
        #         -> shellcont[1]
        #            -> shell (GuiShell / GridView)
        # ---------------------------------------------------------
        grid = None

        try:
            shellcont = grid_container.Children(0)
            splitter = shellcont.Children(0)

            for pane_index in range(splitter.Children.Count):

                pane = splitter.Children(pane_index)

                for child_index in range(pane.Children.Count):

                    child = pane.Children(child_index)

                    try:
                        if str(child.Type) == "GuiShell":
                            grid = child
                            break
                    except Exception:
                        continue

                if grid is not None:
                    break

        except Exception:
            grid = None

        # ---------------------------------------------------------
        # 5. Fallback to exact validated ID
        # ---------------------------------------------------------
        if grid is None:

            exact_grid_id = (
                "wnd[0]/usr/cntlGRID1/shellcont/shell/"
                "shellcont[1]/shell"
            )

            for _ in range(10):
                try:
                    grid = session.findById(exact_grid_id)

                    if grid is not None:
                        break

                except Exception:
                    time.sleep(0.5)

        if grid is None:
            raise RuntimeError(
                "SM13 result GridView could not be found."
            )

        # ---------------------------------------------------------
        # 6. Read grid metadata
        # ---------------------------------------------------------
        row_count = int(grid.RowCount)
        column_count = int(grid.ColumnCount)

        # ---------------------------------------------------------
        # 7. Screenshot ALWAYS retained
        # ---------------------------------------------------------
        try:
            screenshot = capture_screenshot()
        except Exception:
            screenshot = None

        # ---------------------------------------------------------
        # 8. Read structured rows
        # ---------------------------------------------------------
        update_records = []

        for row in range(row_count):

            record = {}

            for column in columns:

                try:
                    value = grid.GetCellValue(
                        row,
                        column
                    )

                    record[column] = (
                        ""
                        if value is None
                        else str(value).strip()
                    )

                except Exception:
                    record[column] = ""

            update_records.append(record)

        # ---------------------------------------------------------
        # 9. Determine status
        # ---------------------------------------------------------
        status_values = [
            str(record.get("STATUS", "")).strip().upper()
            for record in update_records
            if str(record.get("STATUS", "")).strip()
        ]

        error_statuses = {
            "ERROR",
            "FAILED",
            "FAIL",
            "CANCELLED",
            "CANCELED",
            "ABORTED",
        }

        warning_statuses = {
            "WARNING",
            "WARN",
        }

        if any(
            value in error_statuses
            for value in status_values
        ):
            overall_status = "CRITICAL"

        elif any(
            value in warning_statuses
            for value in status_values
        ):
            overall_status = "WARNING"

        else:
            overall_status = "OK"

        # ---------------------------------------------------------
        # 10. Summary
        # ---------------------------------------------------------
        if row_count == 0:
            message = "No update records found in SM13."
        else:
            message = (
                f"{row_count} update record(s) found in SM13."
            )

        summary = {
            "update_count": row_count,
            "status_values": sorted(set(status_values)),
            "message": message,
        }

        # ---------------------------------------------------------
        # 11. Final structured result
        # ---------------------------------------------------------
        return {
            "update_count": row_count,
            "update_records": update_records,

            "updates": update_records,
            "update_summary": message,

            "extraction_method": "sap_gui_alv",

            "alv_row_count": row_count,
            "alv_column_count": column_count,
            "alv_columns": columns,

            "screenshot": screenshot,

            "status": overall_status,
            "summary": summary,
        }

    except Exception as exc:

        # Try to retain screenshot on failure
        try:
            screenshot = capture_screenshot()
        except Exception:
            screenshot = None

        return {
            "update_count": 0,
            "update_records": [],
            "updates": [],

            "update_summary": (
                "SM13 structured extraction failed."
            ),

            "extraction_method": "sap_gui_alv",

            "alv_row_count": 0,
            "alv_column_count": 14,
            "alv_columns": columns,

            "screenshot": screenshot,

            "status": "ERROR",
            "error": str(exc),

            "summary": {
                "update_count": 0,
                "status_values": [],
                "message": (
                    "SM13 structured extraction failed."
                ),
            },
        }

def action_sm21(session, capture_screenshot):
    """
    SM21 - SAP System Log.

    Extracts system-log entries from the visible ALV/grid where possible
    and preserves screenshots for PDF evidence.
    """
    result = {
        "log_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "logs": [],
        "extraction_method": "sap_gui",
    }

    try:
        session.findById("wnd[0]/tbar[0]/okcd").text = "/NSM21"
        session.findById("wnd[0]").sendVKey(0)
        time.sleep(3)

        # Execute today's system-log selection.
        for control_id in (
            "wnd[0]/tbar[1]/btn[8]",
            "wnd[0]/tbar[1]/btn[5]",
        ):
            try:
                session.findById(control_id).press()
                time.sleep(3)
                break
            except Exception:
                continue

        # Always capture the resulting screen.
        try:
            result["screenshot"] = capture_screenshot()
        except Exception:
            result["screenshot"] = None

        # ---------------------------------------------------------
        # 1. Try structured ALV extraction
        # ---------------------------------------------------------
        grid_candidates = (
            "wnd[0]/usr/cntlGRID1/shellcont/shell",
            "wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell",
            "wnd[0]/usr/cntlALV_CONTAINER/shellcont/shell",
            "wnd[0]/usr/cntlCONTAINER/shellcont/shell",
        )

        grid = None

        for grid_id in grid_candidates:
            try:
                candidate = session.findById(grid_id)

                row_count = int(candidate.RowCount)
                column_count = int(candidate.ColumnCount)

                if row_count >= 0 and column_count > 0:
                    grid = candidate
                    break

            except Exception:
                continue

        if grid is not None:
            rows = []

            try:
                row_count = int(grid.RowCount)
            except Exception:
                row_count = 0

            columns = []

            try:
                column_count = int(grid.ColumnCount)

                for i in range(column_count):
                    try:
                        columns.append(
                            str(grid.ColumnOrder(i))
                        )
                    except Exception:
                        pass

            except Exception:
                pass

            for row_index in range(row_count):
                row_data = {}

                for column in columns:
                    try:
                        value = grid.GetCellValue(
                            row_index,
                            column
                        )

                        if value is not None:
                            row_data[column] = str(
                                value
                            ).strip()

                    except Exception:
                        pass

                if row_data:
                    rows.append(row_data)

            result["logs"] = rows
            result["log_count"] = len(rows)
            result["extraction_method"] = "sap_gui_alv"

            # -----------------------------------------------------
            # Analyze severity
            # -----------------------------------------------------
            for row in rows:
                row_text = " ".join(
                    str(value).strip().lower()
                    for value in row.values()
                )

                if any(
                    keyword in row_text
                    for keyword in (
                        "error",
                        "critical",
                        "fatal",
                        "failed",
                    )
                ):
                    result["error_count"] += 1

                elif any(
                    keyword in row_text
                    for keyword in (
                        "warning",
                        "warn",
                    )
                ):
                    result["warning_count"] += 1

            return result

        # ---------------------------------------------------------
        # 2. GUI text fallback
        # ---------------------------------------------------------
        texts = []

        try:
            for child in session.findById("wnd[0]/usr").Children:
                try:
                    text = str(
                        getattr(child, "text", "") or ""
                    ).strip()

                    if text:
                        texts.append(text)

                except Exception:
                    pass

        except Exception:
            pass

        joined = " ".join(texts)
        normalized = joined.lower()

        # Common SAP no-data messages.
        if any(
            phrase in normalized
            for phrase in (
                "no entries",
                "no data",
                "nothing was selected",
                "no records",
                "no system log",
            )
        ):
            result["log_count"] = 0
            result["extraction_method"] = "sap_gui_status"
            result["status_message"] = joined
            result["gui_text"] = texts
            return result

        # ---------------------------------------------------------
        # 3. Label-based fallback
        # ---------------------------------------------------------
        rows = {}

        try:
            for child in session.findById("wnd[0]/usr").Children:
                try:
                    child_id = str(child.Id)

                    match = re.search(
                        r"/lbl\[(\d+),(\d+)\]",
                        child_id
                    )

                    if not match:
                        continue

                    column = int(match.group(1))
                    row_number = int(match.group(2))

                    text = str(
                        getattr(child, "text", "") or ""
                    ).strip()

                    if text:
                        rows.setdefault(
                            row_number,
                            {}
                        )[column] = text

                except Exception:
                    pass

        except Exception:
            pass

        extracted_rows = []

        for row_number in sorted(rows):
            row = rows[row_number]

            values = [
                str(value).strip()
                for value in row.values()
                if str(value).strip()
            ]

            if not values:
                continue

            row_text = " ".join(values).lower()

            # Ignore obvious column headers.
            if row_text in {
                "date",
                "time",
                "server",
                "user",
                "message",
                "type",
                "system log",
            }:
                continue

            extracted_rows.append(row)

            if any(
                keyword in row_text
                for keyword in (
                    "error",
                    "critical",
                    "fatal",
                    "failed",
                )
            ):
                result["error_count"] += 1

            elif any(
                keyword in row_text
                for keyword in (
                    "warning",
                    "warn",
                )
            ):
                result["warning_count"] += 1

        result["logs"] = extracted_rows
        result["log_count"] = len(extracted_rows)
        result["extraction_method"] = "sap_gui_labels"

        if texts:
            result["gui_text"] = texts

        return result

    except Exception as exc:
        try:
            result["screenshot"] = capture_screenshot()
        except Exception:
            pass

        result["error"] = str(exc)
        result["extraction_method"] = "sap_gui_error"

        return result


def action_sm37(session, capture_screenshot=None):
    """
    SM37 - Active background jobs.

    Selection logic follows the recorded SAP GUI VBS:
      - Job Name: *
      - User Name: *
      - Scheduled: unchecked
      - Finished: unchecked
      - Canceled: unchecked
      - Execute via toolbar button

    Result list is extracted from GuiLabel controls.
    """

    import time

    screenshot = None

    try:
        session.findById("wnd[0]").maximize()

        # Open SM37.
        session.findById(
            "wnd[0]/tbar[0]/okcd"
        ).Text = "/NSM37"

        session.findById("wnd[0]").sendVKey(0)

        time.sleep(1)

        # Follow the recorded VBS exactly.
        session.findById(
            "wnd[0]/usr/chkBTCH2170-SCHEDUL"
        ).Selected = False

        session.findById(
            "wnd[0]/usr/chkBTCH2170-FINISHED"
        ).Selected = False

        session.findById(
            "wnd[0]/usr/chkBTCH2170-ABORTED"
        ).Selected = False

        session.findById(
            "wnd[0]/usr/txtBTCH2170-USERNAME"
        ).Text = "*"

        # Job name should remain the recorded/default "*".
        try:
            session.findById(
                "wnd[0]/usr/txtBTCH2170-JOBNAME"
            ).Text = "*"
        except Exception:
            pass

        # Focus exactly as in VBS.
        session.findById(
            "wnd[0]/usr/chkBTCH2170-ABORTED"
        ).SetFocus()

        # Execute exactly as recorded.
        session.findById(
            "wnd[0]/tbar[1]/btn[8]"
        ).Press()

        time.sleep(1.5)

        # Capture evidence after execution.
        if capture_screenshot:
            try:
                screenshot = capture_screenshot("after_execution")
            except Exception:
                screenshot = None

        # Open SM37 List Status and capture the popup that shows the
        # authoritative "Records passed" count. Close it before reading
        # the underlying result list.
        records_passed = None
        try:
            records_passed = _read_list_status_records_passed(session)
            if capture_screenshot and records_passed is not None:
                capture_screenshot("records_passed")
        except Exception as exc:
            log.warning("SM37: List Status read skipped: %s", exc)
        finally:
            try:
                session.findById("wnd[1]").sendVKey(12)
                time.sleep(0.3)
            except Exception:
                pass

        if records_passed is None:
            log.info("SM37: 'Records passed' unavailable on this screen "
                     "(no result list to summarise); using the ALV row count.")
        else:
            log.info("SM37: Records passed = %s", records_passed)

        usr = session.findById("wnd[0]/usr")

        # Collect GuiLabel controls.
        labels = {}

        for i in range(usr.Children.Count):
            try:
                control = usr.Children(i)

                if control.Type != "GuiLabel":
                    continue

                control_id = str(control.Id)

                if "/lbl[" not in control_id:
                    continue

                coords = control_id.rsplit("/lbl[", 1)[1].rstrip("]")
                parts = coords.split(",")

                if len(parts) != 2:
                    continue

                x = int(parts[0])
                y = int(parts[1])

                labels[(x, y)] = str(
                    getattr(control, "Text", "") or ""
                ).strip()

            except Exception:
                continue

        jobs = []

        # Result table begins at screen row 13.
        row = 13

        while row < 500:

            job_name = labels.get((4, row), "").strip()

            if not job_name:
                row += 1
                continue

            # Summary row.
            if job_name in ("Summary", "*"):
                break

            jobs.append({
                "job_name": job_name,
                "created_by": labels.get((46, row), "").strip(),
                "status": labels.get((59, row), "").strip(),
                "start_date": labels.get((75, row), "").strip(),
                "start_time": labels.get((86, row), "").strip(),
                "duration": labels.get((95, row), "").strip(),
                "delay_seconds": labels.get((104, row), "").strip(),
                "end_date": labels.get((117, row), "").strip(),
                "end_time": labels.get((128, row), "").strip(),
                "client": labels.get((137, row), "").strip(),
                "reason_for_delay": labels.get((141, row), "").strip(),
            })

            row += 1

        active_jobs = [
            job for job in jobs
            if job.get("status", "").strip().lower() == "active"
        ]

        return {
            "status": "OK",
            "summary": (
                f"{len(active_jobs)} active job(s) found in SM37."
                if active_jobs
                else "No active jobs found in SM37."
            ),

            "active_job_count": len(active_jobs),
            "records_passed": records_passed,
            "job_count": len(jobs),

            "active_jobs": active_jobs,
            "jobs": jobs,

            "extraction_method": "sap_gui_structured",

            "screenshot": screenshot,

            "alv_row_count": len(jobs),
            "alv_column_count": 11,

            "columns": [
                "JOB_NAME",
                "CREATED_BY",
                "STATUS",
                "START_DATE",
                "START_TIME",
                "DURATION",
                "DELAY_SECONDS",
                "END_DATE",
                "END_TIME",
                "CLIENT",
                "REASON_FOR_DELAY",
            ],

            "summary_data": {
                "active_job_count": len(active_jobs),
                "total_job_count": len(jobs),
                "active_jobs": active_jobs,
            },
        }

    except Exception as exc:

        if capture_screenshot:
            try:
                screenshot = capture_screenshot()
            except Exception:
                screenshot = None

        return {
            "status": "WARNING",
            "summary": f"SM37 structured extraction failed: {exc}",
            "active_job_count": 0,
            "records_passed": None,
            "job_count": 0,
            "active_jobs": [],
            "jobs": [],
            "extraction_method": "sap_gui_structured_failed",
            "screenshot": screenshot,
            "error": str(exc),
        }

def action_sm37_cancelled(session, screenshot_fn=None):
    """
    SM37 - Cancelled Jobs

    Date behavior:
      - From Date = yesterday
      - To Date   = leave SAP's existing/default value unchanged

    Authoritative cancelled-job count:
      Shift+F7 (sendVKey 19) -> List Status -> Records passed

    No individual cancelled-job row extraction is performed.
    """

    import re
    import time
    from datetime import datetime, timedelta

    screenshot = None

    try:
        # ---------------------------------------------------------
        # 1. Open SM37
        # ---------------------------------------------------------
        session.findById("wnd[0]/tbar[0]/okcd").text = "/NSM37"
        session.findById("wnd[0]").sendVKey(0)
        time.sleep(1)

        # ---------------------------------------------------------
        # 2. Configure SM37 selection
        # ---------------------------------------------------------
        # Username = all users
        try:
            session.findById("wnd[0]/usr/txtBTCH2170-USERNAME").text = "*"
        except Exception:
            pass

        # Cancelled / Aborted = selected
        try:
            session.findById("wnd[0]/usr/chkBTCH2170-ABORTED").selected = True
        except Exception:
            pass

        # Other job statuses = unchecked
        for control_id in [
            "chkBTCH2170-SCHEDUL",
            "chkBTCH2170-FINISHED",
            "chkBTCH2170-PRELIM",
            "chkBTCH2170-READY",
            "chkBTCH2170-RUNNING",
        ]:
            try:
                session.findById(f"wnd[0]/usr/{control_id}").selected = False
            except Exception:
                pass

        # ---------------------------------------------------------
        # 3. FROM DATE = YESTERDAY
        #    TO DATE is deliberately NOT touched.
        # ---------------------------------------------------------
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")

        # Standard SM37 date field
        from_date_ids = [
            "wnd[0]/usr/ctxtBTCH2170-FROM_DATE",
            "wnd[0]/usr/ctxtBTCH2170-STRTDATE",
            "wnd[0]/usr/ctxtBTCH2170-SDLSTRTDT",
        ]

        from_date_set = False

        for control_id in from_date_ids:
            try:
                field = session.findById(control_id)
                field.text = yesterday
                from_date_set = True
                print(f"SM37 cancelled: From Date set to {yesterday} ({control_id})")
                break
            except Exception:
                continue

        if not from_date_set:
            print(
                "SM37 cancelled: WARNING - could not locate From Date field; "
                "To Date was left unchanged."
            )

        # ---------------------------------------------------------
        # 4. Execute
        # ---------------------------------------------------------
        session.findById("wnd[0]").sendVKey(8)
        time.sleep(2)

        # ---------------------------------------------------------
        # 5. Capture cancelled-jobs list screenshot
        # ---------------------------------------------------------
        if screenshot_fn:
            try:
                screenshot = screenshot_fn()
            except Exception as exc:
                print(f"SM37 cancelled: screenshot failed: {exc}")

        # ---------------------------------------------------------
        # 6. Shift+F7 -> List Status
        #    sendVKey(19) is confirmed as Shift+F7
        # ---------------------------------------------------------
        records_passed = _read_list_status_records_passed(session)

        if records_passed is None:
            log.warning(
                "SM37 cancelled: 'Records passed' could not be read from the "
                "List Status popup; reporting the count as unknown rather "
                "than as zero."
            )
        else:
            log.info("SM37 cancelled: Records passed = %s", records_passed)

        # Capture the List Status popup itself so the PDF contains visual
        # evidence of the authoritative "Records passed" count.
        if screenshot_fn:
            try:
                screenshot_fn("records_passed")
            except Exception as exc:
                print(f"SM37 cancelled: records-passed popup screenshot failed: {exc}")

        # ---------------------------------------------------------
        # 7. Close List Status popup
        # ---------------------------------------------------------
        try:
            session.findById("wnd[1]").sendVKey(12)
            time.sleep(0.5)
        except Exception:
            pass

        # ---------------------------------------------------------
        # 8. Return structured result
        # ---------------------------------------------------------
        if records_passed is None:
            status = "UNKNOWN"
            summary = (
                "SM37 cancelled-job count could not be read from the "
                "List Status popup."
            )
        elif records_passed > 0:
            status = "WARNING"
            summary = (
                f"{records_passed} cancelled job(s) found in SM37."
            )
        else:
            status = "OK"
            summary = "No cancelled jobs found in SM37."

        return {
            "status": status,
            "summary": summary,

            # Authoritative count from Shift+F7
            "cancelled_job_count": records_passed,
            "records_passed": records_passed,

            # No individual job extraction
            "cancelled_jobs": [],
            "jobs": [],

            "extraction_method": "sap_gui_list_status",

            "screenshot": screenshot,

            "date_filter": {
                "from_date": yesterday,
                "to_date": "unchanged",
            },
        }

    except Exception as exc:
        # Try to close popup if an unexpected error occurred
        try:
            if session.findById("wnd[1]"):
                session.findById("wnd[1]").sendVKey(12)
        except Exception:
            pass

        return {
            "status": "CRITICAL",
            "summary": f"SM37 cancelled extraction failed: {exc}",
            "cancelled_job_count": 0,
            "records_passed": 0,
            "cancelled_jobs": [],
            "jobs": [],
            "extraction_method": "sap_gui_list_status",
            "screenshot": screenshot,
            "date_filter": {
                "from_date": (
                    datetime.now() - timedelta(days=1)
                ).strftime("%d.%m.%Y"),
                "to_date": "unchanged",
            },
        }

def action_sm51(session, capture):
    """
    Collect structured SM51 application-server data through SAP GUI Scripting.

    Primary source:
        SAPGUI.GridViewCtrl.1 ALV technical columns.

    Screenshot is retained as visual evidence.
    OCR is used only as a fallback.
    """
    goto_tcode(session, "SM51")
    wait_until_not_busy(session)

    grid_id = (
        "wnd[0]/usr/cntlGRID1/shellcont/shell/"
        "shellcont[1]/shell/shellcont[1]/shell"
    )

    columns = {
        "name": "NAME",
        "host": "HOSTNAMELONG",
        "services": "SERVICES",
        "status": "STATUS",
        "shutdown_info": "SHUTDOWN_INFO",
        "load_info": "LOAD_INFO",
    }

    # ---------------------------------------------------------------
    # PRIMARY: Direct SAP ALV extraction
    # ---------------------------------------------------------------
    try:
        grid = session.findById(grid_id)

        rows = []

        for row_index in range(grid.RowCount):
            row = {}

            for field, technical_column in columns.items():
                value = grid.GetCellValue(
                    row_index,
                    technical_column,
                )

                row[field] = (
                    "" if value is None
                    else str(value).strip()
                )

            rows.append(row)

        active_instances = sum(
            1
            for row in rows
            if row.get("status", "").lower() == "active"
        )

        if rows and active_instances == len(rows):
            health_status = "OK"
        elif rows and active_instances > 0:
            health_status = "WARNING"
        elif rows:
            health_status = "CRITICAL"
        else:
            health_status = "UNKNOWN"

        result = {
            "instances_started": len(rows),
            "instance_count": len(rows),
            "active_instances": active_instances,
            "instances": rows,
            "status": health_status,
            "extraction_method": "sap_gui_alv",
        }

        log.info(
            "SM51 structured extraction: instances=%d active=%d",
            len(rows),
            active_instances,
        )

        # Always retain screenshot as audit evidence.
        path = capture()

        if path:
            result["screenshot"] = path

        return result

    except Exception as exc:
        log.warning(
            "SM51 direct ALV extraction failed; "
            "using OCR fallback: %s",
            exc,
        )

    # ---------------------------------------------------------------
    # FALLBACK: Screenshot/OCR
    # ---------------------------------------------------------------
    try:
        from sap_gui.ocr_extractor import run_ocr

        path = capture()

        if not path:
            return {}

        text = run_ocr(path)

        match = re.search(
            r"(\d+)\s*AS instance\(s\)\s*started",
            text,
            re.IGNORECASE,
        )

        if match:
            count = int(match.group(1))

            return {
                "instances_started": count,
                "instance_count": count,
                "extraction_method": "ocr_fallback",
                "screenshot": path,
            }

    except Exception as exc:
        log.warning(
            "SM51 OCR fallback failed: %s",
            exc,
        )

    return {}

def action_sm58(session, capture):
    """
    Collect SM58 tRFC monitoring data.

    Scope:
        Information section only.

    User filter:
        All users (*).

    Extracted information:
        - Number of Entries Displayed
        - Number of failed entries
        - Number of entries in execution

    Screenshot:
        Always retained for PDF evidence.
    """
    goto_tcode(session, "SM58")

    # ---------------------------------------------------------------
    # Monitor all users
    # ---------------------------------------------------------------
    session.findById(
        "wnd[0]/usr/txtBENUTZER-LOW"
    ).Text = "*"

    # ---------------------------------------------------------------
    # Execute
    # ---------------------------------------------------------------
    session.findById(
        "wnd[0]/tbar[1]/btn[8]"
    ).press()

    wait_until_not_busy(session)

    # ---------------------------------------------------------------
    # Screenshot for PDF evidence
    # ---------------------------------------------------------------
    path = capture()

    # ---------------------------------------------------------------
    # Extract SM58 Information section
    # ---------------------------------------------------------------
    entries_displayed = 0
    failed_entries = 0
    entries_in_execution = 0

    try:
        usr = session.findById("wnd[0]/usr")

        for i in range(usr.Children.Count):
            try:
                control = usr.Children(i)

                if control.Type != "GuiLabel":
                    continue

                text = (getattr(control, "Text", "") or "").strip()

                if not text:
                    continue

                control_id = control.Id

                # Number of Entries Displayed
                if "lbl[36,2]" in control_id:
                    try:
                        entries_displayed = int(text)
                    except ValueError:
                        entries_displayed = 0

                # Number of failed entries
                elif "lbl[36,3]" in control_id:
                    try:
                        failed_entries = int(text)
                    except ValueError:
                        failed_entries = 0

                # Number of entries in execution
                elif "lbl[36,4]" in control_id:
                    try:
                        entries_in_execution = int(text)
                    except ValueError:
                        entries_in_execution = 0

            except Exception:
                continue

    except Exception as exc:
        log.warning(
            "SM58: unable to extract Information section: %s",
            exc,
        )

    # ---------------------------------------------------------------
    # Determine monitoring status
    # ---------------------------------------------------------------
    if failed_entries > 0:
        trfc_status = "CRITICAL"
    elif entries_displayed > 10:
        trfc_status = "WARNING"
    else:
        # 0-10 displayed entries is within the requested normal range.
        trfc_status = "OK"

    # ---------------------------------------------------------------
    # Result
    # ---------------------------------------------------------------
    result = {
        "trfc_count": entries_displayed,
        "failed_entries": failed_entries,
        "entries_in_execution": entries_in_execution,
        "trfc_status": trfc_status,
        "information": {
            "entries_displayed": entries_displayed,
            "failed_entries": failed_entries,
            "entries_in_execution": entries_in_execution,
        },
        "extraction_method": "sap_gui_information",
    }

    if path:
        result["screenshot"] = path

    log.info(
        "SM58: entries=%s, failed=%s, execution=%s, status=%s",
        entries_displayed,
        failed_entries,
        entries_in_execution,
        trfc_status,
    )

    return result

def action_sm50(session, capture):
    """
    Collect SM50 work-process data.

    Primary source:
        SAP GUI ALV structured extraction.

    Screenshot:
        Always retained for PDF evidence.

    Returns:
        Structured work-process rows suitable for Excel/reporting.
    """
    goto_tcode(session, "SM50")
    wait_until_not_busy(session)

    # Evidence checkpoint 1: the SM50 screen immediately after execution.
    # The collector retains every capture callback result, so this becomes
    # screenshot 1 in the PDF evidence set.
    path = capture("after_execution")

    # Evidence checkpoint 2: click the SAP GUI toolbar action that displays
    # all active processes, then capture the resulting screen.  SAP GUI
    # toolbar IDs can vary by patch/theme, so identify the button by its
    # visible text/tooltip instead of hard-coding one fragile ID.
    try:
        toolbar = session.findById("wnd[0]/tbar[1]")
        active_button = None
        for i in range(int(toolbar.Children.Count)):
            try:
                obj = toolbar.Children(i)
                candidates = [
                    str(getattr(obj, "Tooltip", "") or ""),
                    str(getattr(obj, "Text", "") or ""),
                    str(getattr(obj, "Name", "") or ""),
                ]
                label = " ".join(candidates).lower()
                if "active" in label and "process" in label:
                    active_button = obj
                    break
            except Exception:
                continue

        if active_button is None:
            raise RuntimeError("SM50 'all active processes' toolbar action was not found")

        active_button.press()
        wait_until_not_busy(session)
        capture("all_active_processes")
    except Exception as exc:
        # Do not fail structured monitoring if this optional evidence
        # checkpoint is unavailable on a particular SAP GUI layout.
        log.warning("SM50: all-active-processes screenshot skipped: %s", exc)

    # Confirmed SM50 ALV grid path on the tested SAP GUI.
    grid_id = (
        "wnd[0]/usr/cntlGRID1/shellcont/shell/"
        "shellcont[1]/shell/shellcont[1]/shell"
    )

    try:
        grid = session.findById(grid_id)

        row_count = int(grid.RowCount)
        column_count = int(grid.ColumnCount)

        # ColumnOrder(index) returns the actual SAP ALV
        # column identifier, e.g. WP_INDEX, USER_NAME, CPU.
        columns = [
            grid.ColumnOrder(i)
            for i in range(column_count)
        ]

        process_rows = []

        for row in range(row_count):
            values = {}

            for column in columns:
                try:
                    values[column] = (
                        grid.GetCellValue(row, column) or ""
                    ).strip()
                except Exception as exc:
                    log.warning(
                        "SM50: failed reading row=%s column=%s: %s",
                        row,
                        column,
                        exc,
                    )
                    values[column] = ""

            process_rows.append(values)

        # Calculate useful summary values from the actual SAP values.
        running_processes = sum(
            1
            for row in process_rows
            if row.get("STATE_DISP", "").strip().lower() == "running"
        )

        waiting_processes = sum(
            1
            for row in process_rows
            if row.get("STATE_DISP", "").strip().lower()
            in {"waiting", "on hold"}
        )

        stopped_processes = sum(
            1
            for row in process_rows
            if row.get("STATE_DISP", "").strip().lower()
            in {"stopped", "ended", "error"}
        )

        result = {
            "process_count": row_count,
            "running_processes": running_processes,
            "waiting_processes": waiting_processes,
            "stopped_processes": stopped_processes,
            "process_rows": process_rows,
            "columns": columns,
            "extraction_method": "sap_gui_alv",
        }

        if path:
            result["screenshot"] = path

        log.info(
            "SM50 structured extraction: rows=%s running=%s "
            "waiting=%s stopped=%s",
            row_count,
            running_processes,
            waiting_processes,
            stopped_processes,
        )

        return result

    except Exception as exc:
        log.exception(
            "SM50 structured extraction failed: %s",
            exc,
        )

        # Keep screenshot evidence even if structured extraction fails.
        result = {
            "process_count": 0,
            "running_processes": 0,
            "waiting_processes": 0,
            "stopped_processes": 0,
            "process_rows": [],
            "extraction_method": "sap_gui_error",
            "error": str(exc),
        }

        if path:
            result["screenshot"] = path

        return result

def action_sm66(session, capture):
    """
    Collect structured SM66 global work-process data through SAP GUI
    Scripting.

    Primary source:
        SAPGUI.GridViewCtrl.1 ALV.

    Screenshot is retained as visual evidence.
    OCR is used only as a fallback.
    """
    goto_tcode(session, "SM66")
    wait_until_not_busy(session)

    # SM66 opens on the "all work processes" list, where every row on a
    # healthy system reads "Waiting". Extracting from that screen is why
    # running_processes has always come back 0 while visible_process_rows
    # came back 32, and why the screenshot evidence shows a wall of idle
    # processes rather than the work actually in flight. Press "Active Work
    # Processes" first so both the counts and the captured screen describe
    # the processes that are doing something. action_sm50 already does this;
    # SM66 never got it.
    if _press_labelled_toolbar_button(session, ("active", "process")):
        wait_until_not_busy(session)
        time.sleep(0.5)
    else:
        log.warning(
            "SM66: 'Active Work Processes' toolbar action not found; "
            "capturing the default all-processes view instead."
        )

    grid_id = (
        "wnd[0]/usr/cntlGRID1/shellcont/shell/"
        "shellcont[1]/shell/shellcont[1]/shell"
    )

    columns = {
        "server_name": "SERVER_NAME",
        "wp_index": "WP_INDEX",
        "wp_type": "WP_TYPE_DISP",
        "pid": "PID",
        "state": "STATE_DISP",
        "state_info": "STATE_INFO_DISP",
        "failures": "FAILURES",
        "sem_locked": "SEM_LOCKED",
        "sem_locking": "SEM_LOCKING",
        "cpu": "CPU",
        "elapsed_time": "ELAPSED_TIME",
        "priority": "PRIORITY_DISP",
        "wait_priority": "WAIT_FOR_PRIORITY_DISP",
        "program": "WP_PROGRAM",
        "client": "TENANT_DISP",
        "user": "USER_NAME",
        "current_action": "CURRENT_ACTION_DISP",
        "action_info": "ACTION_INFO",
    }

    # ---------------------------------------------------------------
    # PRIMARY: Direct ALV extraction
    # ---------------------------------------------------------------
    try:
        grid = session.findById(grid_id)

        rows = []

        for row_index in range(grid.RowCount):
            row = {}

            for field, technical_column in columns.items():
                try:
                    value = grid.GetCellValue(
                        row_index,
                        technical_column,
                    )
                except Exception:
                    value = ""

                row[field] = (
                    "" if value is None
                    else str(value).strip()
                )

            rows.append(row)

        running_processes = sum(
            1
            for row in rows
            if row["state"].lower() == "running"
        )

        on_hold_processes = sum(
            1
            for row in rows
            if row["state"].lower() == "on hold"
        )

        failed_processes = sum(
            1
            for row in rows
            if row["failures"]
        )

        servers = sorted({
            row["server_name"]
            for row in rows
            if row["server_name"]
        })

        # PRIV mode and long-running processes are the two states that make
        # an SM66 screen worth acting on. Counting them here lets the report
        # say "OK" on a screen that has neither, instead of leaving the whole
        # check UNKNOWN for want of a rule.
        priv_mode_processes = sum(
            1
            for row in rows
            if "priv" in f"{row['state']} {row['state_info']}".lower()
        )

        def _elapsed_seconds(value):
            parts = str(value or "").strip().split(":")
            try:
                parts = [int(p) for p in parts]
            except ValueError:
                return 0
            while len(parts) < 3:
                parts.insert(0, 0)
            return parts[-3] * 3600 + parts[-2] * 60 + parts[-1]

        long_running_processes = sum(
            1
            for row in rows
            if _elapsed_seconds(row.get("elapsed_time")) >= 600
        )

        result = {
            "process_count": len(rows),
            "visible_process_rows": len(rows),
            "running_processes": running_processes,
            "on_hold_processes": on_hold_processes,
            "failed_processes": failed_processes,
            "priv_mode_processes": priv_mode_processes,
            "long_running_processes": long_running_processes,
            "servers_affected": servers,
            "process_rows": rows,
            "extraction_method": "sap_gui_alv",
        }

        log.info(
            "SM66 structured extraction: "
            "rows=%d running=%d on_hold=%d failed=%d servers=%d",
            len(rows),
            running_processes,
            on_hold_processes,
            failed_processes,
            len(servers),
        )

        path = capture()

        if path:
            result["screenshot"] = path

        return result

    except Exception as exc:
        log.warning(
            "SM66 direct ALV extraction failed; "
            "using OCR fallback: %s",
            exc,
        )

    # ---------------------------------------------------------------
    # FALLBACK: Screenshot/OCR
    # ---------------------------------------------------------------
    try:
        from sap_gui.ocr_extractor import run_ocr, count_occurrences
        import re

        path = capture()

        if not path:
            return {}

        text = run_ocr(path)

        process_rows = len(
            re.findall(r"\d:\d{2}:\d{2}", text)
        )

        running_count = count_occurrences(
            text,
            "Running",
        )

        result = {
            "extraction_method": "ocr_fallback",
            "running_processes": running_count,
        }

        if process_rows:
            result["visible_process_rows"] = process_rows

        result["screenshot"] = path

        return result

    except Exception as exc:
        log.warning(
            "SM66 OCR fallback failed: %s",
            exc,
        )

    return {}


def action_smlg(session, capture):
    """
    Collect SMLG load-distribution / instance response-time data.

    Primary source:
        SAP GUI label controls.

    Screenshot:
        Always retained for PDF evidence.
    """
    import re

    goto_tcode(session, "SMLG")
    wait_until_not_busy(session)

    # Open Load Distribution / Overview
    session.findById(
        "wnd[0]/tbar[1]/btn[5]"
    ).press()

    wait_until_not_busy(session)

    user_area = session.findById("wnd[0]/usr")

    labels = {}

    for i in range(int(user_area.Children.Count)):
        try:
            obj = user_area.Children(i)
            control_id = obj.Id
            text = getattr(obj, "Text", "") or ""

            match = re.search(
                r"/lbl\[(\d+),(\d+)\]$",
                control_id,
            )

            if match:
                column = int(match.group(1))
                row = int(match.group(2))
                labels[(column, row)] = str(text).strip()

        except Exception:
            continue

    instances = []

    # Confirmed live SMLG layout:
    #
    # Instance       = column 1
    # State          = column 18
    # Response time  = column 24
    # Threshold      = column 38
    # User count     = column 45
    # User threshold = column 50
    # Time           = column 57
    # Quality        = column 66
    # Dialog steps   = column 74
    #
    # Instance rows are 3, 5, 7, ...

    for row in range(3, 100, 2):

        instance = labels.get((1, row), "").strip()

        if not instance:
            continue

        # Ignore summary rows.
        if instance.startswith("*"):
            continue

        state = labels.get((18, row), "").strip()
        response_raw = labels.get((24, row), "").strip()
        threshold = labels.get((38, row), "").strip()
        user_count = labels.get((45, row), "").strip()
        user_threshold = labels.get((50, row), "").strip()
        time_value = labels.get((57, row), "").strip()
        quality = labels.get((66, row), "").strip()
        dialog_steps = labels.get((74, row), "").strip()

        if not response_raw:
            continue

        # Extract numeric response time.
        match = re.search(
            r"\d+(?:[.,]\d+)?",
            response_raw,
        )

        if not match:
            continue

        try:
            response_time_ms = float(
                match.group(0).replace(",", ".")
            )
        except (ValueError, TypeError):
            continue

        instances.append({
            "instance": instance,
            "state": state,
            "response_time_ms": response_time_ms,
            "response_time_raw": response_raw,
            "threshold": threshold,
            "user_count": user_count,
            "user_threshold": user_threshold,
            "time": time_value,
            "quality": quality,
            "dialog_steps": dialog_steps,
        })

    response_times = [
        item["response_time_ms"]
        for item in instances
    ]

    result = {
        "instance_count": len(instances),
        "instances": instances,
        "extraction_method": "sap_gui_labels",
    }

    if response_times:
        result["max_response_time_ms"] = max(response_times)
        result["avg_response_time_ms"] = (
            sum(response_times) / len(response_times)
        )

        worst = max(
            instances,
            key=lambda item: item["response_time_ms"],
        )

        result["worst_instance"] = {
            "instance": worst["instance"],
            "response_time_ms": worst["response_time_ms"],
        }

    # Always retain screenshot for PDF evidence.
    screenshot_path = capture()

    if screenshot_path:
        result["screenshot_captured"] = True

    return result

def action_smq1(session, capture):
    """
    SMQ1 - Outbound qRFC Queue Monitoring.

    Extracts queue information from the SAP GUI ALV where possible.
    Screenshot is always retained for PDF evidence.
    """
    import re

    goto_tcode(session, "SMQ1")
    wait_until_not_busy(session)

    try:
        session.findById("wnd[0]/tbar[1]/btn[8]").press()
        wait_until_not_busy(session)
    except Exception as e:
        log.warning(f"SMQ1 execute failed: {e}")

    screenshot = capture()

    # The small "Queue Information" box is authoritative. The ALV may only
    # expose visible rows, and SAP GUI child labels can split formatted
    # values such as 72,648 into separate controls. Read a focused OCR crop
    # of that box first so the complete number is preserved.
    def parse_queue_information(text):
        if not text:
            return None, None

        normalized = " ".join(str(text).split())

        def parse_value(label):
            patterns = [
                rf"{label}\s*[:\-]?\s*([0-9][0-9,\s]*)",
                rf"{label}\s*([0-9][0-9,\s]*)",
            ]
            for pattern in patterns:
                match = re.search(pattern, normalized, re.IGNORECASE)
                if match:
                    digits = re.sub(r"[^0-9]", "", match.group(1))
                    if digits:
                        return int(digits)
            return None

        return (
            parse_value(r"Number\s+of\s+Entries\s+Displayed"),
            parse_value(r"Number\s+of\s+Queues\s+Displayed"),
        )

    def parse_information_crop(image_path):
        if not image_path:
            return None, None
        try:
            from PIL import Image
            from sap_gui.ocr_extractor import run_ocr
            image = Image.open(image_path)
            width, height = image.size
            # Queue Information is in the upper-left area of the SMQ screen.
            crop = image.crop((0, 0, int(width * 0.48), int(height * 0.40)))
            crop = crop.resize((max(crop.width * 2, 1200), max(crop.height * 2, 600)))
            crop_path = str(Path(image_path).with_name(Path(image_path).stem + "_queue_info.png"))
            crop.save(crop_path)
            text = run_ocr(crop_path) or ""
            try:
                Path(crop_path).unlink(missing_ok=True)
            except Exception:
                pass
            return parse_queue_information(text)
        except Exception as exc:
            log.debug("SMQ1 focused information OCR failed: %s", exc)
            return None, None

    summary_entries = None
    summary_queues = None

    # First inspect SAP GUI labels spatially. This avoids the old bug where
    # a formatted value such as 72,648 was reduced to the first token 72.
    try:
        wnd = session.findById("wnd[0]")
        labels = []

        def collect_labels(obj):
            try:
                text = str(getattr(obj, "Text", "") or "").strip()
                if text:
                    labels.append({
                        "text": text,
                        "left": float(getattr(obj, "Left", 0) or 0),
                        "top": float(getattr(obj, "Top", 0) or 0),
                    })
            except Exception:
                pass
            try:
                for child in obj.Children:
                    collect_labels(child)
            except Exception:
                pass

        collect_labels(wnd)

        def spatial_value(label_text):
            target = label_text.lower()
            candidates = [x for x in labels if target in x["text"].lower()]
            for label in candidates:
                same_line = [
                    x for x in labels
                    if x is not label
                    and abs(x["top"] - label["top"]) <= 12
                    and x["left"] > label["left"]
                    and re.fullmatch(r"[0-9][0-9,\s]*", x["text"].strip())
                ]
                same_line.sort(key=lambda x: x["left"])
                if same_line:
                    # SAP GUI can expose a formatted value such as 72,648 as
                    # two adjacent controls: "72" and "648". Combine the
                    # contiguous numeric fragments instead of taking only the
                    # first control.
                    fragments = []
                    previous_left = None
                    for item in same_line:
                        if previous_left is not None and item["left"] - previous_left > 140:
                            break
                        fragments.append(item["text"].strip())
                        previous_left = item["left"]
                        if len(fragments) >= 3:
                            break
                    raw = "".join(fragments)
                    digits = re.sub(r"[^0-9]", "", raw)
                    if digits:
                        return int(digits)
            return None

        summary_entries = spatial_value("Number of Entries Displayed")
        summary_queues = spatial_value("Number of Queues Displayed")
    except Exception as exc:
        log.debug("SMQ1 spatial information extraction failed: %s", exc)

    # OCR is the next source. The live SAP screenshot OCR is known to retain
    # values formatted as "72, 648"; parse that as 72648.
    if screenshot and (summary_entries is None or summary_queues is None):
        try:
            from sap_gui.ocr_extractor import run_ocr
            ocr_entries, ocr_queues = parse_queue_information(run_ocr(screenshot) or "")
            if summary_entries is None:
                summary_entries = ocr_entries
            if summary_queues is None:
                summary_queues = ocr_queues
        except Exception as exc:
            log.debug("SMQ1 full-screen information OCR failed: %s", exc)

    if summary_entries is None or summary_queues is None:
        crop_entries, crop_queues = parse_information_crop(screenshot)
        if summary_entries is None:
            summary_entries = crop_entries
        if summary_queues is None:
            summary_queues = crop_queues

    if summary_entries is None or summary_queues is None:
        try:
            wnd = session.findById("wnd[0]")
            gui_parts = []

            def collect_gui_text(obj):
                try:
                    value = str(getattr(obj, "Text", "") or "").strip()
                    if value:
                        gui_parts.append(value)
                except Exception:
                    pass
                try:
                    for child in obj.Children:
                        collect_gui_text(child)
                except Exception:
                    pass

            collect_gui_text(wnd)
            gui_entries, gui_queues = parse_queue_information(" ".join(gui_parts))
            if summary_entries is None:
                summary_entries = gui_entries
            if summary_queues is None:
                summary_queues = gui_queues
        except Exception as e:
            log.debug("SMQ1 GUI information extraction failed: %s", e)

    if (summary_entries is None or summary_queues is None) and screenshot:
        try:
            from sap_gui.ocr_extractor import run_ocr
            ocr_text = run_ocr(screenshot) or ""
            ocr_entries, ocr_queues = parse_queue_information(ocr_text)
            if summary_entries is None:
                summary_entries = ocr_entries
            if summary_queues is None:
                summary_queues = ocr_queues
        except Exception as e:
            log.warning(f"SMQ1 summary OCR failed: {e}")


    result = {
        "queues_displayed": summary_queues if summary_queues is not None else 0,
        "entries_displayed": summary_entries if summary_entries is not None else 0,
        "information": {
            "number_of_entries_displayed": summary_entries if summary_entries is not None else 0,
            "number_of_queues_displayed": summary_queues if summary_queues is not None else 0,
        },
        "queue_errors": 0,
        "extraction_method": (
            "sap_gui_information"
            if summary_entries is not None or summary_queues is not None
            else "sap_gui"
        ),
    }

    # ---------------------------------------------------------
    # Structured ALV extraction
    # ---------------------------------------------------------
    grid_ids = [
        "wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell",
        "wnd[0]/usr/cntlGRID1/shellcont/shell",
    ]

    grid = None

    for grid_id in grid_ids:
        try:
            grid = session.findById(grid_id)
            break
        except Exception:
            continue

    if grid is not None:
        try:
            row_count = int(grid.RowCount)
        except Exception:
            row_count = 0

        rows = []

        try:
            column_count = int(grid.ColumnCount)

            columns = []

            for i in range(column_count):
                try:
                    columns.append(str(grid.ColumnOrder(i)))
                except Exception:
                    pass

            for row_index in range(row_count):
                row = {}

                for column in columns:
                    try:
                        value = grid.GetCellValue(row_index, column)
                        row[column] = str(value).strip()
                    except Exception:
                        continue

                if any(row.values()):
                    rows.append(row)

        except Exception as e:
            log.warning(f"SMQ1 ALV extraction failed: {e}")
            rows = []

        if rows:
            error_count = 0
            entry_count = 0

            for row in rows:
                text = " ".join(str(v) for v in row.values()).lower()

                if any(
                    x in text
                    for x in (
                        "error",
                        "failed",
                        "stopped",
                        "retry",
                        "waiting",
                    )
                ):
                    error_count += 1

                # Try to identify numeric entry/count fields.
                for key, value in row.items():
                    key_text = str(key).lower()

                    if any(
                        x in key_text
                        for x in ("entry", "count", "number", "messages")
                    ):
                        try:
                            entry_count += int(
                                re.sub(r"[^\d]", "", str(value))
                            )
                        except Exception:
                            pass

            result["queues_displayed"] = (
                summary_queues if summary_queues is not None else len(rows)
            )
            result["entries_displayed"] = (
                summary_entries if summary_entries is not None else entry_count
            )
            result["queue_errors"] = error_count
            result["information"] = {
                "number_of_entries_displayed": result.get("entries_displayed", 0),
                "number_of_queues_displayed": result.get("queues_displayed", 0),
            }
            result["extraction_method"] = (
                "sap_gui_information"
                if summary_entries is not None or summary_queues is not None
                else "sap_gui_alv"
            )

            return result

        if row_count == 0:
            result["queues_displayed"] = summary_queues if summary_queues is not None else 0
            result["entries_displayed"] = summary_entries if summary_entries is not None else 0
            result["queue_errors"] = 0
            result["extraction_method"] = (
                "sap_gui_information"
                if summary_entries is not None or summary_queues is not None
                else "sap_gui_alv"
            )
            return result

    # ---------------------------------------------------------
    # OCR fallback
    # ---------------------------------------------------------
    if screenshot:
        try:
            from sap_gui.ocr_extractor import run_ocr

            text = run_ocr(screenshot)

            if re.search(
                r"nothing\s+(selected|found)|no\s+(data|queues)",
                text,
                re.IGNORECASE,
            ):
                return result

            patterns = {
                "entries_displayed": [
                    r"Number\s+of\s+Entries\s+Displayed\s*[:\-]?\s*([0-9][0-9,\s]*)",
                    r"Entries\s+Displayed\s*[:\-]?\s*([0-9][0-9,\s]*)",
                ],
                "queues_displayed": [
                    r"Number\s+of\s+Queues\s+Displayed\s*[:\-]?\s*([0-9][0-9,\s]*)",
                    r"Queues\s+Displayed\s*[:\-]?\s*([0-9][0-9,\s]*)",
                ],
            }

            for field, field_patterns in patterns.items():
                for pattern in field_patterns:
                    match = re.search(pattern, text, re.IGNORECASE)

                    if match:
                        result[field] = int(re.sub(r"[^0-9]", "", match.group(1)))
                        break

            error_matches = re.findall(
                r"\b(error|failed|stopped|retry|waiting)\b",
                text,
                re.IGNORECASE,
            )

            result["queue_errors"] = len(error_matches)
            result["extraction_method"] = "sap_gui_ocr"

        except Exception as e:
            log.warning(f"SMQ1 OCR fallback failed: {e}")

    return result


def action_smq2(session, capture):
    """
    SMQ2 - Inbound qRFC Queue Monitoring.

    Extracts queue information from the SAP GUI ALV where possible.
    Screenshot is always retained for PDF evidence.
    """
    import re

    goto_tcode(session, "SMQ2")
    wait_until_not_busy(session)

    try:
        session.findById("wnd[0]/tbar[1]/btn[8]").press()
        wait_until_not_busy(session)
    except Exception as e:
        log.warning(f"SMQ2 execute failed: {e}")

    screenshot = capture()

    # SMQ2 has the same authoritative Queue Information box as SMQ1. Use a
    # focused OCR crop first so formatted totals such as 50,709 are preserved.
    def parse_queue_information(text):
        if not text:
            return None, None

        normalized = " ".join(str(text).split())

        def parse_value(label):
            for pattern in (
                rf"{label}\s*[:\-]?\s*([0-9][0-9,\s]*)",
                rf"{label}\s*([0-9][0-9,\s]*)",
            ):
                match = re.search(pattern, normalized, re.IGNORECASE)
                if match:
                    digits = re.sub(r"[^0-9]", "", match.group(1))
                    if digits:
                        return int(digits)
            return None

        return (
            parse_value(r"Number\s+of\s+Entries\s+Displayed"),
            parse_value(r"Number\s+of\s+Queues\s+Displayed"),
        )

    def parse_information_crop(image_path):
        if not image_path:
            return None, None
        try:
            from PIL import Image
            from sap_gui.ocr_extractor import run_ocr
            image = Image.open(image_path)
            width, height = image.size
            crop = image.crop((0, 0, int(width * 0.48), int(height * 0.40)))
            crop = crop.resize((max(crop.width * 2, 1200), max(crop.height * 2, 600)))
            crop_path = str(Path(image_path).with_name(Path(image_path).stem + "_queue_info.png"))
            crop.save(crop_path)
            text = run_ocr(crop_path) or ""
            try:
                Path(crop_path).unlink(missing_ok=True)
            except Exception:
                pass
            return parse_queue_information(text)
        except Exception as exc:
            log.debug("SMQ2 focused information OCR failed: %s", exc)
            return None, None

    summary_entries = None
    summary_queues = None

    # First inspect SAP GUI labels spatially. This avoids the old bug where
    # a formatted value such as 72,648 was reduced to the first token 72.
    try:
        wnd = session.findById("wnd[0]")
        labels = []

        def collect_labels(obj):
            try:
                text = str(getattr(obj, "Text", "") or "").strip()
                if text:
                    labels.append({
                        "text": text,
                        "left": float(getattr(obj, "Left", 0) or 0),
                        "top": float(getattr(obj, "Top", 0) or 0),
                    })
            except Exception:
                pass
            try:
                for child in obj.Children:
                    collect_labels(child)
            except Exception:
                pass

        collect_labels(wnd)

        def spatial_value(label_text):
            target = label_text.lower()
            candidates = [x for x in labels if target in x["text"].lower()]
            for label in candidates:
                same_line = [
                    x for x in labels
                    if x is not label
                    and abs(x["top"] - label["top"]) <= 12
                    and x["left"] > label["left"]
                    and re.fullmatch(r"[0-9][0-9,\s]*", x["text"].strip())
                ]
                same_line.sort(key=lambda x: x["left"])
                if same_line:
                    # SAP GUI can expose a formatted value such as 72,648 as
                    # two adjacent controls: "72" and "648". Combine the
                    # contiguous numeric fragments instead of taking only the
                    # first control.
                    fragments = []
                    previous_left = None
                    for item in same_line:
                        if previous_left is not None and item["left"] - previous_left > 140:
                            break
                        fragments.append(item["text"].strip())
                        previous_left = item["left"]
                        if len(fragments) >= 3:
                            break
                    raw = "".join(fragments)
                    digits = re.sub(r"[^0-9]", "", raw)
                    if digits:
                        return int(digits)
            return None

        summary_entries = spatial_value("Number of Entries Displayed")
        summary_queues = spatial_value("Number of Queues Displayed")
    except Exception as exc:
        log.debug("SMQ1 spatial information extraction failed: %s", exc)

    # OCR is the next source. The live SAP screenshot OCR is known to retain
    # values formatted as "72, 648"; parse that as 72648.
    if screenshot and (summary_entries is None or summary_queues is None):
        try:
            from sap_gui.ocr_extractor import run_ocr
            ocr_entries, ocr_queues = parse_queue_information(run_ocr(screenshot) or "")
            if summary_entries is None:
                summary_entries = ocr_entries
            if summary_queues is None:
                summary_queues = ocr_queues
        except Exception as exc:
            log.debug("SMQ1 full-screen information OCR failed: %s", exc)

    if summary_entries is None or summary_queues is None:
        crop_entries, crop_queues = parse_information_crop(screenshot)
        if summary_entries is None:
            summary_entries = crop_entries
        if summary_queues is None:
            summary_queues = crop_queues

    if summary_entries is None or summary_queues is None:
        try:
            wnd = session.findById("wnd[0]")
            gui_parts = []

            def collect_gui_text(obj):
                try:
                    value = str(getattr(obj, "Text", "") or "").strip()
                    if value:
                        gui_parts.append(value)
                except Exception:
                    pass
                try:
                    for child in obj.Children:
                        collect_gui_text(child)
                except Exception:
                    pass

            collect_gui_text(wnd)
            gui_entries, gui_queues = parse_queue_information(" ".join(gui_parts))
            if summary_entries is None:
                summary_entries = gui_entries
            if summary_queues is None:
                summary_queues = gui_queues
        except Exception as e:
            log.debug("SMQ2 GUI information extraction failed: %s", e)

    if (summary_entries is None or summary_queues is None) and screenshot:
        try:
            from sap_gui.ocr_extractor import run_ocr
            ocr_text = run_ocr(screenshot) or ""
            summary_entries, summary_queues = parse_queue_information(ocr_text)
        except Exception as e:
            log.warning(f"SMQ2 summary OCR failed: {e}")

    result = {
        "queues_displayed": summary_queues if summary_queues is not None else 0,
        "entries_displayed": summary_entries if summary_entries is not None else 0,
        "information": {
            "number_of_entries_displayed": summary_entries if summary_entries is not None else 0,
            "number_of_queues_displayed": summary_queues if summary_queues is not None else 0,
        },
        "queue_errors": 0,
        "extraction_method": "sap_gui",
    }

    # ---------------------------------------------------------
    # Structured ALV extraction
    # ---------------------------------------------------------
    grid_ids = [
        "wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell",
        "wnd[0]/usr/cntlGRID1/shellcont/shell",
    ]

    grid = None

    for grid_id in grid_ids:
        try:
            grid = session.findById(grid_id)
            break
        except Exception:
            continue

    if grid is not None:
        try:
            row_count = int(grid.RowCount)
        except Exception:
            row_count = 0

        rows = []

        try:
            column_count = int(grid.ColumnCount)

            columns = []

            for i in range(column_count):
                try:
                    columns.append(str(grid.ColumnOrder(i)))
                except Exception:
                    pass

            for row_index in range(row_count):
                row = {}

                for column in columns:
                    try:
                        value = grid.GetCellValue(row_index, column)
                        row[column] = str(value).strip()
                    except Exception:
                        continue

                if any(row.values()):
                    rows.append(row)

        except Exception as e:
            log.warning(f"SMQ2 ALV extraction failed: {e}")
            rows = []

        if rows:
            error_count = 0
            entry_count = 0

            for row in rows:
                text = " ".join(
                    str(value) for value in row.values()
                ).lower()

                if any(
                    keyword in text
                    for keyword in (
                        "error",
                        "failed",
                        "stopped",
                        "retry",
                        "waiting",
                    )
                ):
                    error_count += 1

                for key, value in row.items():
                    key_text = str(key).lower()

                    if any(
                        keyword in key_text
                        for keyword in (
                            "entry",
                            "count",
                            "number",
                            "messages",
                        )
                    ):
                        try:
                            digits = re.sub(r"[^\d]", "", str(value))
                            if digits:
                                entry_count += int(digits)
                        except Exception:
                            pass

            result["queues_displayed"] = (
                summary_queues if summary_queues is not None else len(rows)
            )
            result["entries_displayed"] = (
                summary_entries if summary_entries is not None else entry_count
            )
            result["queue_errors"] = error_count
            result["information"] = {
                "number_of_entries_displayed": result.get("entries_displayed", 0),
                "number_of_queues_displayed": result.get("queues_displayed", 0),
            }
            result["extraction_method"] = "sap_gui_alv"

            return result

        if row_count == 0:
            result["queues_displayed"] = (
                summary_queues if summary_queues is not None else 0
            )
            result["entries_displayed"] = (
                summary_entries if summary_entries is not None else 0
            )
            result["queue_errors"] = 0
            result["extraction_method"] = (
                "sap_gui_information"
                if summary_entries is not None or summary_queues is not None
                else "sap_gui_alv"
            )

            return result

    # ---------------------------------------------------------
    # OCR fallback
    # ---------------------------------------------------------
    if screenshot:
        try:
            from sap_gui.ocr_extractor import run_ocr

            text = run_ocr(screenshot)

            if re.search(
                r"nothing\s+(selected|found)|no\s+(data|queues)",
                text,
                re.IGNORECASE,
            ):
                return result

            parsed_entries, parsed_queues = parse_queue_information(text)
            if parsed_entries is not None:
                result["entries_displayed"] = parsed_entries
            if parsed_queues is not None:
                result["queues_displayed"] = parsed_queues

            error_matches = re.findall(
                r"\b(error|failed|stopped|retry|waiting)\b",
                text,
                re.IGNORECASE,
            )

            result["queue_errors"] = len(error_matches)
            result["extraction_method"] = "sap_gui_ocr"

        except Exception as e:
            log.warning(f"SMQ2 OCR fallback failed: {e}")

    return result

def action_sost(session, capture):
    """
    SOST - SAPconnect Send Requests.

    Captures send-request statistics from the SOST screen.
    Uses SAP GUI text/labels first and OCR as fallback.
    Screenshot is always retained for PDF evidence.
    """
    import re

    goto_tcode(session, "SOST")
    wait_until_not_busy(session)

    # Increase the maximum number of displayed requests where
    # the control exists, then refresh the list.
    try:
        base = (
            "wnd[0]/usr/subSUB:SAPLSBCS_OUT:1100/"
            "subTOPSUB:SAPLSBCS_OUT:1110/"
            "tabsTAB1/tabpTAB1_FC1/"
            "ssubTAB1_SCA:SAPLSBCS_OUT:0003"
        )

        try:
            session.findById(
                f"{base}/txtG_MAXSEL"
            ).text = "50000"
        except Exception:
            pass

        try:
            session.findById(
                f"{base}/btnREFRICO2"
            ).press()
            wait_until_not_busy(session)
        except Exception as e:
            log.warning(f"SOST refresh failed: {e}")

    except Exception as e:
        log.warning(f"SOST setup failed: {e}")

    screenshot = capture()

    result = {
        "send_requests": 0,
        "waiting": 0,
        "sent": 0,
        "errors": 0,
        "extraction_method": "sap_gui",
    }

    # ---------------------------------------------------------
    # Read the entire SOST window (including the bottom statistics bar).
    # The four authoritative values are normally shown at the very bottom:
    #   <Send> Send  <Waiting> Waiting  <Sent> Sent  <Errors> Errors
    # ---------------------------------------------------------
    gui_text = ""

    try:
        wnd = session.findById("wnd[0]")
        texts = []

        def collect_texts(obj):
            try:
                text = str(getattr(obj, "Text", "") or "").strip()
                if text:
                    texts.append(text)
            except Exception:
                pass
            try:
                for child in obj.Children:
                    collect_texts(child)
            except Exception:
                pass

        collect_texts(wnd)
        gui_text = " ".join(texts)
    except Exception as e:
        log.warning(f"SOST GUI text extraction failed: {e}")

    # ---------------------------------------------------------
    # Parse the SOST bottom statistics independently. This avoids depending
    # on one exact combined sentence and handles labels separated by GUI
    # controls or OCR line breaks.
    # ---------------------------------------------------------
    def parse_summary(text):
        if not text:
            return None

        # Parse label/value pairs independently. This prevents dates/times or
        # other numeric fields elsewhere on the SOST screen from being folded
        # into the Waiting value (the previous parser could produce 28500).
        values = {
            "send_requests": None,
            "waiting": None,
            "sent": None,
            "errors": None,
        }
        lines = [str(line).strip() for line in str(text).splitlines() if str(line).strip()]
        if not lines:
            lines = [" ".join(str(text).split())]

        patterns = (
            ("send_requests", r"(\d[\d,]*)\s*=??\s*Send(?:\s+Requests?)?\b"),
            ("waiting", r"(\d[\d,]*)\s*=??\s*Waiting\b"),
            ("sent", r"(\d[\d,]*)\s*=??\s*Sent\b"),
            ("errors", r"(\d[\d,]*)\s*=??\s*Errors?\b"),
        )

        for line in lines:
            for key, pattern in patterns:
                matches = list(re.finditer(pattern, line, re.IGNORECASE))
                if matches:
                    raw = matches[-1].group(1)
                    values[key] = int(raw.replace(",", ""))

            # Also handle label-first GUI text such as "Waiting 500".
            for key, label in (("send_requests", "Send"), ("waiting", "Waiting"),
                               ("sent", "Sent"), ("errors", "Errors")):
                match = re.search(rf"\b{label}(?:\s+Requests?)?\b\s*[:=\-]?\s*(\d[\d,]*)", line, re.IGNORECASE)
                if match and values[key] is None:
                    values[key] = int(match.group(1).replace(",", ""))

        if sum(v is not None for v in values.values()) >= 2:
            values["extraction_method"] = "sap_gui_footer"
            return values
        return None

    def parse_footer_crop(image_path):
        """
        OCR the SOST statistics bar.

        The single wide crop this used to take (bottom 22% of the frame,
        doubled, full width) reliably lost the leftmost counter: on a real
        capture it read "0 Sent ... 0 Errors" and dropped "669 Send"
        entirely, so the report announced a clean mailbox while 669 requests
        sat queued. The counters are small text next to spinner controls, so
        they need a tighter band, more magnification and greyscale, and the
        band position varies with window size -- hence several candidates,
        best parse wins.
        """
        if not image_path:
            return None

        try:
            from PIL import Image
            from sap_gui.ocr_extractor import run_ocr
        except Exception as exc:
            log.debug("SOST footer OCR unavailable: %s", exc)
            return None

        best = None
        best_score = 0

        for lo, hi in ((0.88, 0.98), (0.90, 1.00), (0.84, 0.95), (0.78, 1.00)):
            crop_path = None
            try:
                image = Image.open(image_path)
                width, height = image.size
                crop = image.crop((0, int(height * lo), width, int(height * hi)))
                crop = crop.resize(
                    (crop.width * 4, crop.height * 4), Image.LANCZOS
                ).convert("L")
                crop_path = str(Path(image_path).with_name(
                    Path(image_path).stem + f"_sost_footer_{int(lo * 100)}.png"))
                crop.save(crop_path)

                parsed = parse_summary(run_ocr(crop_path) or "")
                if parsed:
                    score = sum(
                        1 for k in ("send_requests", "waiting", "sent", "errors")
                        if parsed.get(k) is not None
                    )
                    if score > best_score:
                        best, best_score = parsed, score
                    if score == 4:
                        break
            except Exception as exc:
                log.debug("SOST footer crop %.2f-%.2f failed: %s", lo, hi, exc)
            finally:
                if crop_path:
                    try:
                        Path(crop_path).unlink(missing_ok=True)
                    except Exception:
                        pass

        return best

    parsed = parse_footer_crop(screenshot)
    if not parsed:
        parsed = parse_summary(gui_text)

    if parsed:
        result.update(parsed)
        return result

    # ---------------------------------------------------------
    # OCR fallback
    # ---------------------------------------------------------
    if screenshot:
        try:
            from sap_gui.ocr_extractor import run_ocr

            ocr_text = run_ocr(screenshot)
            parsed = parse_summary(ocr_text)

            if parsed:
                result.update(parsed)
                result["extraction_method"] = "sap_gui_ocr"
                return result

            # Handle an empty SOST result screen.
            if re.search(
                r"no\s+(send\s+requests?|data)"
                r"|nothing\s+(selected|found)",
                ocr_text,
                re.IGNORECASE,
            ):
                return result

        except Exception as e:
            log.warning(f"SOST OCR fallback failed: {e}")

    return result

def action_sp01(session, capture):
    """
    SP01 - Spool Requests.

    Extracts spool request rows from the SAP GUI ALV where available.
    Screenshot is always retained for PDF evidence.
    """
    import re

    goto_tcode(session, "SP01")
    wait_until_not_busy(session)

    # SP01 defaults its "Created By" field to the logged-on user, so
    # executing the selection as-is reported only CAQ_ADMIN's own spool
    # requests -- two of them -- as if that were the whole system. Widen it
    # to "*" before executing so the check covers every user's spool.
    # Value match first. SP01 pre-fills "Created by" with the logon user,
    # so the field can be picked out by what it contains rather than by a
    # technical name or a caption position -- neither of which had matched
    # on this system.
    widened = _wildcard_field_holding_logon_user(session)

    if not widened:
        widened = _wildcard_selection_field(
            session, "RQOWNER", "OWNER", "UNAME", "CREATOR", "ERSTELLER"
        )

    if not widened:
        # Fall back to the caption. SP01's selection screen labels this
        # field "Created by", which is stable in a way the technical name
        # is not.
        widened = _set_field_beside_label(
            session, "*", "created by", "created on behalf", "owner"
        )

    if not widened:
        log.warning(
            "SP01: could not widen the 'Created by' selection field; the "
            "spool count below covers only the logged-on user. The field "
            "names present on the screen are logged above."
        )

    # Capture the selection screen BEFORE executing. Only the result list
    # was ever captured, so the evidence could not show what the list had
    # been filtered on -- there was no way to tell a genuine two-request
    # system from one where the user filter was still in place. This
    # screenshot makes the filter state part of the record.
    try:
        capture("selection")
    except Exception as exc:
        log.debug("SP01 selection-screen capture skipped: %s", exc)

    # Log the other restriction on this screen. SP01 also defaults its
    # "Created On" range to today, so even a system-wide owner filter still
    # reports only today's spool requests. That is a deliberate scope
    # choice rather than a bug, so it is reported, not silently widened.
    try:
        for label_text, field_obj in _labelled_fields(session):
            if "created on" in label_text.lower():
                log.info("SP01: 'Created On' restriction is %r (today only "
                         "by default; widen it if the check should cover "
                         "the full spool).",
                         str(getattr(field_obj, "Text", "") or ""))
                break
    except Exception:
        pass

    # Execute using the (now system-wide) selection.
    try:
        session.findById("wnd[0]/tbar[1]/btn[8]").press()
        wait_until_not_busy(session)
    except Exception as e:
        log.warning(f"SP01 execute failed: {e}")

    screenshot = capture()

    result = {
        "spool_requests": 0,
        "spool_errors": 0,
        "spool_without_output_request": 0,
        "extraction_method": "sap_gui",
    }

    # The SP01 result screen also displays authoritative summary lines at
    # the bottom, e.g. "2 spool requests displayed" and
    # "2 spool requests without output request". Read those explicitly.
    def parse_spool_summary(text):
        if not text:
            return None, None
        normalized = " ".join(str(text).split())

        requests = None
        without_output = None

        patterns = [
            r"([0-9][0-9,\s]*)\s*spool\s+requests?\s+displayed",
            r"([0-9][0-9,\s]*)\s*spool\s+requests?\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                digits = re.sub(r"[^0-9]", "", match.group(1))
                if digits:
                    requests = int(digits)
                    break

        match = re.search(
            r"([0-9][0-9,\s]*)\s*spool\s+requests?\s+without\s+an?\s+output\s+request",
            normalized,
            re.IGNORECASE,
        )
        if not match:
            match = re.search(
                r"([0-9][0-9,\s]*)\s*spool\s+requests?\s+without\s+output\s+request",
                normalized,
                re.IGNORECASE,
            )
        if match:
            digits = re.sub(r"[^0-9]", "", match.group(1))
            if digits:
                without_output = int(digits)

        return requests, without_output

    summary_requests = None
    summary_without_output = None

    try:
        wnd = session.findById("wnd[0]")
        texts = []
        def collect_texts(obj):
            try:
                value = str(getattr(obj, "Text", "") or "").strip()
                if value:
                    texts.append(value)
            except Exception:
                pass
            try:
                for child in obj.Children:
                    collect_texts(child)
            except Exception:
                pass
        collect_texts(wnd)
        summary_requests, summary_without_output = parse_spool_summary(" ".join(texts))
    except Exception as e:
        log.debug("SP01 GUI summary extraction failed: %s", e)

    # ---------------------------------------------------------
    # Structured ALV extraction
    # ---------------------------------------------------------
    grid_ids = [
        "wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell",
        "wnd[0]/usr/cntlGRID1/shellcont/shell",
    ]

    grid = None

    for grid_id in grid_ids:
        try:
            grid = session.findById(grid_id)
            break
        except Exception:
            continue

    if grid is not None:
        try:
            row_count = int(grid.RowCount)
        except Exception:
            row_count = 0

        rows = []

        try:
            column_count = int(grid.ColumnCount)

            columns = []

            for i in range(column_count):
                try:
                    columns.append(str(grid.ColumnOrder(i)))
                except Exception:
                    pass

            for row_index in range(row_count):
                row = {}

                for column in columns:
                    try:
                        value = grid.GetCellValue(row_index, column)
                        row[column] = str(value).strip()
                    except Exception:
                        continue

                if any(row.values()):
                    rows.append(row)

        except Exception as e:
            log.warning(f"SP01 ALV extraction failed: {e}")
            rows = []

        if rows:
            error_count = 0

            for row in rows:
                text = " ".join(
                    str(value) for value in row.values()
                ).lower()

                if any(
                    keyword in text
                    for keyword in (
                        "error",
                        "failed",
                        "incorrect",
                        "problem",
                    )
                ):
                    error_count += 1

            result["spool_requests"] = (
                summary_requests if summary_requests is not None else len(rows)
            )
            result["spool_without_output_request"] = (
                summary_without_output if summary_without_output is not None else 0
            )
            result["spool_errors"] = error_count
            result["rows"] = rows
            result["extraction_method"] = "sap_gui_alv"

            return result

        if row_count == 0:
            result["spool_requests"] = (
                summary_requests if summary_requests is not None else 0
            )
            result["spool_without_output_request"] = (
                summary_without_output if summary_without_output is not None else 0
            )
            result["spool_errors"] = 0
            result["extraction_method"] = "sap_gui_alv"
            return result

    # ---------------------------------------------------------
    # OCR fallback
    # ---------------------------------------------------------
    if screenshot:
        try:
            from sap_gui.ocr_extractor import run_ocr, count_spool_rows

            text = run_ocr(screenshot)

            ocr_requests, ocr_without_output = parse_spool_summary(text)
            if summary_requests is None:
                summary_requests = ocr_requests
            if summary_without_output is None:
                summary_without_output = ocr_without_output

            if summary_requests is not None:
                result["spool_requests"] = summary_requests
                result["spool_without_output_request"] = (
                    summary_without_output if summary_without_output is not None else 0
                )
                result["extraction_method"] = "sap_gui_summary"

            if re.search(
                r"nothing\s+(selected|found)"
                r"|no\s+spool\s+requests?"
                r"|no\s+data",
                text,
                re.IGNORECASE,
            ):
                return result

            count = count_spool_rows(text)

            if count is not None and summary_requests is None:
                result["spool_requests"] = int(count)
                result["extraction_method"] = "sap_gui_ocr"

            result["spool_errors"] = len(
                re.findall(
                    r"\b(error|failed|incorrect|problem)\b",
                    text,
                    re.IGNORECASE,
                )
            )

        except Exception as e:
            log.warning(f"SP01 OCR fallback failed: {e}")

    return result


def action_st06(session, capture):
    """
    ST06 - Operating System / Host Health.

    Extracts the main OS/host health values exposed by the SAP GUI.
    Screenshot is always retained for PDF evidence.
    """
    import re

    goto_tcode(session, "ST06")
    wait_until_not_busy(session)

    screenshot = capture()

    result = {
        "host": "",
        "cpu_utilization": None,
        "memory_utilization": None,
        "disk_utilization": None,
        "extraction_method": "sap_gui",
    }

    # ---------------------------------------------------------
    # Collect visible SAP GUI text recursively
    # ---------------------------------------------------------
    gui_text_parts = []

    try:
        usr = session.findById("wnd[0]/usr")

        def collect_texts(obj):
            try:
                value = str(
                    getattr(obj, "Text", "") or ""
                ).strip()

                if value:
                    gui_text_parts.append(value)
            except Exception:
                pass

            try:
                for child in obj.Children:
                    collect_texts(child)
            except Exception:
                pass

        collect_texts(usr)

    except Exception as e:
        log.warning(
            f"ST06 GUI text extraction failed: {e}"
        )

    gui_text = " ".join(gui_text_parts)

    # ---------------------------------------------------------
    # Generic percentage parser
    # ---------------------------------------------------------
    def parse_percent(text, patterns):
        for pattern in patterns:
            match = re.search(
                pattern,
                text,
                re.IGNORECASE,
            )

            if match:
                try:
                    return float(
                        match.group(1).replace(",", ".")
                    )
                except Exception:
                    pass

        return None

    # ---------------------------------------------------------
    # Host
    # ---------------------------------------------------------
    host_patterns = [
        r"Host\s*[:\-]\s*([A-Za-z0-9_.\-]+)",
        r"Hostname\s*[:\-]\s*([A-Za-z0-9_.\-]+)",
    ]

    for pattern in host_patterns:
        match = re.search(
            pattern,
            gui_text,
            re.IGNORECASE,
        )

        if match:
            result["host"] = match.group(1).strip()
            break

    # ---------------------------------------------------------
    # CPU
    # ---------------------------------------------------------
    result["cpu_utilization"] = parse_percent(
        gui_text,
        [
            r"CPU\s*(?:utilization|usage|load)"
            r"\s*[:\-]?\s*([\d.,]+)\s*%?",
            r"CPU\s*[:\-]\s*([\d.,]+)\s*%",
        ],
    )

    # ---------------------------------------------------------
    # Memory
    # ---------------------------------------------------------
    result["memory_utilization"] = parse_percent(
        gui_text,
        [
            r"Memory\s*(?:utilization|usage)"
            r"\s*[:\-]?\s*([\d.,]+)\s*%?",
            r"Memory\s*[:\-]\s*([\d.,]+)\s*%",
            r"Physical\s*Memory"
            r".*?([\d.,]+)\s*%",
        ],
    )

    # ---------------------------------------------------------
    # Disk
    # ---------------------------------------------------------
    result["disk_utilization"] = parse_percent(
        gui_text,
        [
            r"Disk\s*(?:utilization|usage)"
            r"\s*[:\-]?\s*([\d.,]+)\s*%?",
            r"Disk\s*[:\-]\s*([\d.,]+)\s*%",
        ],
    )

    if any(
        value is not None
        for key, value in result.items()
        if key not in ("host", "extraction_method")
    ) or result["host"]:
        result["extraction_method"] = "sap_gui_labels"
        return result

    # ---------------------------------------------------------
    # OCR fallback
    # ---------------------------------------------------------
    if screenshot:
        try:
            from sap_gui.ocr_extractor import run_ocr

            ocr_text = run_ocr(screenshot)

            if not result["host"]:
                for pattern in host_patterns:
                    match = re.search(
                        pattern,
                        ocr_text,
                        re.IGNORECASE,
                    )

                    if match:
                        result["host"] = match.group(1).strip()
                        break

            if result["cpu_utilization"] is None:
                result["cpu_utilization"] = parse_percent(
                    ocr_text,
                    [
                        r"CPU\s*(?:utilization|usage|load)"
                        r"\s*[:\-]?\s*([\d.,]+)\s*%?",
                        r"CPU\s*[:\-]\s*([\d.,]+)\s*%",
                    ],
                )

            if result["memory_utilization"] is None:
                result["memory_utilization"] = parse_percent(
                    ocr_text,
                    [
                        r"Memory\s*(?:utilization|usage)"
                        r"\s*[:\-]?\s*([\d.,]+)\s*%?",
                        r"Memory\s*[:\-]\s*([\d.,]+)\s*%",
                        r"Physical\s*Memory"
                        r".*?([\d.,]+)\s*%",
                    ],
                )

            if result["disk_utilization"] is None:
                result["disk_utilization"] = parse_percent(
                    ocr_text,
                    [
                        r"Disk\s*(?:utilization|usage)"
                        r"\s*[:\-]?\s*([\d.,]+)\s*%?",
                        r"Disk\s*[:\-]\s*([\d.,]+)\s*%",
                    ],
                )

            if any(
                value is not None
                for key, value in result.items()
                if key not in ("host", "extraction_method")
            ) or result["host"]:
                result["extraction_method"] = "sap_gui_ocr"

        except Exception as e:
            log.warning(
                f"ST06 OCR fallback failed: {e}"
            )

    return result

def action_st03n(session, capture):
    """
    ST03N - Workload / Response Time.

    Captures the workload overview and extracts the key response-time
    information available from the SAP GUI screen.
    Screenshot is always retained for PDF evidence.
    """
    import re

    goto_tcode(session, "ST03N")
    wait_until_not_busy(session)

    # Try to open the workload overview.
    try:
        session.findById("wnd[0]/tbar[1]/btn[8]").press()
        wait_until_not_busy(session)
    except Exception as e:
        log.warning(f"ST03N execute failed: {e}")

    screenshot = capture()

    result = {
        "response_time_ms": None,
        "dialog_steps": None,
        "workload_entries": 0,
        "extraction_method": "sap_gui",
    }

    # ---------------------------------------------------------
    # Collect visible SAP GUI text recursively
    # ---------------------------------------------------------
    gui_text = ""

    try:
        usr = session.findById("wnd[0]/usr")
        texts = []

        def collect_texts(obj):
            try:
                value = str(
                    getattr(obj, "Text", "") or ""
                ).strip()

                if value:
                    texts.append(value)
            except Exception:
                pass

            try:
                for child in obj.Children:
                    collect_texts(child)
            except Exception:
                pass

        collect_texts(usr)
        gui_text = " ".join(texts)

    except Exception as e:
        log.warning(f"ST03N GUI text extraction failed: {e}")

    # ---------------------------------------------------------
    # Parse workload / response-time values
    # ---------------------------------------------------------
    def parse_text(text):
        if not text:
            return False

        normalized = " ".join(str(text).split())
        found = False

        # Response time
        response_patterns = [
            r"response\s*time\s*[:\-]?\s*([\d.,]+)\s*(ms|s)?",
            r"average\s*response\s*time\s*[:\-]?\s*([\d.,]+)\s*(ms|s)?",
            r"resp(?:onse)?\.?\s*time\s*[:\-]?\s*([\d.,]+)\s*(ms|s)?",
        ]

        for pattern in response_patterns:
            match = re.search(
                pattern,
                normalized,
                re.IGNORECASE,
            )

            if match:
                try:
                    value = float(
                        match.group(1).replace(",", "")
                    )

                    unit = (match.group(2) or "ms").lower()

                    if unit == "s":
                        value *= 1000

                    result["response_time_ms"] = value
                    found = True
                    break
                except Exception:
                    pass

        # Dialog steps
        dialog_patterns = [
            r"dialog\s*steps\s*[:\-]?\s*([\d,]+)",
            r"dialog\s*step\s*[:\-]?\s*([\d,]+)",
        ]

        for pattern in dialog_patterns:
            match = re.search(
                pattern,
                normalized,
                re.IGNORECASE,
            )

            if match:
                try:
                    result["dialog_steps"] = int(
                        match.group(1).replace(",", "")
                    )
                    found = True
                    break
                except Exception:
                    pass

        # Generic workload entry count
        workload_patterns = [
            r"workload\s*(?:entries|records|requests)"
            r"\s*[:\-]?\s*([\d,]+)",
            r"number\s*of\s*(?:entries|records|requests)"
            r"\s*[:\-]?\s*([\d,]+)",
        ]

        for pattern in workload_patterns:
            match = re.search(
                pattern,
                normalized,
                re.IGNORECASE,
            )

            if match:
                try:
                    result["workload_entries"] = int(
                        match.group(1).replace(",", "")
                    )
                    found = True
                    break
                except Exception:
                    pass

        return found

    if parse_text(gui_text):
        result["extraction_method"] = "sap_gui_labels"
        return result

    # ---------------------------------------------------------
    # OCR fallback
    # ---------------------------------------------------------
    if screenshot:
        try:
            from sap_gui.ocr_extractor import run_ocr

            ocr_text = run_ocr(screenshot)

            if parse_text(ocr_text):
                result["extraction_method"] = "sap_gui_ocr"

        except Exception as e:
            log.warning(f"ST03N OCR fallback failed: {e}")

    return result


def action_st22_today(session, capture):
    """
    Collect today's ST22 ABAP runtime errors.

    ST22 has two valid outcomes:

    1. Today contains dumps:
       SAP displays an ALV containing the dump records.

    2. Today contains no dumps:
       SAP remains on the ST22 selection screen and no ALV may exist.

    Both outcomes are successful monitoring results.

    Navigation is intentionally minimal:
        ST22 -> TODAY -> capture -> inspect ALV if available
    """

    import time

    goto_tcode(session, "ST22")
    wait_until_not_busy(session)

    log.info("ST22: opened ST22. Selecting TODAY only.")

    # ---------------------------------------------------------------
    # Select TODAY
    # ---------------------------------------------------------------

    today_button = session.findById(
        "wnd[0]/usr/btnTODAY"
    )

    log.info("ST22: TODAY button found. Pressing it.")

    today_button.press()

    wait_until_not_busy(session)

    # Give SAP GUI time to update the screen.
    time.sleep(0.8)

    log.info("ST22: TODAY selection completed.")

    # ---------------------------------------------------------------
    # Capture screenshot immediately.
    #
    # IMPORTANT:
    # Do this BEFORE looking for the ALV.
    #
    # When there are zero dumps, SAP may not create the ALV control.
    # The screenshot is still valid evidence.
    # ---------------------------------------------------------------

    screenshot_path = None

    try:
        screenshot_path = capture("today")
        log.info(
            "ST22: TODAY screenshot captured: %s",
            screenshot_path,
        )
    except Exception as e:
        log.warning(
            "ST22: TODAY screenshot capture failed: %s: %s",
            type(e).__name__,
            e,
        )

    # ---------------------------------------------------------------
    # Check status bar.
    #
    # With zero dumps SAP commonly reports something similar to:
    #
    #   No short dumps match the selection criteria
    #
    # Do not depend exclusively on the exact wording because it can
    # vary between SAP versions/languages.
    # ---------------------------------------------------------------

    status_text = ""

    try:
        status_bar = session.findById("wnd[0]/sbar")
        status_text = str(
            getattr(status_bar, "Text", "") or ""
        ).strip()

        if status_text:
            log.info(
                "ST22: status bar = %r",
                status_text,
            )

    except Exception as e:
        log.debug(
            "ST22: could not read status bar: %s: %s",
            type(e).__name__,
            e,
        )

    # ---------------------------------------------------------------
    # Try to find the ALV.
    #
    # ALV exists when one or more dumps are displayed.
    # It may NOT exist when today's count is zero.
    # ---------------------------------------------------------------

    alv_id = (
        "wnd[0]/usr/"
        "cntlRSSHOWRABAX_ALV_100/"
        "shellcont/shell"
    )

    grid = None

    try:
        grid = session.findById(alv_id)
        log.info("ST22: TODAY ALV found.")

    except Exception as e:
        log.info(
            "ST22: TODAY ALV is not available. "
            "This can be normal when there are zero dumps. "
            "status=%r error=%s",
            status_text,
            type(e).__name__,
        )

    # ---------------------------------------------------------------
    # ZERO-DUMP CASE
    # ---------------------------------------------------------------

    if grid is None:

        log.info(
            "ST22: No TODAY ALV detected. "
            "Treating TODAY as zero dumps."
        )

        return {
            "dump_count": 0,
            "dumps": [],
            "unique_users": [],
            "unique_programs": [],
            "unique_runtime_errors": [],
            "screenshot_captured": bool(screenshot_path),
            "status_message": status_text,
        }

    # ---------------------------------------------------------------
    # ALV EXISTS -> read rows
    # ---------------------------------------------------------------

    columns = [
        "DATUM",
        "UZEIT",
        "AHOST",
        "UNAME",
        "MANDT",
        "XHOLD",
        "ERRORID",
        "REXCEPTION",
        "GPROGRAM",
        "MODNO",
        "TID",
    ]

    try:
        row_count = int(grid.RowCount)
    except Exception as e:
        log.warning(
            "ST22: Could not read ALV RowCount: %s: %s",
            type(e).__name__,
            e,
        )
        row_count = 0

    log.info(
        "ST22: TODAY ALV contains %d row(s).",
        row_count,
    )

    dumps = []

    # ---------------------------------------------------------------
    # Extract every dump
    # ---------------------------------------------------------------

    for row in range(row_count):

        record = {}

        for column in columns:

            try:
                value = grid.GetCellValue(
                    row,
                    column,
                )

                if value is None:
                    value = ""

                record[column] = str(value).strip()

            except Exception as e:

                log.warning(
                    "ST22: Could not read row=%d column=%s: %s",
                    row,
                    column,
                    type(e).__name__,
                )

                record[column] = ""

        dump = {
            "date": record.get("DATUM", ""),
            "time": record.get("UZEIT", ""),
            "host": record.get("AHOST", ""),
            "user": record.get("UNAME", ""),
            "client": record.get("MANDT", ""),
            "hold_status": record.get("XHOLD", ""),
            "runtime_error": record.get("ERRORID", ""),
            "exception": record.get("REXCEPTION", ""),
            "program": record.get("GPROGRAM", ""),
            "module": record.get("MODNO", ""),
            "transaction_id": record.get("TID", ""),
        }

        # Ignore completely empty rows.
        if not any(
            str(value).strip()
            for value in dump.values()
        ):
            continue

        dumps.append(dump)

        log.info(
            "ST22: dump[%d] date=%s time=%s "
            "user=%s error=%s program=%s",
            len(dumps),
            dump["date"],
            dump["time"],
            dump["user"],
            dump["runtime_error"],
            dump["program"],
        )

    # ---------------------------------------------------------------
    # Aggregate information
    # ---------------------------------------------------------------

    users = sorted(
        {
            dump["user"]
            for dump in dumps
            if dump.get("user")
        }
    )

    programs = sorted(
        {
            dump["program"]
            for dump in dumps
            if dump.get("program")
        }
    )

    runtime_errors = sorted(
        {
            dump["runtime_error"]
            for dump in dumps
            if dump.get("runtime_error")
        }
    )

    log.info(
        "ST22: TODAY collection complete: "
        "dump_count=%d users=%d programs=%d runtime_errors=%d",
        len(dumps),
        len(users),
        len(programs),
        len(runtime_errors),
    )

    return {
        "dump_count": len(dumps),
        "dumps": dumps,
        "unique_users": users,
        "unique_programs": programs,
        "unique_runtime_errors": runtime_errors,
        "screenshot_captured": bool(screenshot_path),
        "status_message": status_text,
    }


ACTIONS = {
    "al08": action_al08,
    "db01": action_db01,
    "db02": action_db02,
    "db12": action_db12,
    "scot": action_scot,
    "sm12": action_sm12,
    "sm13": action_sm13,
    "sm21": action_sm21,
    "sm37_active": action_sm37,
    "sm37_cancelled": action_sm37_cancelled,
    "sm51": action_sm51,
    "sm58": action_sm58,
    "sm66": action_sm66,
    "smlg": action_smlg,
    "smq1": action_smq1,
    "smq2": action_smq2,
    "sost": action_sost,
    "sp01": action_sp01,
    "st03n": action_st03n,
    "st22_today": action_st22_today,
    "st06": action_st06,
}
