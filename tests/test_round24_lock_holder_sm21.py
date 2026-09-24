"""
Round 24: the lock-holder line skips IBOPS's own processes and names the
monitoring account; SM21 grades 1-4 errors WARNING and says what the error
was; per-user lock limits in line with the lock-count limits.
Figures: PS4 run of 22.09.2026 17:28.
"""
import time

import pytest

import sap_gui.tcode_actions as ta
from core.models import MetricResult, MonitoringResult, Status

try:
    from test_round18_reads_and_reports import FakeSession, _Ctl, _Children, _gui
    from test_round23_noise import _SyslogGrid, _PS4_SYSLOG
except ImportError:  # tests/ collected as a package
    from tests.test_round18_reads_and_reports import FakeSession, _Ctl, _Children, _gui
    from tests.test_round23_noise import _SyslogGrid, _PS4_SYSLOG


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(ta, "goto_tcode", lambda *a, **k: None)
    monkeypatch.setattr(ta, "wait_until_not_busy", lambda *a, **k: None)


def _wp(index, wp_type, program, server, user="PS4_ADMIN"):
    return {"SERVER_NAME": server, "WP_INDEX": index, "WP_TYPE_DISP": wp_type,
            "STATE_DISP": "Running", "WP_PROGRAM": program, "USER_NAME": user}


def _analysis(rows, monkeypatch):
    import reporting.excel_template_writer as xw
    from reporting.check_narratives import narrate_all, deterministic_analysis
    monkeypatch.setattr(xw, "_system_config", lambda name: {"username": "ps4_admin"})
    res = MonitoringResult(system="PS4", client="500")
    res.overall_status = Status.CRITICAL
    res.metrics = [MetricResult(name="sap.sm12.locks_per_user_max", value=1001,
                                display_value="1001 count", status=Status.CRITICAL,
                                detail="PS4_ADMIN 1001, AVHP1001 60, SAP_WFRT 14")]
    gui = [_gui("SM12", lock_count=1085), _gui("SM66", process_rows=rows)]
    return [i for i in deterministic_analysis(res, narrate_all(gui), gui)["incidents"]
            if i.startswith("[SM12 + SM66]")]


def test_lock_holder_names_the_jobs_not_the_monitoring_session(monkeypatch):
    (note,) = _analysis([
        _wp("36", "DIA", "CL_SERVER_INFO================CP", "vhrrnps4ci_PS4_00"),
        _wp("58", "DIA", "CL_SERVER_INFO================CP", "vhrrnps4ai01_PS4_00"),
        _wp("69", "BTC", "SAPLSENA", "vhrrnps4ai01_PS4_00"),
        _wp("81", "BTC", "/UI5/APP_INDEX_CALCULATE", "vhrrnps4ai01_PS4_00"),
    ], monkeypatch)
    assert "User PS4_ADMIN (the account IBOPS logs on with) holds 1,001 lock entries of 1,085" in note
    assert "SAPLSENA in BTC work process 69" in note
    assert "/UI5/APP_INDEX_CALCULATE in BTC work process 81" in note
    assert "CL_SERVER_INFO" not in note
    assert "dedicated monitoring user" in note


def test_lock_holder_with_only_the_monitoring_session(monkeypatch):
    (note,) = _analysis([_wp("36", "DIA", "CL_SERVER_INFO================CP", "vhrrnps4ci_PS4_00")],
                        monkeypatch)
    assert "only active work processes are IBOPS's own monitoring session" in note


def test_sm21_says_what_the_error_was():
    r49 = {"DATE": "22.09.2026", "TIME": "16:30:21", "INSTANCE": "vhrrnps4ci_PS4_00", "TYPE": "DIA",
           "CLIENT": "500", "USER": "65142", "MSGID": "R49",
           "TEXT": "Communication error, CPIC-RC=CM_PARAMETER_ERROR(19), SAP-RC=CONV_ID_NOT_FOUND(728)"}
    container = _Ctl("cont", type_="GuiContainer", Children=_Children([_SyslogGrid(_PS4_SYSLOG + [r49])]))
    data = ta.action_sm21(FakeSession(usr_children=[container]), lambda *a: None)
    from reporting.excel_template_writer import _build_observation, _status_for_metric
    metric = _gui("SM21", **data)
    assert _status_for_metric(metric) == "WARNING"
    assert _build_observation(metric, "WARNING").startswith(
        "1 error-level entry: R49 at 16:30:21 user 65142 — Communication error, CPIC-RC=CM_PARAMETER_ERROR(19)")


def test_per_user_lock_limits():
    from core.config_loader import get_thresholds
    assert get_thresholds()["sap.sm12.locks_per_user_max"] == {"warning": 200, "critical": 1000}
