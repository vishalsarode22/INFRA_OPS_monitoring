"""
Attribution turns counters into named things. These tests pin the shapes
the RCA endpoint and the prompt rely on, using the real field names the
collectors emit.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.attribution import build_attribution, _team_for_program


def _row(metric, value, status="NORMAL", extra=None, detail=""):
    return {"metric": metric, "value": value, "status": status, "detail": detail, "extra": extra or {}}


def test_team_routing_custom_vs_standard():
    assert _team_for_program("ZESR_PROD_NEW").startswith("ABAP")
    assert _team_for_program("YREPORT").startswith("ABAP")
    assert _team_for_program("RSPFLDOC").startswith("Basis")
    assert _team_for_program("SAPLSTRD").startswith("Basis")
    assert _team_for_program("") == "unknown"


def test_dumps_by_user_program_team_and_gap_when_no_program():
    payload = {"dump_detail": {"count": 54,
        "by_user": [{"user": "BASIS2", "count": 40, "email": "b2@x.com"}, {"user": "HG009632", "count": 14, "email": None}],
        "by_host": [{"host": "sybase1", "count": 54}], "by_program": [], "recent": [],
        "note": "program names need the Z FM"}}
    a = build_attribution(payload, [])
    d = a["dumps"]
    assert d["total_today"] == 54
    assert d["by_user"][0]["user"] == "BASIS2" and d["by_user"][0]["email"] == "b2@x.com"
    assert d["by_program"] == [] and d["by_team"] == []
    assert any("Z FM" in g for g in a["gaps"])          # gap stated, not filled in

    payload["dump_detail"]["by_program"] = [{"program": "ZESR_PROD_NEW", "count": 40}, {"program": "SAPLSTRD", "count": 14}]
    payload["dump_detail"]["note"] = None
    a = build_attribution(payload, [])
    teams = {t["team"]: t["count"] for t in a["dumps"]["by_team"]}
    assert teams["ABAP (custom code)"] == 40 and teams["Basis / SAP standard"] == 14
    assert not any("Z FM" in g for g in a["gaps"])


def test_jobs_failed_why_and_owner():
    payload = {"job_detail": {
        "cancelled": [
            {"job": "DBA:CHECKDB", "user": "DDIC", "scheduled_by": "BASIS2", "owner": "BASIS2",
             "owner_email": "basis2@x.com", "never_started": True, "duration_text": "0s", "is_backup": True,
             "started_at": "02:00:00", "ended_at": "02:00:00"},
            {"job": "ZINV_LOAD", "user": "BGCLOUD", "scheduled_by": "HG009632", "owner": "HG009632",
             "owner_email": None, "never_started": False, "duration_text": "12m 4s", "is_backup": False,
             "started_at": "09:10:00", "ended_at": "09:22:04"},
        ],
        "long_running": [{"job": "ZESR_PROD_NEW", "user": "BGCLOUD", "owner": "BASIS2", "owner_email": None,
                          "started_at": "07:00:00", "duration_text": "117m 2s"}],
    }}
    a = build_attribution(payload, [])
    j = a["jobs"]
    assert j["backup_failed"] is True
    assert j["cancelled"][0]["job"] == "DBA:CHECKDB" and "never started" in j["cancelled"][0]["why"]
    assert j["cancelled"][0]["owner_email"] == "basis2@x.com"
    assert "ran 12m 4s then cancelled" in j["cancelled"][1]["why"] and "09:22:04" in j["cancelled"][1]["why"]
    assert j["long_running"][0]["job"] == "ZESR_PROD_NEW" and j["long_running"][0]["running_for"] == "117m 2s"
    assert "SM50/SM66" in j["long_running"][0]["why"]


def test_work_processes_why_blocked_vs_working_and_team():
    rows = [
        {"wp_no": "30", "instance": "srlprdap1_DB1_00", "user": "BGCLOUD", "client": "100",
         "report": "ZESR_PROD_NEW", "action": "Sequential read", "table": "VBAK", "reason": "", "elapsed_s": 7020, "status": "running"},
        {"wp_no": "12", "instance": "srlprdap1_DB1_00", "user": "HG009632", "client": "100",
         "report": "SAPLSTRD", "action": "", "table": "", "reason": "ENQ", "elapsed_s": 95, "status": "running"},
    ]
    priv = [{"wp_no": "4", "instance": "srlprdap1_DB1_00", "user": "U1", "report": "ZBIGREP",
             "action": "Direct read", "table": "", "reason": "", "elapsed_s": 300}]
    metrics = [_row("sap.sm50.long_running_wp", 2, "WARNING", {"long_running": rows}),
               _row("sap.sm50.priv_mode_wp", 1, "WARNING", {"priv": priv})]
    a = build_attribution({"rfc_metrics": metrics, "work_processes": {"in_use": 2, "total": 72}}, [])
    wp = a["work_processes"]
    assert wp["long_running"][0]["report"] == "ZESR_PROD_NEW"
    assert wp["long_running"][0]["team"].startswith("ABAP")
    assert wp["long_running"][0]["elapsed"] == "117m 0s"
    assert "working, but heavy" in wp["long_running"][0]["why"] and "VBAK" in wp["long_running"][0]["why"]
    assert "waiting on ENQ" in wp["long_running"][1]["why"] and "blocked" in wp["long_running"][1]["why"]
    assert wp["priv_mode"][0]["report"] == "ZBIGREP" and "PRIV mode" in wp["priv_mode"][0]["why"]
    assert wp["in_use"] == 2 and wp["total"] == 72


def test_response_drivers_and_low_sample_gap():
    extra = {"window": "last 5 min", "steps": 23, "low_sample": False,
             "per_instance": {"srlprdap1_DB1_00": 328562, "srlprdap2_DB1_02": 173685},
             "top_users_by_total_ms": [{"user": "BASIS2", "total_ms": 462778}],
             "top_reports_by_response": [{"report": "ZESR_PROD_NEW", "avg_ms": 300000}],
             "top_reports_by_db_time": [{"report": "RSPFLDOC", "db_ms": 110089}],
             "top_reports_by_memory": []}
    metrics = [_row("sap.st03.dialog_resp_ms", 211135, "CRITICAL", extra),
               _row("sap.st03.db_time_pct", 13.1)]
    a = build_attribution({"rfc_metrics": metrics}, [])
    r = a["response"]
    assert r["avg_ms"] == 211135 and r["status"] == "CRITICAL"
    assert r["top_users"][0]["user"] == "BASIS2"
    assert r["top_reports_by_response"][0]["team"].startswith("ABAP")
    assert r["top_reports_by_db_time"][0]["team"].startswith("Basis")
    assert r["db_share_pct"] == 13.1
    assert not any("too few" in g for g in a["gaps"])

    extra2 = dict(extra, steps=1, low_sample=True, top_users_by_total_ms=[], top_reports_by_response=[], top_reports_by_db_time=[])
    a = build_attribution({"rfc_metrics": [_row("sap.st03.dialog_resp_ms", 3712, "NORMAL", extra2)]}, [])
    assert any("too few to blame" in g for g in a["gaps"])


def test_locks_owner_and_clock_offset():
    metrics = [_row("sap.sm12.locks_per_user_max", 12, "WARNING",
                    {"top_users": [["BASIS2", 12], ["BGCLOUD", 4]], "same_object_dups": [["BASIS2", "RSTABLE"]]}),
               _row("sap.sm12.oldest_lock_minutes", 124, "CRITICAL",
                    {"owner_clock_offset_min": 330}, detail="BASIS2 RSTABLE ZNUMBER_RANGE 100")]
    a = build_attribution({"rfc_metrics": metrics}, [])
    l = a["locks"]
    assert l["top_users"][0][0] == "BASIS2" and l["oldest_minutes"] == 124
    assert "BASIS2 RSTABLE" in l["oldest_detail"] and l["clock_offset_min"] == 330


def test_empty_payload_is_safe_and_reports_gaps():
    a = build_attribution({}, [])
    assert a["dumps"]["total_today"] == 0 and a["jobs"]["cancelled"] == []
    assert any("TBTCO" in g for g in a["gaps"])
