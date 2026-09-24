"""
Round 23: less standing noise. SM21 graded by message ID (routine and dump
messages counted, not alerted), oldest lock at 24 h with the timer-daemon lock
excluded, and SM12 / ST03 limits fitted to a production system.
Figures: PS4, 22.09.2026.
"""
import time

import pytest

import sap_gui.tcode_actions as ta

try:
    from test_round18_reads_and_reports import FakeSession, _Ctl, _Children, _gui
except ImportError:  # tests/ collected as a package
    from tests.test_round18_reads_and_reports import FakeSession, _Ctl, _Children, _gui


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(ta, "goto_tcode", lambda *a, **k: None)
    monkeypatch.setattr(ta, "wait_until_not_busy", lambda *a, **k: None)


class _SyslogGrid(_Ctl):
    COLUMNS = ["DATE", "TIME", "INSTANCE", "TYPE", "CLIENT", "USER", "MSGID", "TEXT"]

    def __init__(self, rows):
        super().__init__("grid", type_="GuiShell", SubType="GridView", Children=_Children([]))
        self.rows, self.RowCount = rows, len(rows)
        self.ColumnCount, self.VisibleRowCount, self.firstVisibleRow = len(self.COLUMNS), 40, 0

    def ColumnOrder(self, i): return self.COLUMNS[i]
    def GetCellValue(self, r, c): return self.rows[r].get(c, "")


def _entry(msg_id, text, wp_type="DIA"):
    return {"DATE": "22.09.2026", "TIME": "14:07:21", "INSTANCE": "vhrrnps4ci_PS4_00",
            "TYPE": wp_type, "CLIENT": "500", "USER": "64814", "MSGID": msg_id, "TEXT": text}


# The 15:53 SM21 screen: soft-cancels, client disconnects, WP restarts, one dump echo.
_PS4_SYSLOG = (
    [_entry("R47", "Delete ABAP session T42_U3516_M0 (Info = Execution was canceled "
                   "(Softcancel)) [Warning/Session]")] * 20
    + [_entry("R48", "> Reason for Soft Cancel: DP_SOFTCANCEL_SAP_GUI_DISCONNECT")] * 20
    + [_entry("Q0I", "Operating system call recv failed (error no. 104 )", "DP")] * 9
    + [_entry("Q04", "Connection to terminal LP-MU-1200417 closed (user = 64814)", "DP")] * 9
    + [_entry("Q02", "Stops work process 67 (PID = 14858, Info = Exit with status 0)", "BTC")] * 3
    + [_entry("AB0", 'Runtime error "RAISE_EXCEPTION" occurred.')]
    + [_entry("AB1", '> Short dump "260922 130500 vhrrnps4ci_PS4_00 62358" created.')]
    + [_entry("NK2", "RF_BELEG 1100 00 2026 : ROLLBACK in Italian buffering")]
)


def test_sm21_routine_and_dump_messages_do_not_alert():
    container = _Ctl("cont", type_="GuiContainer", Children=_Children([_SyslogGrid(_PS4_SYSLOG)]))
    data = ta.action_sm21(FakeSession(usr_children=[container]), lambda *a: None)
    assert data["log_count"] == 64
    assert (data["error_count"], data["warning_count"]) == (0, 0)
    assert (data["routine_count"], data["dump_echo_count"]) == (61, 2)
    assert data["message_id_column"] == "MSGID"

    from reporting.excel_template_writer import _build_actual_result, _status_for_metric
    metric = _gui("SM21", **data)
    assert _status_for_metric(metric) == "OK"
    assert _build_actual_result(metric).endswith("routine: 61; dump messages: 2 (see ST22)")


def test_sm21_other_messages_still_alert():
    # Round 24: 1-4 errors WARNING, 5 or more CRITICAL.
    from reporting.excel_template_writer import _status_for_metric
    one = _PS4_SYSLOG + [_entry("BY2", "Database error 131 at SEL access to table MARC")]
    container = _Ctl("cont", type_="GuiContainer", Children=_Children([_SyslogGrid(one)]))
    data = ta.action_sm21(FakeSession(usr_children=[container]), lambda *a: None)
    assert data["error_count"] == 1
    assert _status_for_metric(_gui("SM21", **data)) == "WARNING"
    five = _PS4_SYSLOG + [_entry("BY2", "Database error 131 at SEL access to table MARC")] * 5
    container = _Ctl("cont", type_="GuiContainer", Children=_Children([_SyslogGrid(five)]))
    data = ta.action_sm21(FakeSession(usr_children=[container]), lambda *a: None)
    assert _status_for_metric(_gui("SM21", **data)) == "CRITICAL"


def test_type_and_client_columns_are_not_mistaken_for_the_message_id():
    rows = [{"TYPE": "DIA", "CLIENT": "500", "MSG": "R47"}, {"TYPE": "BTC", "CLIENT": "500", "MSG": "Q02"}]
    assert ta._sm21_id_column(rows) == "MSG"


def test_limits_and_daemon_lock_exclusion():
    from core.config_loader import get_thresholds
    from collectors.rfc_perf import LOCK_AGE_IGNORE_PREFIXES, _oldest_lock_detail
    from reporting.excel_template_writer import _status_for_metric
    limits = get_thresholds()
    assert limits["sap.sm12.oldest_lock_minutes"] == {"warning": 1440, "critical": 2880}
    assert limits["sap.st03.top_user_memory_mb"] == {"warning": 4096, "critical": 8192}
    assert "FDC_TIMERDAEMON_LOCK".startswith(LOCK_AGE_IGNORE_PREFIXES)
    detail = _oldest_lock_detail({"oldest": ("AVHP1001", "EKKO", "5000300080527"),
                                  "housekeeping_locks": [{"object": "FDC_TIMERDAEMON_LOCK"}]})
    assert detail.endswith("(standing locks not counted: FDC_TIMERDAEMON_LOCK)")
    assert _status_for_metric(_gui("SM12", lock_count=43)) == "OK"
