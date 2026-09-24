"""
Round 19: SM66 four-figure check, SM58 status text, DB12 running backup,
thresholds-based grading, SM58 <-> ST22 correlation, the PDF choosing the
capture record over a derived metric, and invalid AI keys.

Figures are the PS4 run of 22.09.2026 14:11.
"""
import time
from datetime import datetime

import pytest

import sap_gui.tcode_actions as ta
from core.models import MetricResult, MonitoringResult, Status

try:
    from test_round18_reads_and_reports import FakeSession, _Ctl, _Children, _gui
except ImportError:  # tests/ collected as a package
    from tests.test_round18_reads_and_reports import FakeSession, _Ctl, _Children, _gui


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(ta, "goto_tcode", lambda *a, **k: None)
    monkeypatch.setattr(ta, "wait_until_not_busy", lambda *a, **k: None)


class _WPGrid(_Ctl):
    COLUMNS = ["SERVER_NAME", "WP_INDEX", "WP_TYPE_DISP", "STATE_DISP",
               "STATE_INFO_DISP", "FAILURES", "WP_PROGRAM", "USER_NAME"]

    def __init__(self, rows):
        super().__init__("grid", type_="GuiShell", SubType="GridView", Children=_Children([]))
        self.set_rows(rows)
        self.ColumnCount, self.VisibleRowCount, self.firstVisibleRow = len(self.COLUMNS), 30, 0

    def set_rows(self, rows):
        self.rows, self.RowCount = rows, len(rows)

    def ColumnOrder(self, i): return self.COLUMNS[i]
    def GetCellValue(self, r, c): return self.rows[r].get(c, "")


def _wp(server, state, reason=""):
    return {"SERVER_NAME": server, "STATE_DISP": state, "STATE_INFO_DISP": reason}


# The 14:13 SM66 screen: 6 Running, 11 On Hold (one reason "PRIV" added here).
_ACTIVE = ([_wp("vhrrnps4ci_PS4_00", "Running")] * 3 + [_wp("vhrrnps4ai01_PS4_00", "Running")] * 3
           + [_wp("vhrrnps4ci_PS4_00", "On Hold", "ABAP WAIT")] * 5
           + [_wp("vhrrnps4ai01_PS4_00", "On Hold", "ABAP WAIT")] * 5
           + [_wp("vhrrnps4ai01_PS4_00", "On Hold", "PRIV")])
_ALL = _ACTIVE + [_wp("vhrrnps4ci_PS4_00", "Waiting")] * 40


def test_sm66_reports_active_on_hold_waiting_and_priv(monkeypatch):
    grid = _WPGrid(list(_ACTIVE))
    session = FakeSession(usr_children=[_Ctl("c", type_="GuiContainer", Children=_Children([grid]))])

    def press(_session, *groups):
        if groups[0] == ("all", "process"):
            grid.set_rows(list(_ALL))
            return True
        return False
    monkeypatch.setattr(ta, "_press_labelled_toolbar_button", press)

    shots = []
    data = ta.action_sm66(session, lambda *a: shots.append(a) or None)
    assert (data["active_processes"], data["running_processes"], data["on_hold_processes"]) == (17, 6, 11)
    assert data["waiting_processes"] == 40
    assert data["priv_mode_processes"] == 1
    assert len(shots) == 1, "screenshot stays on the active-only view"

    from reporting.excel_template_writer import _build_actual_result, _status_for_metric
    metric = _gui("SM66", **data)
    assert _build_actual_result(metric) == \
        "Active: 17 (Running 6, On Hold 11); Waiting: 40; PRIV mode: 1"
    assert _status_for_metric(metric) == "WARNING"


