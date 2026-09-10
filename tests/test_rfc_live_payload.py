"""
Regression tests for the live dashboard payload. Fakes only -- no pyrfc.

Covers: every RFC metric is serialised with its full record; perf metrics
join the checklist and the full record; the perf read is cached on its own
clock and rides the same session (no second logon); a failing perf read
degrades to an error field rather than blanking the live view.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import rfc_live as rl
from core.models import MetricResult, Status


def _m(name, value, status=Status.NORMAL, tcode=None, extra=None, source="rfc_collector",
       warn=None, crit=None, unit="", detail=""):
    return MetricResult(
        name=name, value=value, display_value=f"{value}", status=status,
        threshold_warning=warn, threshold_critical=crit, source=source, tcode=tcode,
        detail=detail, unit=unit, extra_data=extra or {},
    )


class FakeSession:
    ok = True
    error = None

    def __init__(self, fms=None):
        self.fms = fms or {}
        self.calls = []

    def call(self, name, **kw):
        self.calls.append(name)
        return self.fms.get(name)


def setup_function(_):
    rl.reset_perf_cache()


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def test_serialize_keeps_every_field_and_extra_data():
    m = _m("sap.st03.dialog_resp_ms", 307.0, Status.NORMAL, "ST03",
           extra={"collector": "RFC_PERF", "per_instance": {"prd_00": 307},
                  "top_users_by_total_ms": [{"user": "BASIS2", "total_ms": 4}]},
           source="rfc_perf", warn=1000, crit=2000, unit="ms", detail="prd_00 307ms")
    [row] = rl._serialize_metrics([m], "12:00:00", 3.2)

    assert row["metric"] == "sap.st03.dialog_resp_ms"
    assert row["label"] == "Dialog response"          # human label from _CHECK_LABELS
    assert row["tcode"] == "ST03"
    assert row["value"] == 307.0 and row["display_value"] == "307.0"
    assert row["status"] == "NORMAL"
    assert row["threshold_warning"] == 1000 and row["threshold_critical"] == 2000
    assert row["unit"] == "ms" and row["detail"] == "prd_00 307ms"
    assert row["collector"] == "RFC_PERF"
    # collector tag is lifted out; the rest of extra_data goes through intact
    assert "collector" not in row["extra"]
    assert row["extra"]["per_instance"] == {"prd_00": 307}
    assert row["extra"]["top_users_by_total_ms"][0]["user"] == "BASIS2"
    assert row["read_at"] == "12:00:00" and row["age_seconds"] == 3.2


def test_serialize_unknown_metric_falls_back_to_name_and_tcode():
    [row] = rl._serialize_metrics([_m("sap.zz.custom", 1, tcode="ZZ")], "now")
    assert row["label"] == "sap.zz.custom" and row["tcode"] == "ZZ"


def test_serialize_makes_extra_data_json_safe():
    import json
    from datetime import datetime
    m = _m("x", 1, extra={"when": datetime(2026, 9, 8, 10, 0, 0), "ids": {1, 2}})
    [row] = rl._serialize_metrics([m], "now")
    json.dumps(row)  # must not raise
    assert row["extra"]["when"] == "2026-09-08 10:00:00"
    assert sorted(row["extra"]["ids"]) == [1, 2]


# ---------------------------------------------------------------------------
# Perf read: cache and failure modes
# ---------------------------------------------------------------------------

def test_perf_read_uses_same_session_and_is_cached(monkeypatch):
    calls = []

    def fake_build(system, session, client):
        calls.append((system, id(session), client))
        return [_m("sap.sm66.max_instance_saturation_pct", 7.0, tcode="SM66", source="rfc_perf")]

    monkeypatch.setattr(rl, "_perf_build_metrics", fake_build)
    sess = FakeSession()

    metrics, err, age, _ = rl._perf_metrics(sess, "PRD", "100")
    assert err is None and len(metrics) == 1 and age == 0.0
    assert calls == [("PRD", id(sess), "100")]        # rode the caller's session

    metrics2, err2, age2, _ = rl._perf_metrics(sess, "PRD", "100")
    assert len(calls) == 1                             # second call served from cache
    assert metrics2 is metrics and err2 is None and age2 >= 0


def test_perf_cache_expires_on_its_own_clock(monkeypatch):
    n = {"calls": 0}
    monkeypatch.setattr(rl, "_perf_build_metrics",
                        lambda *a: n.__setitem__("calls", n["calls"] + 1) or [_m("a", 1)])
    monkeypatch.setattr(rl, "_PERF_TTL_SECONDS", 0.01)
    rl._perf_metrics(FakeSession(), "PRD", "100")
    time.sleep(0.02)
    rl._perf_metrics(FakeSession(), "PRD", "100")
    assert n["calls"] == 2


def test_perf_read_failure_is_an_error_not_an_exception(monkeypatch):
    def boom(*a):
        raise RuntimeError("STAT table gone")
    monkeypatch.setattr(rl, "_perf_build_metrics", boom)
    metrics, err, _, _ = rl._perf_metrics(FakeSession(), "PRD", "100")
    assert metrics == [] and "STAT table gone" in err


def test_perf_empty_result_is_labelled(monkeypatch):
    monkeypatch.setattr(rl, "_perf_build_metrics", lambda *a: [])
    metrics, err, _, _ = rl._perf_metrics(FakeSession(), "PRD", "100")
    assert metrics == [] and "no performance source" in err


# ---------------------------------------------------------------------------
# End-to-end payload shape through read_live
# ---------------------------------------------------------------------------

def _wire_read_live(monkeypatch, checks, perf):
    """Stub every RFC call read_live makes so only the merge logic runs."""
    class Session:
        ok, error = True, None
        def __init__(self, system, cfg): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def call(self, name, **kw): return None

    monkeypatch.setattr(rl, "SapSession", Session)
    monkeypatch.setattr(rl, "cooldown_remaining", lambda s: (0, None))
    monkeypatch.setattr(rl, "_from_function_module", lambda s: checks)
    monkeypatch.setattr(rl, "_from_standard_modules", lambda s: [])
    monkeypatch.setattr(rl, "_perf_build_metrics", lambda sy, se, c: perf)
    for name in ("_smon", "_logon_groups", "_jobs", "_locked_users"):
        monkeypatch.setattr(rl, name, lambda *a, **k: {})
    monkeypatch.setattr(rl, "_icm", lambda s: {})
    monkeypatch.setattr(rl, "_work_processes", lambda s: {"available": False})
    monkeypatch.setattr(rl, "_instances", lambda s: [])
    rl._cache.clear()


def test_read_live_exposes_full_record_and_merged_checklist(monkeypatch):
    checks = [
        _m("sap.st22.dumps", 2, Status.WARNING, "ST22", extra={"collector": "Z_FM"}),
        _m("cpu", 40, Status.NORMAL),                       # tile metric: excluded from checks
    ]
    perf = [
        _m("sap.sm12.oldest_lock_minutes", 291, Status.CRITICAL, "SM12", source="rfc_perf",
           extra={"collector": "RFC_PERF", "owner_clock_offset_min": 330}),
        _m("sap.st03.dialog_resp_ms", 307, Status.NORMAL, "ST03", source="rfc_perf"),
    ]
    _wire_read_live(monkeypatch, checks, perf)

    p = rl.read_live("PRD", {"rfc": {"client": "100"}}, use_cache=False)

    assert p["connected"] is True
    # full record: both collectors, tile metrics included
    names = [r["metric"] for r in p["rfc_metrics"]]
    assert names == ["sap.st22.dumps", "cpu", "sap.sm12.oldest_lock_minutes", "sap.st03.dialog_resp_ms"]
    assert {r["collector"] for r in p["rfc_metrics"]} == {"Z_FM", "RFC_PERF", "RFC_COLLECTOR"}
    # perf block on its own clock
    assert p["perf"]["error"] is None and p["perf"]["age_seconds"] == 0.0
    assert [r["metric"] for r in p["perf"]["metrics"]] == [
        "sap.sm12.oldest_lock_minutes", "sap.st03.dialog_resp_ms"]
    assert p["perf"]["metrics"][0]["extra"]["owner_clock_offset_min"] == 330
    # checklist: merged, sorted by severity, tiles excluded
    checklist = [(c["metric"], c["status"]) for c in p["checks"]]
    assert checklist == [
        ("sap.sm12.oldest_lock_minutes", "CRITICAL"),
        ("sap.st22.dumps", "WARNING"),
        ("sap.st03.dialog_resp_ms", "NORMAL"),
    ]
    assert p["checks"][0]["label"] == "Oldest lock age"


def test_read_live_perf_failure_keeps_checks(monkeypatch):
    checks = [_m("sap.sm21.errors", 6, Status.WARNING, "SM21")]
    _wire_read_live(monkeypatch, checks, [])

    def boom(*a):
        raise TimeoutError("STAT read timed out")
    monkeypatch.setattr(rl, "_perf_build_metrics", boom)

    p = rl.read_live("PRD", {"rfc": {"client": "100"}}, use_cache=False)
    assert p["connected"] is True
    assert [c["metric"] for c in p["checks"]] == ["sap.sm21.errors"]
    assert p["perf"]["metrics"] == [] and "timed out" in p["perf"]["error"]
    assert [r["metric"] for r in p["rfc_metrics"]] == ["sap.sm21.errors"]
