import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy


COLUMNS = [
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


def main():
    session = get_scripting_session()

    goto_tcode(session, "ST22")
    wait_until_not_busy(session)

    session.findById("wnd[0]/usr/btnTODAY").press()
    wait_until_not_busy(session)

    grid = session.findById(
        "wnd[0]/usr/"
        "cntlRSSHOWRABAX_ALV_100/"
        "shellcont/shell"
    )

    print("=" * 80)
    print("ST22 TODAY - DUMP DATA")
    print("=" * 80)

    print("ROWS =", grid.RowCount)
    print()

    for row in range(grid.RowCount):
        print("-" * 80)
        print("ROW", row)
        print("-" * 80)

        for column in COLUMNS:
            try:
                value = grid.GetCellValue(row, column)
                print(f"{column:12} = {value!r}")
            except Exception as e:
                print(
                    f"{column:12} = ERROR "
                    f"{type(e).__name__}: {e}"
                )


if __name__ == "__main__":
    main()