"""
Round 31: the operations wall shows five checks and nothing else, and the
long-running work process names the user holding it.
"""
from core.models import MetricResult, Status
from collectors.rfc_live import _checks_from_metrics


def _m(name, value, status=Status.NORMAL, tcode="", detail=""):
    return MetricResult(name=name, value=value, display_value=f"{value} count",
                        status=status, tcode=tcode, detail=detail)


def test_only_the_five_requested_checks_reach_the_wall():
    rows = _checks_from_metrics([
        _m("sap.st22.dumps", 6, Status.CRITICAL),
        _m("sap.sm37.cancelled_jobs", 15, Status.WARNING),
        _m("sap.sm50.long_running_wp", 1),
        _m("sap.sm58.stuck_entries", 3, Status.WARNING),
        _m("sap.sm12.lock_count", 64),
        # everything below stays in the reports and alerts, off the wall
        _m("sap.al08.user_logons", 139),
        _m("sap.sm12.oldest_lock_minutes", 47),
        _m("sap.st03.db_time_pct", 19.7),
        _m("sap.sm58.sysfail_backlog", 2693, Status.WARNING),
        _m("sap.sm66.wp_saturation_pct", 12),
    ])
    assert [r["metric"] for r in rows] == [
        "sap.st22.dumps", "sap.sm37.cancelled_jobs", "sap.sm58.stuck_entries",
        "sap.sm50.long_running_wp", "sap.sm12.lock_count"]


def test_the_long_running_line_names_the_user_and_program():
    (row,) = _checks_from_metrics([
        _m("sap.sm50.long_running_wp", 1, detail=
           "vhrrnps4ci_PS4_00 64014 ZRP_SD_SALES_REGISER 1885s; vhrrnps4ai01_PS4_00 58327 CL_MD_BP 900s")])
    assert row["sub"] == "64014 ZRP_SD_SALES_REGISER 1885s"


def test_nothing_is_named_when_no_process_is_long_running():
    (row,) = _checks_from_metrics([_m("sap.sm50.long_running_wp", 0, detail="")])
    assert row["sub"] == ""


def test_the_wall_renders_the_sub_line_and_keeps_priv_mode_with_the_work_processes():
    wall = open("dashboard/static/wall.html", encoding="utf-8").read()
    assert 'c.sub ? " · " + IB.esc(c.sub) : ""' in wall
    assert 'PRIV mode (SM50)' in wall
    assert 'ABAP dumps today (ST22)' not in wall, "dumps live in the grid now, not twice"
