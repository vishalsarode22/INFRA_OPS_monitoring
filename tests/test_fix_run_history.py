"""
Run history (finding 14) and the manual-run hold (finding 11) of the
23 Oct 2026 review.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import run_history  # noqa: E402


@pytest.fixture
def rh(tmp_path, monkeypatch):
    monkeypatch.setattr(run_history, "DB_PATH", str(tmp_path / "rh.sqlite"))
    monkeypatch.setattr(run_history, "LEGACY_JSON", str(tmp_path / "run_history.json"))
    return run_history


def test_record_and_filter(rh):
    t0 = datetime(2026, 10, 23, 8, 0)
    rh.record(system="PRD", trigger="schedule", planned_at=t0, started_at=t0 + timedelta(seconds=20),
              ended_at=t0 + timedelta(minutes=7), result="success", screenshots_captured=19,
              attempt=1, attempts_max=2, profile_id="prd-full")
    rh.record(system="QAS", trigger="manual-system", started_at=t0, ended_at=t0, result="failed",
              failed_stage="monitoring-cycle", failure_reason="RFC timeout")
    rh.record_decision("PRD", "missed", planned_at=t0 + timedelta(hours=2), reason="VM was off")
    rows = rh.query()
    assert [r["result"] for r in rows] == ["missed", "failed", "success"]      # newest first
    ok = rows[-1]
    assert ok["duration_s"] == 400.0 and ok["unexpected"] is False and ok["screenshots_captured"] == 19
    assert ok["planned_at"].startswith("2026-10-23T08:00:00")                 # stored with offset
    assert ok["status"] == "ok" and ok["event"] == "Sweep completed"          # old feed fields
    assert [r["system"] for r in rh.query(system="QAS")] == ["QAS"]
    assert {r["result"] for r in rh.query(result="failed,missed")} == {"failed", "missed"}
    assert rh.query(since=datetime.now() + timedelta(days=1)) == []
    assert len(rh.query(limit=1)) == 1


def test_unexpected_flag():
    p = datetime(2026, 10, 23, 8, 0)
    assert run_history.is_unexpected("schedule", p, p + timedelta(minutes=5)) is False
    assert run_history.is_unexpected("schedule", p, p + timedelta(hours=3)) is True
    assert run_history.is_unexpected("schedule", None, p) is True             # no plan behind it
    assert run_history.is_unexpected("manual-system", None, p) is False
    assert run_history.is_unexpected("retry", None, p) is False
    assert run_history.is_unexpected("", None, p) is True                     # unknown origin


def test_record_run_uses_the_pipeline_outcome(rh):
    cfg = {"name": "PRD", "_run_ctx": {"run_id": "r1", "attempt": 2, "attempts_max": 2,
                                       "result": "hung-killed", "failed_stage": "PRD:tcode:SM50",
                                       "failure_reason": "hung -- no progress for 180s",
                                       "screenshots_captured": 3}}
    row = rh.record_run(cfg, trigger="schedule", started_at=datetime.now(), ok=False,
                        planned_at=datetime.now(), next_run_at="2026-10-23 10:00",
                        next_run_reason="schedule")
    assert row["result"] == "hung-killed" and row["attempt"] == 2
    got = rh.query()[0]
    assert got["failed_stage"] == "PRD:tcode:SM50" and got["next_run_reason"] == "schedule"
    # a stubbed pipeline (no ctx) still records a sensible row
    rh.record_run({"name": "QAS"}, trigger="manual-all", started_at=datetime.now(), ok=True)
    assert rh.query(system="QAS")[0]["result"] == "success"


def test_legacy_json_is_imported_once(rh):
    with open(rh.LEGACY_JSON, "w", encoding="utf-8") as fh:
        json.dump([{"system": "PRD", "event": "Sweep completed", "timestamp": "2026-10-20 08:05:00",
                    "status": "ok"},
                   {"type": "skipped", "requested": "QAS", "blocked_by": "PRD",
                    "at": "2026-10-20T08:06:00"}], fh)
    os.environ["IBO_RUN_HISTORY_DB"] = rh.DB_PATH           # allow the import under pytest
    try:
        rows = rh.query()
    finally:
        os.environ.pop("IBO_RUN_HISTORY_DB", None)
    assert sorted(r["result"] for r in rows) == ["skipped", "success"]
    rh._ready.discard(rh.DB_PATH)
    rh._local.conns = {}
    assert len(rh.query()) == 2                              # not imported twice


def test_csv_export_neutralises_formulas(rh):
    rh.record(system="=HYPERLINK(1)", trigger="cli", result="failed", failure_reason="-cmd")
    text = rh.to_csv(rh.query())
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][0] == "run_id"
    rec = dict(zip(rows[0], rows[1]))
    assert rec["system"].startswith("'=") and rec["failure_reason"] == "'-cmd"


# ---------------------------------------------------------------- API + page

@pytest.fixture
def client(rh, monkeypatch):
    from fastapi.testclient import TestClient
    import dashboard.app as app
    monkeypatch.setattr(app, "get_systems", lambda: [])
    return TestClient(app.app), app


def test_api_run_history_json_csv_next_and_page(client):
    c, app = client
    run_history.record(system="PRD", trigger="schedule", result="success",
                       started_at=datetime.now(), ended_at=datetime.now())
    run_history.record(system="QAS", trigger="manual-system", result="failed",
                       started_at=datetime.now(), ended_at=datetime.now())
    legacy = c.get("/api/run-history").json()               # the start page's feed
    assert isinstance(legacy, list) and {"event", "status", "timestamp"} <= set(legacy[0])
    only = c.get("/api/run-history", params={"system": "PRD", "result": "success"}).json()
    assert [r["system"] for r in only] == ["PRD"]
    r = c.get("/api/run-history.csv", params={"system": "QAS"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "QAS" in r.text and "PRD" not in r.text
    nxt = c.get("/api/run-history/next").json()
    assert "items" in nxt and "config_error" in nxt
    page = c.get("/run-history")
    assert page.status_code == 200 and "Run history" in page.text


# ---------------------------------------------------------------- finding 11

def test_manual_run_holds_scheduling_and_releases_it_by_itself(rh, monkeypatch):
    import dashboard.app as app
    monkeypatch.setattr(app, "get_systems", lambda: [{"name": "PRD"}])
    monkeypatch.setattr(app, "_run_single_system", lambda cfg: True)
    monkeypatch.setattr(app, "_skip_due_after_manual_run", lambda only=None: None)
    app._scheduler_state.update(enabled=True, manual_pause=False, snooze_until=None)
    app._run_state.update(running=False, stop_requested=False)
    app._begin_manual_hold()
    assert app._scheduler_state["manual_pause"] is True
    assert app._scheduler_state["enabled"] is True          # not switched off any more
    assert app._run_one_system_background("PRD") is True    # trigger manual-system
    assert app._scheduler_state["manual_pause"] is False
    assert app._scheduler_state["enabled"] is True


def test_pause_during_a_manual_run_sticks_after_it(rh, monkeypatch):
    import dashboard.app as app
    monkeypatch.setattr(app, "get_systems", lambda: [{"name": "PRD"}])
    monkeypatch.setattr(app, "_run_single_system", lambda cfg: app.scheduler_control("pause") and True)
    monkeypatch.setattr(app, "_skip_due_after_manual_run", lambda only=None: None)
    app._scheduler_state.update(enabled=True, manual_pause=False, snooze_until=None)
    app._run_state.update(running=False, stop_requested=False)
    app._begin_manual_hold()
    app._run_one_system_background("PRD")
    assert app._scheduler_state["enabled"] is False and app._scheduler_state["manual_pause"] is False
    app._scheduler_state["enabled"] = True


def test_scheduled_run_losing_the_lock_is_recorded_as_skipped(rh, monkeypatch):
    import dashboard.app as app
    monkeypatch.setattr(app, "next_runs", lambda now=None: {"systems": {}})
    monkeypatch.setattr(app, "_append_run_history", lambda ev: None)
    app._run_state.update(running=True, current_system="QAS")
    try:
        slot = datetime(2026, 10, 23, 8, 0)
        assert app._run_one_system_background("PRD", profile_id="prd-full", trigger="schedule",
                                              planned_at=slot) is False
    finally:
        app._run_state.update(running=False, current_system=None)
    row = run_history.query(result="skipped")[0]
    assert row["system"] == "PRD" and row["trigger"] == "schedule"
    assert row["profile_id"] == "prd-full" and "QAS" in row["failure_reason"]


def test_single_system_runner_records_one_history_row(rh, monkeypatch):
    import dashboard.app as app
    import main
    monkeypatch.setattr(app, "_append_run_history", lambda ev: None)
    monkeypatch.setattr(app, "next_runs", lambda now=None: {"systems": {}})

    def fake(cfg):
        cfg["_run_ctx"] = {"result": "success", "attempt": 1, "attempts_max": 2,
                           "screenshots_captured": 7}
        return True
    monkeypatch.setattr(main, "run_pipeline_for_system", fake)
    slot = datetime.now().replace(second=0, microsecond=0)
    assert app._run_single_system({"name": "PRD", "_run_meta": {"trigger": "schedule",
                                                               "planned_at": slot,
                                                               "profile_id": "p"}}) is True
    row = run_history.query()[0]
    assert (row["system"], row["trigger"], row["result"], row["screenshots_captured"]) == \
        ("PRD", "schedule", "success", 7)
    assert row["unexpected"] is False
