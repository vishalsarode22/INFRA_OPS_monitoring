"""When SAP GUI did not run, the sheet and the PDF show the RFC figures and the reason."""
import os

from core.models import MetricResult, MonitoringResult, Status
from reporting import rfc_fallback as F

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _m(name, tcode, v, st, source="rfc_collector", detail=""):
    return MetricResult(name=name, value=float(v), display_value=f"{v} count", status=st, threshold_warning=1,
                        threshold_critical=5, source=source, tcode=tcode, detail=detail, category="x", unit="count")


def _result(reason=True):
    r = MonitoringResult(system="CEAT BW_PRD", client="940")
    r.metrics = [_m("sap.st22.dumps", "ST22", 3, Status.NORMAL),
                 _m("sap.sm37.cancelled_jobs", "SM37", 0, Status.NORMAL),
                 _m("sap.sm58.sysfail_backlog", "SM58", 6, Status.WARNING, detail="oldest 2019"),
                 _m("sap.smq2.entries", "SMQ2", 2, Status.WARNING),
                 _m("sap.st22.dumps", "ST22", 9, Status.CRITICAL, source="sap_gui_collector")]
    if reason:
        r.errors.append("sap_gui: other SAP GUI sessions were open on the monitoring VM")
    return r


def test_row_uses_rfc_and_never_the_gui_metric():
    actual, status, obs = F.row(_result(), "ST22")
    assert actual == "ABAP dumps today: 3 count" and status == "OK"
    assert "Read over RFC" in obs and "other SAP GUI sessions" in obs


def test_cancelled_row_and_warning_detail():
    assert F.row(_result(), "SM37_CANCELLED")[0] == "Cancelled jobs today: 0 count"
    actual, status, obs = F.row(_result(), "SM58")
    assert status == "WARNING" and "oldest 2019" in obs


def test_no_rfc_figure_says_why():
    actual, status, _ = F.row(_result(), "SP01")
    assert status == "UNKNOWN" and actual.startswith("Not captured this run -- other SAP GUI sessions")
    assert F.row(_result(reason=False), "SP01") is None


def test_sheet_and_pdf(tmp_path):
    from reporting.excel_template_writer import fill_metrobrands_template
    from reporting.production_reports import generate_system_pdf
    import openpyxl
    r = _result()
    r.metrics = [m for m in r.metrics if not m.source.startswith("sap_gui")]
    r.compute_overall_status()
    x = fill_metrobrands_template(os.path.join(_ROOT, "config", "templates", "InfraBeatOps_Simple_Monitoring_Sheet.xlsx"),
                                  str(tmp_path / "s.xlsx"), [], result=r)
    ws = openpyxl.load_workbook(x)["Monitoring Sheet"]
    rows = {row[6]: row for row in ws.iter_rows(min_row=4, values_only=True) if row[6]}
    assert rows["ST22"][8] == "ABAP dumps today: 3 count" and rows["ST22"][9] == "OK"
    assert rows["SMQ2"][9] == "WARNING"
    assert os.path.getsize(generate_system_pdf(r, [], output_path=str(tmp_path / "r.pdf"))) > 1000
