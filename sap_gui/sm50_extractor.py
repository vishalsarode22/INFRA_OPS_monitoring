from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy


SM50_GRID_ID = (
    "wnd[0]/usr/cntlGRID1/shellcont/shell/"
    "shellcont[1]/shell"
)


SM50_COLUMNS = [
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


def get_sm50_grid(session):
    """
    Navigate to SM50 and return the actual ALV grid.
    """

    goto_tcode(session, "SM50")

    wait_until_not_busy(session)

    return session.findById(SM50_GRID_ID)


def extract_sm50_rows(session):
    """
    Extract all visible SM50 work-process rows
    directly from the SAP ALV grid.
    """

    grid = get_sm50_grid(session)

    rows = []

    for row_index in range(grid.RowCount):

        row = {}

        for column in SM50_COLUMNS:
            try:
                row[column] = grid.GetCellValue(
                    row_index,
                    column,
                )
            except Exception:
                row[column] = ""

        rows.append(row)

    return rows