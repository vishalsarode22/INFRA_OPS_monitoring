"""
Round 30 (as trimmed by round 31): the wall's five checks are ordered by what
can hurt a productive system, worst status first.
"""
from core.models import MetricResult, Status
from collectors.rfc_live import _checks_from_metrics


def _m(name, value, status=Status.NORMAL):
    return MetricResult(name=name, value=value, display_value=f"{value} count", status=status)


def test_worst_status_leads_then_risk_order():
    rows = _checks_from_metrics([
        _m("sap.sm12.lock_count", 64),
        _m("sap.sm50.long_running_wp", 0),
        _m("sap.sm58.stuck_entries", 3, Status.WARNING),
        _m("sap.st22.dumps", 6, Status.CRITICAL),
    ])
    assert [r["metric"] for r in rows] == [
        "sap.st22.dumps",            # CRITICAL first
        "sap.sm58.stuck_entries",    # then WARNING
        "sap.sm50.long_running_wp",  # then risk order among the normal ones
        "sap.sm12.lock_count"]


def test_busy_counters_are_flagged_informational():
    rows = _checks_from_metrics([_m("sap.st22.dumps", 0), _m("sap.al08.user_logons", 139)])
    assert [r["metric"] for r in rows] == ["sap.st22.dumps"], "user sessions stay off the wall"
    assert rows[0]["risk"] is True
