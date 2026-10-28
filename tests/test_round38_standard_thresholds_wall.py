"""
Round 38 (24.09.2026): company standard for ABAP dumps and cancelled jobs --
5 = WARNING, 10 = CRITICAL -- in every grader (live RFC, sweep engine, Excel
sheet), and the two counters shown as tiles in the wall's uptime row.
"""
import pytest

from core.models import MetricResult, Status
from collectors.rfc_collector import _grade
from evaluation.threshold_engine import evaluate_metric


@pytest.mark.parametrize("n,expected", [(0, Status.NORMAL), (4, Status.NORMAL),
                                        (5, Status.WARNING), (9, Status.WARNING),
                                        (10, Status.CRITICAL), (25, Status.CRITICAL)])
def test_live_rfc_grades(n, expected):
    assert _grade("sap.st22.dumps", n) is expected
    assert _grade("sap.sm37.cancelled_jobs", n) is expected


@pytest.mark.parametrize("n,expected", [(4, Status.NORMAL), (5, Status.WARNING),
                                        (9, Status.WARNING), (10, Status.CRITICAL)])
def test_sweep_engine_grades(n, expected):
    for name in ("sap.st22.dump_count", "sap.sm37.cancelled_jobs"):
        m = MetricResult(name=name, value=float(n), display_value=str(n), status=Status.UNKNOWN)
        out = evaluate_metric(m, {name: {"warning": 5, "critical": 10, "inclusive": True}})
        assert out.status is expected, (name, n)


def test_engine_without_inclusive_keeps_strictly_above():
    m = MetricResult(name="x", value=10.0, display_value="10", status=Status.UNKNOWN)
    assert evaluate_metric(m, {"x": {"warning": 5, "critical": 10}}).status is Status.WARNING


def test_wall_has_the_counter_tiles_and_highlight():
    wall = open("dashboard/static/wall.html", encoding="utf-8").read()
    assert 'counterTile(s.checks, "sap.st22.dumps"' in wall
    assert 'counterTile(s.checks, "sap.sm37.cancelled_jobs"' in wall
    assert ".sys.s-critical{border:2px solid var(--red)" in wall
    assert "whyLine(s, st)" in wall
