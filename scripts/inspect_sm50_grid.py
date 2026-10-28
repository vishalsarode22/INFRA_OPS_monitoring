import os
import sys

# Add project root to Python import path
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import goto_tcode

SM50_GRID = (
    "wnd[0]/usr/cntlGRID1/shellcont/shell/"
    "shellcont[1]/shell"
)

COLUMNS = [
    "WP_INDEX",
    "WP_TYPE_DISP",
    "PID",
    "STATE_DISP",
    "STATE_INFO_DISP",
    "FAILURES",
    "SEM_LOCKED",
    "SEM_LOCKING",
    "CPU",
    "ELAPSED_TIME",
    "PRIORITY_DISP",
    "WAIT_FOR_PRIORITY_DISP",
    "WP_PROGRAM",
    "TENANT_DISP",
    "USER_NAME",
    "CURRENT_ACTION_DISP",
    "ACTION_INFO",
]


def main():
    session = get_scripting_session()

    goto_tcode(session, "SM50")

    grid = session.findById(SM50_GRID)

    print("=" * 80)
    print("SM50 DIRECT ALV EXTRACTION")
    print("=" * 80)

    print("Rows:", grid.RowCount)
    print("Columns:", grid.ColumnCount)

    print()
    print("--- COLUMN ORDER ---")

    column_order = grid.ColumnOrder

    for i in range(column_order.Count):
        print(i, "=>", column_order.ElementAt(i))

    print()
    print("--- ROW DATA ---")

    for row_index in range(grid.RowCount):

        row = {}

        for column in COLUMNS:
            try:
                row[column] = grid.GetCellValue(
                    row_index,
                    column
                )
            except Exception as exc:
                row[column] = ""
                print(
                    "WARNING:",
                    "row=", row_index,
                    "column=", column,
                    "error=", exc
                )

        print()
        print("ROW", row_index)

        for key, value in row.items():
            print(
                " ",
                key,
                "=",
                repr(value)
            )


if __name__ == "__main__":
    main()