def test_sm66_waiting_is_not_invented_when_the_all_view_is_unavailable(monkeypatch):
    grid = _WPGrid(list(_ACTIVE[:16]))  # without the PRIV row
    session = FakeSession(usr_children=[_Ctl("c", type_="GuiContainer", Children=_Children([grid]))])
    monkeypatch.setattr(ta, "_press_labelled_toolbar_button", lambda *a: False)
    data = ta.action_sm66(session, lambda *a: None)
    assert data["waiting_processes"] is None and data["priv_mode_processes"] == 0
    from reporting.excel_template_writer import _build_actual_result
    assert "Waiting: not read" in _build_actual_result(_gui("SM66", **data))


def test_sm58_reads_the_status_text_of_each_entry():
    page = {(2, 1): "Information", (36, 2): "4", (36, 3): "4", (36, 4): "0",
            (1, 7): "Caller", (12, 7): "Function Module", (41, 7): "Target System",
            (62, 7): "Date", (73, 7): "Time", (82, 7): "Status Text", (126, 7): "Transaction ID"}
    for i, when in enumerate(("11:47:43", "11:46:46", "11:47:12", "14:11:19")):
        y = 9 + i
        page.update({(1, y): "10342", (12, y): "SWW_WI_CREATE_VIA_EVENT_IBF",
                     (41, y): "WORKFLOW_LOCAL_500", (62, y): "22.09.2026", (73, y): when,
                     (82, y): "Syntax error in program ZFI_WF_APP_DOA_CL=========",
                     (126, y): f"AC1CCA{i}"})
    data = ta.action_sm58(FakeSession([page]), lambda *a: None)
    assert data["failed_entries"] == 4 and len(data["trfc_rows"]) == 4
    assert data["status_texts"] == [{"text": "Syntax error in program ZFI_WF_APP_DOA_CL", "count": 4}]


def test_same_second_sm58_failures_and_st22_dumps_are_one_incident():
    from reporting.check_narratives import narrate_all, deterministic_analysis
    sm58 = _gui("SM58", trfc_status="CRITICAL", failed_entries=4,
                information={"entries_displayed": 4, "failed_entries": 4},
                trfc_rows=[{"date": "22.09.2026", "time": t,
                            "status_text": "Syntax error in program ZFI_WF_APP_DOA_CL",
                            "function_module": "SWW_WI_CREATE_VIA_EVENT_IBF"}
                           for t in ("11:47:43", "11:46:46", "11:47:12", "14:11:19")])
    dumps = [{"date": "22.09.2026", "time": t, "runtime_error": e, "user": u, "program": p}
             for t, e, u, p in (("14:11:18", "SYNTAX_ERROR", "SAP_WFRT", "CL_ABAP_TYPEDESCR=====CP"),
                                ("13:41:01", "TIME_OUT", "111159", "RM07MLBS"),
                                ("13:05:00", "RAISE_EXCEPTION", "62358", "SAPLESH_SR_LTXT_UPDATE"),
                                ("11:47:43", "SYNTAX_ERROR", "SAP_WFRT", "CL_ABAP_TYPEDESCR=====CP"),
                                ("11:47:12", "SYNTAX_ERROR", "SAP_WFRT", "CL_ABAP_TYPEDESCR=====CP"),
                                ("11:46:46", "SYNTAX_ERROR", "SAP_WFRT", "CL_ABAP_TYPEDESCR=====CP"))]
    st22 = _gui("ST22", dump_count=6, dumps=dumps)
    res = MonitoringResult(system="PS4", client="500")
    res.overall_status = Status.CRITICAL
    da = deterministic_analysis(res, narrate_all([sm58, st22]), [sm58, st22])
    assert len(da["incidents"]) == 1
    note = da["incidents"][0]
    assert "4 of 4" in note and "ZFI_WF_APP_DOA_CL" in note and "SAP_WFRT" in note

    from reporting.excel_template_writer import _build_observation
    assert _build_observation(st22, "CRITICAL").startswith(
        "SYNTAX_ERROR ×4 (CL_ABAP_TYPEDESCR, user SAP_WFRT); ")


