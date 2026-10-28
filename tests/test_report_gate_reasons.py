"""The held-report mail says WHY a check is not filled."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import report_gate as rg  # noqa: E402
from core.models import MetricResult, Status  # noqa: E402


def _m(tcode, display="captured (1)", **data):
    return MetricResult(name=f"screenshot_{tcode}", value=None, display_value=display, status=Status.UNKNOWN,
                        tcode=tcode, extra_data=data)


def test_step_attempts_and_sap_message():
    r = rg.reason_for(_m("SP01", extraction_failed=True, failed_step="execute",
                         error="F8 (execute) failed: com_error: x", attempts=2,
                         status_message="Enter a valid value"))
    assert r.startswith("the selection was not executed (F8 failed)")
    assert "2 attempts" in r and "SAP said: 'Enter a valid value'" in r


def test_authorisation_is_named():
    r = rg.reason_for(_m("SP01", status_message="You are not authorized to use transaction SP01"))
    assert r.startswith("the SAP user is not authorised")


def test_failed_capture_and_missing_result():
    r = rg.reason_for(_m("SP01", display="failed", error="com_error: The control could not be found by id.",
                         attempts=3))
    assert "the SAP GUI step failed: com_error" in r and "3 attempts" in r
    assert "no result came back" in rg.reason_for(None)


def test_password_text_is_masked():
    r = rg.reason_for(_m("SP01", error="logon failed password=Secret123", extraction_failed=True))
    assert "Secret123" not in r


def test_check_returns_details(tmp_path=None):
    import tempfile
    import openpyxl
    d = tempfile.mkdtemp()
    path = os.path.join(d, "s.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Monitoring Sheet"
    ws.append(["T-Code", "Status", "Actual Result"])
    ws.append(["SP01", "UNKNOWN", "Not measured: the spool list could not be read"])
    ws.append(["SM12", "OK", "2 locks"])
    wb.save(path)
    shot = os.path.join(d, "x.png")
    open(shot, "wb").close()
    gui = [MetricResult(name="screenshot_SP01", value=None, display_value="captured (1)", status=Status.UNKNOWN,
                        tcode="SP01", screenshot_path=shot, screenshot_paths=[shot],
                        extra_data={"extraction_failed": True, "failed_step": "list", "attempts": 1,
                                    "error": "spool list could not be read (no 'Spool no.' header row)"}),
           MetricResult(name="screenshot_SM12", value=None, display_value="captured (1)", status=Status.UNKNOWN,
                        tcode="SM12", screenshot_path=shot, screenshot_paths=[shot])]
    orig = rg._expected_tcodes
    rg._expected_tcodes = lambda cfg: ["SP01", "SM12"]
    try:
        g = rg.check({"name": "T"}, True, "", gui, path)
    finally:
        rg._expected_tcodes = orig
    assert not g["complete"]
    assert list(g["details"]) == ["SP01"]
    assert "the result list could not be read" in g["details"]["SP01"]
    assert rg._detail_lines(g)[0].startswith("SP01 — not filled: ")
