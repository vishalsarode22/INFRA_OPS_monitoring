"""
Round 25: fixes found by running the readers on a second system
(CARFOUR QAS, 23.09.2026) alongside PS4.
"""
import time

import pytest

import sap_gui.tcode_actions as ta
from core.models import MetricResult, MonitoringResult, Status

try:
    from test_round18_reads_and_reports import FakeSession, _gui
except ImportError:  # tests/ collected as a package
    from tests.test_round18_reads_and_reports import FakeSession, _gui


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(ta, "goto_tcode", lambda *a, **k: None)
    monkeypatch.setattr(ta, "wait_until_not_busy", lambda *a, **k: None)


def test_smlg_reads_a_screen_that_says_instance_not_application_server():
    # CARFOUR QAS 10:06: one instance, 33 ms. The caption is "Instance".
    page = {(1, 3): "Instance", (22, 3): "State", (28, 3): "Resp.time(ms)", (42, 3): "Thrshd",
            (49, 3): "User", (54, 3): "Thrshd", (61, 3): "Time", (70, 3): "Quality",
            (78, 3): "Dialog steps",
            (1, 5): "vhrrncaqci_CAQ_00", (38, 5): "33", (50, 5): "19", (61, 5): "10:02:28",
            (71, 5): "6", (86, 5): "12",
            (1, 7): "* Summary", (50, 7): "19",
            (1, 10): "Logon Group", (18, 10): "Current Instance",
            (1, 12): "PUBLIC", (18, 12): "vhrrncaqci_CAQ_00"}
    data = ta.action_smlg(FakeSession([page]), lambda *a: None)
    assert data["instances"] == [{"instance": "vhrrncaqci_CAQ_00", "state": "", "response_time_ms": 33.0,
                                  "response_time_raw": "33", "user_count": "19", "time": "10:02:28",
                                  "quality": "6", "dialog_steps": "12"}]
    assert data["response_time_ms"] == 33.0 and not data.get("extraction_failed")


def test_a_value_on_the_limit_grades_the_same_as_its_metric():
    from reporting.excel_template_writer import _status_for_metric
    # thresholds.yaml: sap.st22.dump_count warning 1, critical 5 -- the metric
    # engine grades "above" the limit, so 5 dumps is WARNING, 6 is CRITICAL.
    assert _status_for_metric(_gui("ST22", dump_count=5)) == "WARNING"
    assert _status_for_metric(_gui("ST22", dump_count=6)) == "CRITICAL"
    assert _status_for_metric(_gui("SM12", lock_count=500)) == "OK"
    assert _status_for_metric(_gui("SM12", lock_count=501)) == "WARNING"


def test_scot_without_an_smtp_node_says_what_it_means():
    from reporting.excel_template_writer import _build_observation
    scot = _gui("SCOT", status="WARNING", smtp_nodes=[],
                summary="No SMTP node configured: the SMTP Nodes list in SCOT is empty")
    assert _build_observation(scot, "WARNING").startswith("Outbound mail cannot leave the system")


def _analysis(metrics, gui):
    from reporting.check_narratives import narrate_all, deterministic_analysis
    res = MonitoringResult(system="PS4", client="500")
    res.overall_status = Status.WARNING
    res.metrics = metrics
    return deterministic_analysis(res, narrate_all(gui), gui)


def test_small_lock_differences_are_not_reported_as_a_conflict():
    metric = MetricResult(name="sap.sm12.locks_per_user_max", value=13, display_value="13 count",
                          status=Status.WARNING, detail="SAP_WFRT 13, BGRFC_SUPER 5")
    da = _analysis([metric], [_gui("SM12", lock_count=12)])
    assert not any(c.startswith("[SM12]") for c in da["conflicts"])
    far = MetricResult(name="sap.sm12.locks_per_user_max", value=80, display_value="80 count",
                       status=Status.WARNING, detail="SAP_WFRT 80")
    da = _analysis([far], [_gui("SM12", lock_count=12)])
    assert any(c.startswith("[SM12]") for c in da["conflicts"])


def test_cancelled_jobs_scope_difference_is_not_a_conflict():
    today_only = MetricResult(name="sap.sm37.cancelled_jobs", value=0, display_value="0 count",
                              status=Status.NORMAL, extra_data={"scope": "today"})
    gui = [_gui("SM37_CANCELLED", status="WARNING", cancelled_job_count=1,
                cancelled_job_names=["ZSD_LS_RET_ITEMS_PRICE_API"],
                date_filter={"from_date": "22.09.2026"})]
    assert not any(c.startswith("[SM37_CANCELLED]") for c in _analysis([today_only], gui)["conflicts"])