def test_one_number_one_severity():
    from reporting.excel_template_writer import _status_for_metric
    assert _status_for_metric(_gui("SM12", lock_count=2211)) == "CRITICAL"  # critical >= 2000
    assert _status_for_metric(_gui("SM12", lock_count=680)) == "WARNING"    # warning >= 500
    assert _status_for_metric(_gui("SM12", lock_count=35)) == "OK"
    assert _status_for_metric(_gui("ST22", dump_count=6)) == "CRITICAL"     # critical >= 5
    assert _status_for_metric(_gui("ST22", dump_count=0)) == "OK"


def test_db12_reports_running_backup_and_grades_age():
    from reporting.excel_template_writer import _build_actual_result, _status_for_metric
    today = _gui("DB12", finished_at="2026-09-22T14:12:20",
                 latest_backup={"end_time": "21.09.2026 14:47:58", "status": "successful"},
                 running_backup={"start_time": "22.09.2026 13:46:19"})
    assert _status_for_metric(today) == "OK"
    assert _build_actual_result(today) == ("Last successful backup: 21.09.2026 14:47:58; "
                                           "backup running since 22.09.2026 13:46:19")
    stale = _gui("DB12", finished_at="2026-09-22T14:12:20",
                 latest_backup={"end_time": "20.09.2026 14:47:58", "status": "successful"})
    assert _status_for_metric(stale) == "WARNING"


def test_pdf_uses_the_capture_record_not_the_derived_smlg_metric():
    from reporting.check_narratives import narrate_all
    derived = MetricResult(name="sap.smlg.response_time", value=893.0, display_value="893 ms",
                           status=Status.NORMAL, source="sap_gui_collector", tcode="SMLG",
                           extra_data={"ocr_source": True, "alert_level": "normal"})
    capture = _gui("SMLG", instances=[
        {"instance": "vhrrnps4ai01_PS4_00", "response_time_ms": 893.0},
        {"instance": "vhrrnps4ci_PS4_00", "response_time_ms": 519.0}])
    (n,) = narrate_all([derived, capture])
    assert n.status == "OK"
    assert n.result == "vhrrnps4ai01_PS4_00: 893 ms; vhrrnps4ci_PS4_00: 519 ms"
    assert n.captured_at


def test_sm58_500_is_reported_as_a_read_limit():
    from reporting.check_narratives import narrate_all, deterministic_analysis
    sm58 = _gui("SM58", trfc_status="CRITICAL", failed_entries=4,
                information={"entries_displayed": 4, "failed_entries": 4})
    res = MonitoringResult(system="PS4", client="500")
    res.metrics = [MetricResult(name="sap.sm58.stuck_entries", value=500, display_value="500 count",
                                status=Status.CRITICAL)]
    da = deterministic_analysis(res, narrate_all([sm58]), [sm58])
    assert any("at least 500" in c and "lists 4" in c for c in da["conflicts"])


def test_screen_metrics_are_not_counted_as_rfc_breaches():
    from reporting.check_narratives import narrate_all, deterministic_analysis
    sm12 = _gui("SM12", lock_count=680)
    res = MonitoringResult(system="PS4", client="500")
    res.metrics = [
        MetricResult(name="sap.sm12.lock_count", value=680, display_value="680 count",
                     status=Status.CRITICAL, source="sap_gui_collector", tcode="SM12"),
        MetricResult(name="sap.st03.top_user_memory_mb", value=6525, display_value="6525 MB",
                     status=Status.CRITICAL, source="rfc"),
    ]
    da = deterministic_analysis(res, narrate_all([sm12]), [sm12])
    assert da["counts"]["metric_findings"] == 1 and da["counts"]["metric_critical"] == 1


def test_an_invalid_ai_key_is_retired_not_retried():
    from evaluation.providers.failover import _looks_like_quota
    assert _looks_like_quota(RuntimeError(
        'grok#4: HTTP 400 from xAI -- {"code":"invalid-argument","error":"Incorrect API key provided."}'))
    assert not _looks_like_quota(RuntimeError("Gemini transient HTTP 503"))
