"""
Round 33: ABAP dumps via /SDF/GET_DUMP_LOG.

RFC_READ_TABLE refuses SNAP (TABLE_NOT_AVAILABLE) on CARFOUR QAS and PS4.
The ST-PI dump reader was checked against ST22 on 23.09.2026: CAQ 1 = 1,
PS4 18 = 18. Rows below are taken from that PS4 test.
"""
from core.models import Status
from collectors import rfc_collector
from collectors.rfc_collector import read_dump_log, _split_user_client, DUMP_LOG_FM
from collectors.rfc_live import _checks_from_metrics, _dumps


def _row(time, user, host, error, exc, comp, prog):
    return {"E2E_DATE": "20260923", "E2E_TIME": time, "E2E_USER": user,
            "E2E_SEVERITY": "1", "E2E_HOST": host, "FIELD1": error,
            "FIELD2": exc, "FIELD3": comp, "FIELD4": prog}


PS4_ROWS = [
    _row("173058", "58128_500", "vhrrnps4ai01_PS4_00", "TIME_OUT", "", "FI-GL-IS", "RFITEMAP"),
    _row("170519", "SAP_WFRT_500", "vhrrnps4ci_PS4_00", "SYNTAX_ERROR",
         "CX_SY_RTTI_SYNTAX_ERROR", "BC-ABA-LA", "CL_ABAP_TYPEDESCR=============CP"),
    _row("164202", "58128_500", "vhrrnps4ci_PS4_00", "TIME_OUT", "", "FI-GL-IS", "RFITEMAP"),
    _row("162426", "56798_500", "vhrrnps4ci_PS4_00", "CONVT_NO_NUMBER",
         "CX_SY_CONVERSION_NO_NUMBER", "UNKNOWN", "ZFI_TAQ_GL_BALANCE_UPLOAD"),
    _row("124806", "PS4_ADMIN_500", "vhrrnps4ci_PS4_00", "CALL_FUNCTION_SEND_ERROR", "",
         "BC-CCM-MON-TUN", "SAPLSCSM_NW_WORKLOAD"),
]


class _Session:
    ok = True

    def __init__(self, system="PS4", rows=None, error=None):
        self.system, self.rows, self.error = system, rows, error
        self.cfg = {"client": "500"}
        self.last_error = ""
        self.fm_calls = []
        self.snap_reads = 0

    def call(self, name, **kw):
        self.last_error = ""
        if name == DUMP_LOG_FM:
            self.fm_calls.append(kw)
            if self.error:
                self.last_error = f"{name}: ABAPApplicationError: Key: {self.error}"
                return None
            return {"ET_E2E_LOG": self.rows or []}
        return None

    def read_table(self, table, fields, where="", rows=500):
        if table == "SNAP":
            self.snap_reads += 1
            self.last_error = "RFC_READ_TABLE(SNAP): ABAPApplicationError: Key: TABLE_NOT_AVAILABLE"
        return None


def _st22(session):
    return next(m for m in rfc_collector._from_standard_modules(session)
                if m.name == "sap.st22.dumps")


def test_user_and_client_are_split():
    assert _split_user_client("58128_500") == ("58128", "500")
    assert _split_user_client("IB_SATYA_800") == ("IB_SATYA", "800")
    assert _split_user_client("SAP_WFRT_500") == ("SAP_WFRT", "500")
    assert _split_user_client("DDIC") == ("DDIC", "")


def test_dump_log_counts_todays_dumps_and_skips_snap():
    s = _Session(rows=PS4_ROWS)
    m = _st22(s)
    assert m.value == 5 and m.status != Status.UNKNOWN
    assert m.extra_data["dump_source"] == DUMP_LOG_FM
    assert m.extra_data["top_errors"].startswith("TIME_OUT 2")
    assert s.snap_reads == 0
    kw = s.fm_calls[0]
    assert kw["DATE_FROM"] == kw["DATE_TO"] and len(kw["DATE_FROM"]) == 8
    assert (kw["TIME_FROM"], kw["TIME_TO"]) == ("000000", "235959")


def test_no_data_found_is_zero_dumps_not_a_failure():
    s = _Session(error="NO_DATA_FOUND")
    m = _st22(s)
    assert m.value == 0 and m.status != Status.UNKNOWN
    assert s.snap_reads == 0


def test_missing_fm_falls_back_to_snap_and_is_not_asked_again():
    rfc_collector._dump_log_missing.discard("R33NOFM")
    s = _Session(system="R33NOFM", error="FU_NOT_FOUND")
    m = _st22(s)
    assert m.status == Status.UNKNOWN and "TABLE_NOT_AVAILABLE" in m.detail
    assert s.snap_reads >= 1
    _st22(s)
    assert len(s.fm_calls) == 1, "FU_NOT_FOUND is remembered per system"
    rfc_collector._dump_log_missing.discard("R33NOFM")


def test_not_authorized_reason_is_shown_with_the_snap_reason():
    m = _st22(_Session(system="R33AUTH", error="NOT_AUTHORIZED"))
    assert m.status == Status.UNKNOWN
    assert "NOT_AUTHORIZED" in m.detail and "TABLE_NOT_AVAILABLE" in m.detail


def test_wall_line_names_the_runtime_errors():
    s = _Session(rows=PS4_ROWS)
    rows = _checks_from_metrics(rfc_collector._from_standard_modules(s))
    st22 = next(r for r in rows if r["metric"] == "sap.st22.dumps")
    assert st22["value"] == "5 count"
    assert "TIME_OUT 2" in st22["sub"]


def test_breakdown_has_users_programs_and_errors():
    out = _dumps(_Session(rows=PS4_ROWS), "PS4")
    assert out["count"] == 5
    assert out["program_source"] == DUMP_LOG_FM
    assert out["by_user"][0]["user"] == "58128" and out["by_user"][0]["count"] == 2
    assert out["by_program"][0] == {"program": "RFITEMAP", "count": 2}
    assert out["by_error"][0] == {"error": "TIME_OUT", "count": 2}
    assert out["recent"][0]["time"] == "17:30:58"
    assert out["recent"][0]["program"] == "RFITEMAP"
    assert out["note"] is None
