"""
Regression tests for the RFC performance collector. Fakes only.

Covers: PRIV detection across instances, per-instance response time and the
connected-only fallback, RAW1 task type from pyrfc, lock aggregation with
age, SQLMD role mapping against a discovered shape, and the essential-column
skip that must log rather than guess.
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import rfc_perf as rp
from core.models import Status


class FakeSession:
    def __init__(self, fms: dict, tables: dict | None = None):
        self.fms = fms          # name -> result or callable(kwargs)->result
        self.tables = tables or {}
        self.calls = []
        self.ok = True

    def call(self, name, **kw):
        self.calls.append((name, kw))
        v = self.fms.get(name)
        return v(kw) if callable(v) else v

    def read_table(self, table, fields, where="", rows=500):
        t = self.tables.get(table)
        if t is None:
            return None
        return [[str(r.get(f, "")) for f in fields] for r in t]


def _wp(no, typ, status, user="", report="", eltime=0, reason="", pid="1"):
    return {"WP_NO": no, "WP_TYP": typ, "WP_STATUS": status, "WP_BNAME": user,
            "WP_REPORT": report, "WP_ELTIME": eltime, "WP_WAITING": reason, "WP_PID": pid}


SERVERS = {"LIST": [{"NAME": "sapprd01_PRD_00", "HOST": "sapprd01"},
                    {"NAME": "sapprd02_PRD_00", "HOST": "sapprd02"}]}


def _by_key(session, client="100"):
    rp._shape_cache.clear()
    rp._sap_clock.clear()       # clock is learned per poll; never carry it between tests
    rp._owner_offset_min.clear()
    # "SQL Monitor is off for PRD" is remembered for the whole process once
    # learned (rp._sqlm_off), so a test that saw an empty SQLMD would make
    # every later SQLMD test in the session skip the scan and KeyError. Each
    # test gets its own session, so each starts with a clean memo.
    rp.reset_sqlm_off()
    return {m.name: m for m in rp.build_metrics("PRD", session, client)}


def test_priv_and_long_running_across_instances():
    def wpinfo(kw):
        if kw.get("SRVNAME", "").startswith("sapprd01"):
            return {"WPLIST": [_wp(0, "DIA", "Running", "JSMITH", "ZMM_REPORT", 1400, "PRIV"),
                               _wp(1, "DIA", "Waiting"), _wp(2, "BTC", "Running")]}
        return {"WPLIST": [_wp(0, "DIA", "hold", "AKUMAR", "SAPLMEPO", 30, "PRIV"),
                           _wp(1, "DIA", "Running", "BJONES", "RSNAST00", 900),
                           _wp(2, "DIA", "Waiting"), _wp(3, "DIA", "Waiting")]}
    s = FakeSession({"TH_SERVER_LIST": SERVERS, "TH_WPINFO": wpinfo})
    m = _by_key(s)
    assert m["sap.sm50.priv_mode_wp"].value == 2
    assert m["sap.sm50.priv_mode_wp"].status == Status.WARNING
    assert "JSMITH ZMM_REPORT" in m["sap.sm50.priv_mode_wp"].detail
    # long-running excludes the waiting ones and counts >= 600s busy DIA
    assert m["sap.sm50.long_running_wp"].value == 2
    sat = m["sap.sm66.max_instance_saturation_pct"]
    assert sat.value == 50 and sat.extra_data["worst_instance"] == "sapprd01_PRD_00"
    # per-instance addressing actually used
    assert any(kw.get("SRVNAME") == "sapprd02_PRD_00" for n, kw in s.calls if n == "TH_WPINFO")


def test_wpinfo_falls_back_without_srvname_on_old_kernel():
    def wpinfo(kw):
        return None if "SRVNAME" in kw else {"WPLIST": [_wp(0, "DIA", "Running", eltime=5)]}
    s = FakeSession({"TH_SERVER_LIST": SERVERS, "TH_WPINFO": wpinfo})
    m = _by_key(s)
    assert m["sap.sm50.priv_mode_wp"].value == 0


def _frame(instance, recs):
    return {"SYSTEMID": "PRD", "INSTANCE": instance, "INTERVAL_COMPLETED": "X", "STATRECS": recs}


from contextlib import contextmanager


@contextmanager
def _min_steps(n):
    """
    Temporarily relax MIN_DIALOG_STEPS.

    build_metrics() falls back to the ST03N daily aggregate whenever the
    live STAT window holds fewer than MIN_DIALOG_STEPS dialog steps, and
    omits the metric entirely when there is no aggregate either. Tests that
    exercise record SHAPE rather than grading use a handful of records on
    purpose, so they lower the threshold instead of padding the fixture with
    rows that would change every median, mean and per-instance assertion.
    """
    original = rp.MIN_DIALOG_STEPS
    rp.MIN_DIALOG_STEPS = n
    try:
        yield
    finally:
        rp.MIN_DIALOG_STEPS = original


def _rec(tasktype, resp_ms, user, maxbytes=0, priv=b"\x00", db=None, report="", tcode="", dsql=0):
    """
    One live STAT record, built from a response time given in MILLISECONDS.

    RESPTI from SWNC_GET_STATRECS_FRAME is in MICROSECONDS and the collector
    divides by 1000. Writing an ms figure straight into RESPTI only matched
    reality while that division was missing, so these fixtures encoded the
    1000x inflation bug and began failing when it was fixed. Converting here
    keeps every call site and assertion readable in ms.

    Note this applies to the live STAT path only. RESPTI in
    SWNC_COLLECTOR_GET_AGGREGATES is a total in ms per bucket, so the
    aggregate fixtures further down are deliberately left unscaled.
    """
    main = {"TASKTYPE": tasktype, "RESPTI": resp_ms * 1000, "ACCOUNT": user, "MAXBYTES": maxbytes,
            "PRIVMODE": priv, "CPUTI": 10, "QUEUETI": 1, "REPORT": report, "TCODE": tcode,
            "DSQLCNT": dsql}
    if db is not None:
        main.update({"DBREQTIME": db, "DBPREQTIME": 0})     # confirmed live field names
    return {"MAINREC": main, "DBRECS": [], "TABLERECS": []}


def test_response_time_from_real_all_statrecs_shape():
    # Shape confirmed live on CENTOR_QAS: ALL_STATRECS -> frames -> STATRECS -> MAINREC
    def stat(kw):
        assert "INSTANCE" not in kw and "SERVER" not in kw     # one call, no addressing
        return {"ALL_STATRECS": [
            _frame("sapprd01_PRD_00", [_rec(b"\x01", 800, "U1", 900 * 1048576, db=200, report="SAPLMEPO", tcode="ME21N", dsql=40),
                                       _rec(b"\x01", 800, "U2", 100 * 1048576, db=200, report="SAPLMEPO", tcode="ME23N", dsql=40),
                                       _rec(b"f", 73243, "UNKNOWN")]),          # RFC step: excluded
            _frame("sapprd02_PRD_00", [_rec(b"\x01", 2400, "U3", 2200 * 1048576, priv=b"\x01", db=600, report="ZMM_REPORT", tcode="ZMM01", dsql=5000),
                                       _rec(b"\x04", 90000, "BATCH")]),        # background: excluded
        ], "EXT_ESI_RECORDS": [], "PROTOCOL": [], "TREX_RECORDS": [], "WEBSERVICE_RECORDS": []}
    s = FakeSession({"TH_SERVER_LIST": SERVERS, "SWNC_GET_STATRECS_FRAME": stat})
    # Three dialog steps is a shape fixture, not a grading fixture.
    with _min_steps(1):
        m = _by_key(s)
    r = m["sap.st03.dialog_resp_ms"]
    # The tile value is the MEDIAN step response, not the mean: one stuck
    # 460s step used to drag a 300ms system to "464549 ms" on the wall. The
    # mean is still published (mean_resp_ms) and the detail names both.
    assert r.value == 800                                   # median of 800, 800, 2400
    assert r.extra_data["mean_resp_ms"] == round((800 + 800 + 2400) / 3)
    assert r.extra_data["per_instance"] == {"sapprd01_PRD_00": 800, "sapprd02_PRD_00": 2400}
    assert r.extra_data["note"] == ""
    assert r.extra_data["task_mix"] == {"DIALOG": 3, "RFC": 1, "BTC": 1}
    by_db = r.extra_data["top_reports_by_db_time"]
    assert by_db[0]["report"] == "ZMM_REPORT" and by_db[0]["custom"] is True
    assert by_db[0]["tcodes"] == ["ZMM01"] and by_db[0]["db_calls"] == 5000
    assert by_db[1] == {"report": "SAPLMEPO", "custom": False, "steps": 2, "total_resp_ms": 1600,
                        "total_db_ms": 400, "avg_resp_ms": 800, "max_mb": 900.0, "db_calls": 80,
                        "tcodes": ["ME21N", "ME23N"], "users": ["U1", "U2"]}
    assert m["sap.st03.top_report_db_ms"].value == 600
    assert "ZMM_REPORT" in m["sap.st03.top_report_db_ms"].detail
    assert m["sap.st03.max_instance_resp_ms"].value == 2400
    assert m["sap.st03.db_time_pct"].value == 25.0
    mem = m["sap.st03.top_user_memory_mb"]
    assert mem.value == 2200.0 and mem.status == Status.CRITICAL
    assert mem.detail.startswith("U3 2200.0MB on sapprd02_PRD_00")
    assert mem.extra_data["priv_mode_steps"] == 1


def test_tasktype_accepts_bytes_hexstring_and_repr():
    assert rp._tasktype_is_dialog(b"\x01")
    assert rp._tasktype_is_dialog("01")
    assert rp._tasktype_is_dialog("b'\\x01'")
    assert not rp._tasktype_is_dialog(b"f")
    assert rp._tasktype_code(b"f") == 0x66


def test_idle_window_emits_no_response_metric():
    def stat(kw):
        return {"ALL_STATRECS": [_frame("qassrv_QAS_00", [_rec(b"f", 70000, "UNKNOWN")])]}
    s = FakeSession({"TH_SERVER_LIST": [], "SWNC_GET_STATRECS_FRAME": stat})
    m = _by_key(s)
    assert not any(k.startswith("sap.st03.") for k in m)


def test_lock_aggregation_users_dups_and_age():
    old = (datetime.now() - timedelta(minutes=95))
    enq = [{"GUNAME": "JSMITH", "GNAME": "EVVBAKE", "GARG": "1000000123",
            "GTDATE": old.strftime("%Y%m%d"), "GTTIME": old.strftime("%H%M%S")}]
    enq += [{"GUNAME": "JSMITH", "GNAME": "EVVBAKE", "GARG": f"10000{i:05d}"} for i in range(11)]
    enq += [{"GUNAME": "AKUMAR", "GNAME": "EMMARAE", "GARG": "X"}] * 3
    s = FakeSession({"ENQUE_READ2": {"ENQ": enq}})
    m = _by_key(s)
    assert m["sap.sm12.locks_per_user_max"].value == 12
    assert m["sap.sm12.users_with_many_locks"].value == 1
    assert m["sap.sm12.oldest_lock_minutes"].value in (94, 95, 96)
    assert m["sap.sm12.oldest_lock_minutes"].status == Status.WARNING
    dups = m["sap.sm12.locks_per_user_max"].extra_data["same_object_dups"]
    assert dups[0] == (("AKUMAR", "EMMARAE", "X"), 3)


def test_sqlm_maps_real_release_columns():
    # Exact shape returned by DDIF_FIELDINFO_GET on CENTOR_QAS, 2026-09-07.
    ddif = {"DFIES_TAB": [{"FIELDNAME": f} for f in
            ["PROGNAME", "PROGKIND", "PROCTYPE", "PROCNAME", "PROCLINE", "RUNLEVEL", "ROOTTYPE",
             "ROOTNAME", "STMTKIND", "TABLENAME", "XCNT", "RCNT", "XCNTRCNT", "RTMIN", "RTMAX",
             "RTCNT", "RTSUM", "RTSQS", "RTAVG", "RTDEV", "DBMIN", "DBMAX", "DBCNT", "DBSUM",
             "DBSQS", "DBAVG", "DBDEV", "DDATE", "DTIME", "RTMCNT"]]}
    rows = [
        {"PROGNAME": "ZMM_REPORT", "PROCNAME": "ZMM_REPORT_F01", "PROCLINE": "412", "STMTKIND": "SELECT",
         "TABLENAME": "MSEG", "ROOTNAME": "ZMM01", "ROOTTYPE": "TRAN", "XCNT": "1200", "RCNT": "5000000",
         "DBSUM": str(400 * 1_000_000), "DBMAX": "9000000", "RTSUM": str(420 * 1_000_000), "DDATE": "20260907"},
        {"PROGNAME": "ZMM_REPORT", "PROCNAME": "ZMM_REPORT_F01", "PROCLINE": "430", "STMTKIND": "SELECT",
         "TABLENAME": "MKPF", "ROOTNAME": "ZMM01", "ROOTTYPE": "TRAN", "XCNT": "1200", "RCNT": "1000",
         "DBSUM": str(50 * 1_000_000), "DBMAX": "10000", "RTSUM": str(55 * 1_000_000), "DDATE": "20260907"},
        {"PROGNAME": "SAPLMEPO", "PROCNAME": "LMEPOF01", "PROCLINE": "10", "STMTKIND": "SELECT",
         "TABLENAME": "EKPO", "ROOTNAME": "ME21N", "ROOTTYPE": "TRAN", "XCNT": "90000", "RCNT": "90000",
         "DBSUM": str(20 * 1_000_000), "DBMAX": "5000", "RTSUM": str(25 * 1_000_000), "DDATE": "20260907"},
    ]
    s = FakeSession({"DDIF_FIELDINFO_GET": ddif}, tables={"SQLMD": rows})
    m = _by_key(s)
    e = m["sap.sqlm.expensive_programs"]
    assert e.value == 1 and "ZMM_REPORT 450.0s" in e.detail
    top = e.extra_data["top"][0]
    assert top["program"] == "ZMM_REPORT" and top["custom"] is True
    assert top["execs"] == 2400 and top["tables"] == ["MKPF", "MSEG"]
    assert top["entry_points"] == ["ZMM01"] and top["rt_s"] == 475.0
    assert e.extra_data["columns"]["total_us"] == "DBSUM"
    assert m["sap.sqlm.top_program_total_s"].value == 450.0
    assert m["sap.sqlm.top_program_total_s"].status == Status.WARNING


def test_sqlm_read_is_date_restricted_when_ddate_exists():
    ddif = {"DFIES_TAB": [{"FIELDNAME": f} for f in ["PROGNAME", "XCNT", "DBSUM", "DDATE"]]}
    class Spy(FakeSession):
        def read_table(self, table, fields, where="", rows=500):
            self.where = where; self.rows = rows
            return super().read_table(table, fields, where, rows)
    s = Spy({"DDIF_FIELDINFO_GET": ddif}, tables={"SQLMD": []})
    _by_key(s)
    assert s.where.startswith("DDATE >= '") and s.rows == rp.SQLM_ROWS


def test_sqlm_maps_alternate_release_columns():
    ddif = {"DFIES_TAB": [{"FIELDNAME": f} for f in
            ["PROGRAM_NAME", "TABLE_NAMES", "EXEC_COUNT", "TOTAL_TIME", "MAX_TIME", "TOTAL_RECORDS"]]}
    rows = [{"PROGRAM_NAME": "ZX", "TABLE_NAMES": "T1", "EXEC_COUNT": "1",
             "TOTAL_TIME": str(70 * 1_000_000), "MAX_TIME": "1", "TOTAL_RECORDS": "1"}]
    s = FakeSession({"DDIF_FIELDINFO_GET": ddif}, tables={"SQLMD": rows})
    m = _by_key(s)
    assert m["sap.sqlm.expensive_programs"].value == 1


def test_sqlm_skipped_when_essential_columns_missing():
    ddif = {"DFIES_TAB": [{"FIELDNAME": f} for f in ["SOMETHING", "ELSE"]]}
    s = FakeSession({"DDIF_FIELDINFO_GET": ddif}, tables={"SQLMD": [{"SOMETHING": "x"}]})
    m = _by_key(s)
    assert not any(k.startswith("sap.sqlm.") for k in m)


def test_sqlm_absent_table_is_silent():
    s = FakeSession({"DDIF_FIELDINFO_GET": None})
    m = _by_key(s)
    assert not any(k.startswith("sap.sqlm.") for k in m)


def test_nothing_readable_gives_no_metrics():
    assert rp.build_metrics("PRD", FakeSession({}), "100") == []


def test_all_metrics_tagged():
    s = FakeSession({"ENQUE_READ2": {"ENQ": []}})
    for m in rp.build_metrics("PRD", s, "100"):
        assert m.source == "rfc_perf" and m.extra_data["collector"] == "RFC_PERF"


def test_lock_age_parsed_from_owner_id_when_no_gtdate():
    old = datetime.now() - timedelta(minutes=45)
    enq = [{"GUNAME": "HG009632", "GNAME": "E_ABAP_GENPH", "GARG": "X",
            "GUSR": old.strftime("%Y%m%d%H%M%S") + "126635000900qassrv"}]
    s = FakeSession({"ENQUE_READ2": {"ENQ": enq}})
    m = _by_key(s)
    assert m["sap.sm12.oldest_lock_minutes"].value in (44, 45, 46)
    assert m["sap.sm12.oldest_lock_minutes"].status == Status.WARNING


def test_st03n_aggregate_used_when_stat_returns_nothing():
    # RESPTI in SWNC_COLLECTOR_GET_AGGREGATES is total ms per bucket; COUNT is
    # steps. Confirmed live: RESPTI 2075 over COUNT 14 = 148 ms/step.
    def stat(kw):
        return {"STATRECS": []}
    def agg(kw):
        assert kw["COMPONENT"] == "qassrv_QAS_00" and kw["ASSIGNDSYS"] == "QAS"
        return {"TASKTIMES": [
            {"TASKTYPE": b"\x01", "COUNT": 400, "RESPTI": 400 * 650, "READSEQTI": 400 * 200,
             "READDIRTI": 0, "CHNGTI": 0},
            {"TASKTYPE": b"\x04", "COUNT": 10, "RESPTI": 10 * 90000},
        ], "USERTCODE": [
            {"TASKTYPE": b"\x01", "ACCOUNT": "U1", "RESPTI": 100000},
        ]}
    servers = {"LIST": [{"NAME": "qassrv_QAS_00", "HOST": "qassrv"}]}
    s = FakeSession({"TH_SERVER_LIST": servers, "SWNC_GET_STATRECS_FRAME": stat,
                     "SWNC_COLLECTOR_GET_AGGREGATES": agg})
    m = _by_key(s)
    r = m["sap.st03.dialog_resp_ms"]
    assert r.value == 650 and "ST03N daily aggregate" in r.detail
    assert r.extra_data["per_instance"] == {"qassrv_QAS_00": 650}
    assert m["sap.st03.db_time_pct"].value == round(200 / 650 * 100, 1)
    assert r.extra_data["top_users_by_total_ms"][0]["user"] == "U1"


def test_low_sample_response_is_not_published_as_a_verdict():
    """
    An idle QAS with a single slow dialog step must not paint the tile.

    The original form of this test asserted that the figure was still SHOWN,
    graded NORMAL, carrying a low_sample flag and a "near-idle" note. That
    design was superseded: build_metrics() now falls back to the ST03N daily
    aggregate below MIN_DIALOG_STEPS, and omits the metric altogether when no
    aggregate is available. Showing nothing is the stronger guarantee -- a
    one-step average is not a reading, and a tile with a number on it invites
    a conclusion however it is graded.

    See also: the low_sample branch in rfc_perf.build_metrics is now
    unreachable, because the threshold check above it replaces rs first.
    """
    def stat(kw):
        # one dialog record at 3712 ms (RESPTI in microseconds), plus batch noise
        return {"ALL_STATRECS": [
            {"TASKTYPE": b"\x01", "RESPTI": str(3712 * 1000), "ACCOUNT": "3318",
             "STARTDATE": "20260908", "STARTTIME": "115853", "_instance": "srlqsap_QA1_00"},
            {"TASKTYPE": b"\xfe", "RESPTI": str(1024 * 1000), "ACCOUNT": "SAPSYS",
             "STARTDATE": "20260908", "STARTTIME": "115853", "_instance": "srlqsap_QA1_00"},
        ]}
    servers = {"LIST": [{"NAME": "srlqsap_QA1_00", "HOST": "srlqsap"}]}
    s = FakeSession({"TH_SERVER_LIST": servers, "SWNC_GET_STATRECS_FRAME": stat})
    m = _by_key(s, client="500")
    assert "sap.st03.dialog_resp_ms" not in m
    assert "sap.st03.max_instance_resp_ms" not in m


def test_low_sample_is_shown_when_the_threshold_allows_it():
    """The same single step IS published once MIN_DIALOG_STEPS permits it,
    which keeps the parsing path covered rather than only its suppression."""
    def stat(kw):
        return {"ALL_STATRECS": [
            {"TASKTYPE": b"\x01", "RESPTI": str(3712 * 1000), "ACCOUNT": "3318",
             "STARTDATE": "20260908", "STARTTIME": "115853", "_instance": "srlqsap_QA1_00"},
        ]}
    servers = {"LIST": [{"NAME": "srlqsap_QA1_00", "HOST": "srlqsap"}]}
    s = FakeSession({"TH_SERVER_LIST": servers, "SWNC_GET_STATRECS_FRAME": stat})
    with _min_steps(1):
        m = _by_key(s, client="500")
    assert m["sap.st03.dialog_resp_ms"].value == 3712


def test_ample_sample_still_grades_normally():
    # 12 dialog steps averaging ~3000 ms SHOULD grade (above warn threshold).
    def stat(kw):
        recs = [{"TASKTYPE": b"\x01", "RESPTI": str(3000 * 1000), "ACCOUNT": "U1",
                 "STARTDATE": "20260908", "STARTTIME": "115853",
                 "_instance": "srlqsap_QA1_00"} for _ in range(12)]
        return {"ALL_STATRECS": recs}
    servers = {"LIST": [{"NAME": "srlqsap_QA1_00", "HOST": "srlqsap"}]}
    s = FakeSession({"TH_SERVER_LIST": servers, "SWNC_GET_STATRECS_FRAME": stat})
    m = _by_key(s, client="500")
    r = m["sap.st03.dialog_resp_ms"]
    assert r.value == 3000
    assert r.extra_data["low_sample"] is False
    assert r.status in (Status.WARNING, Status.CRITICAL)   # graded, as it should be


def test_connected_only_e_statrecs_shape_is_read():
    """
    The older single-list reply shape (E_STATRECS, no per-instance frames).

    This assertion used to sit at the foot of test_ample_sample_still_grades_
    normally, where it shared that test's name and could only run if the
    grading assertions above it passed -- so it was invisible for as long as
    they failed. It covers a different reply shape and belongs on its own.
    """
    def stat(kw):
        return {"E_STATRECS": [{"TASKTYPE": "01", "RESPTI": 700 * 1000, "ACCOUNT": "U1"}],
                "RETURN": "X"}
    s = FakeSession({"TH_SERVER_LIST": SERVERS, "SWNC_GET_STATRECS_FRAME": stat})
    with _min_steps(1):
        m = _by_key(s)
    assert m["sap.st03.dialog_resp_ms"].value == 700


def test_aggregate_fallback_tries_yesterday_when_today_has_no_data():
    calls = []
    def agg(kw):
        calls.append((kw["COMPONENT"], kw["PERIODSTRT"]))
        if kw["PERIODSTRT"] == datetime.now().strftime("%Y%m%d"):
            return None                                 # NO_DATA_FOUND today
        return {"TASKTIMES": [{"TASKTYPE": b"\x01", "COUNT": 10, "RESPTI": 5000, "DBTIME": 1000}]}
    servers = {"LIST": [{"NAME": "qassrv_QAS_00", "HOST": "qassrv"}]}
    s = FakeSession({"TH_SERVER_LIST": servers,
                     "SWNC_GET_STATRECS_FRAME": {"ALL_STATRECS": []},
                     "SWNC_COLLECTOR_GET_AGGREGATES": agg})
    m = _by_key(s)
    assert m["sap.st03.dialog_resp_ms"].value == 500
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    assert ("qassrv_QAS_00", yesterday) in calls


def test_lock_age_uses_sap_clock_when_host_clock_is_behind():
    rp._sap_clock.clear()
    # SAP system is 10 minutes AHEAD of the host. A lock set 3 minutes ago in
    # SAP time is 7 minutes in the FUTURE by host time -> was clamped to 0.
    sap_now = datetime.now() + timedelta(minutes=10)
    set_at = sap_now - timedelta(minutes=3)
    utc_end = (sap_now - timedelta(hours=5, minutes=30)).strftime("%Y%m%d%H%M%S")   # INDIA offset
    rec_local = (sap_now - timedelta(seconds=30))
    rec_utc = rec_local - timedelta(hours=5, minutes=30)
    def stat(kw):
        return {"ALL_STATRECS": [{"INSTANCE": "sybase1_PRD_00", "ENDTIMESTAMP": utc_end, "STATRECS": [
            {"MAINREC": {"TASKTYPE": b"\x04", "RESPTI": 1, "ACCOUNT": "BGCLOUD",
                         "STARTDATE": rec_local.strftime("%Y%m%d"), "STARTTIME": rec_local.strftime("%H%M%S"),
                         "STARTTIMESTAMP": rec_utc.strftime("%Y%m%d%H%M%S")}}]}]}
    enq = [{"GUNAME": "BGCLOUD", "GNAME": "EMMARAE", "GARG": "X",
            "GUSR": set_at.strftime("%Y%m%d%H%M%S") + "000000000900sybase1"}]
    s = FakeSession({"TH_SERVER_LIST": {"LIST": [{"NAME": "sybase1_PRD_00", "HOST": "sybase1"}]},
                     "SWNC_GET_STATRECS_FRAME": stat, "ENQUE_READ2": {"ENQ": enq}})
    m = _by_key(s)
    lock = m["sap.sm12.oldest_lock_minutes"]
    assert lock.value == 3
    assert lock.extra_data["clock_source"] == "sap"
    assert abs((rp.sap_now("PRD") - sap_now).total_seconds()) < 2


def test_lock_age_falls_back_to_host_clock_without_stat_frame():
    rp._sap_clock.clear()
    old = datetime.now() - timedelta(minutes=20)
    enq = [{"GUNAME": "U", "GNAME": "E", "GARG": "X", "GUSR": old.strftime("%Y%m%d%H%M%S") + "000000000900h"}]
    s = FakeSession({"ENQUE_READ2": {"ENQ": enq}})
    m = _by_key(s)
    assert m["sap.sm12.oldest_lock_minutes"].value == 20
    assert m["sap.sm12.oldest_lock_minutes"].extra_data["clock_source"] == "host"


def test_owner_stamp_offset_inferred_and_ages_corrected():
    # PRD, 2026-09-07: SAP time 17:21, every owner stamp ~5:30 ahead.
    rp._sap_clock.clear(); rp._owner_offset_min.clear()
    sap = datetime(2026, 9, 7, 17, 21, 41)
    rp._sap_clock["PRD"] = sap
    def stamp(dt):
        return dt.strftime("%Y%m%d%H%M%S") + "484300001200syba"
    enq = [
        {"GUNAME": "BASIS2",  "GNAME": "VBAK",           "GARG": "1", "GUSR": stamp(datetime(2026, 9, 7, 22, 50, 54))},
        {"GUNAME": "BGCLOUD", "GNAME": "/SDF/CALM_HM_K", "GARG": "2", "GUSR": stamp(datetime(2026, 9, 7, 21, 4, 41))},
        {"GUNAME": "BASIS2",  "GNAME": "/SDF/SMON_CALL", "GARG": "3", "GUSR": stamp(datetime(2026, 9, 7, 18, 3, 48))},
    ]
    s = FakeSession({"ENQUE_READ2": {"ENQ": enq}})
    m = {x.name: x for x in rp.build_metrics("PRD", s, "100")}
    lock = m["sap.sm12.oldest_lock_minutes"]
    assert lock.extra_data["owner_clock_offset_min"] == 330
    # /SDF/ housekeeping locks (SMON 288 min, CALM 107 min) are excluded from
    # the age metric; the oldest *application* lock is BASIS2's VBAK, ~1 min.
    assert lock.value == 1 and lock.status == Status.NORMAL
    assert lock.extra_data["oldest"][1] == "VBAK"
    hk = {h["object"]: h["age_min"] for h in lock.extra_data["housekeeping_locks"]}
    assert hk == {"/SDF/SMON_CALL": 288, "/SDF/CALM_HM_K": 107}
    assert m["sap.sm12.locks_per_user_max"].value == 2      # still counted per user


def test_owner_stamp_offset_zero_when_stamps_are_in_the_past():
    rp._sap_clock.clear(); rp._owner_offset_min.clear()
    old = datetime.now() - timedelta(minutes=20)
    enq = [{"GUNAME": "U", "GNAME": "E", "GARG": "X", "GUSR": old.strftime("%Y%m%d%H%M%S") + "000000000900h"}]
    m = {x.name: x for x in rp.build_metrics("PRD", FakeSession({"ENQUE_READ2": {"ENQ": enq}}), "100")}
    assert m["sap.sm12.oldest_lock_minutes"].extra_data["owner_clock_offset_min"] == 0
    assert m["sap.sm12.oldest_lock_minutes"].value == 20


def test_padded_lock_argument_is_cleaned():
    assert rp._s("TVFKT 100" + "\uffff" * 30 + "\x00\x00") == "TVFKT 100"
