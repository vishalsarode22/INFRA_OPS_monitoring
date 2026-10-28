"""
Pipeline orchestration (findings 6, 7, 8, 12 of the 23 Oct 2026 review):
per-run heartbeat, abandoned threads, outbox deliveries, retries, DOWN
systems, profile delivery channels.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

main = pytest.importorskip("main")
from core import heartbeat, outbox  # noqa: E402


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(outbox, "OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setattr(main, "_WATCH_POLL_SECONDS", 0.05)
    monkeypatch.setattr(main, "PIPELINE_JOIN_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(main, "STOP_CHECK", None)
    main._ABANDONED.clear()
    yield
    main._ABANDONED.clear()


# ---------------------------------------------------------------- finding 6

def test_beats_reach_only_their_own_run():
    import contextvars
    a = heartbeat.start_run("A")
    b = heartbeat.start_run("B")
    a.at -= 100
    b.at -= 100

    def work():
        tok = heartbeat.bind(a)
        heartbeat.beat("a:stage")
        heartbeat.unbind(tok)
    t = threading.Thread(target=contextvars.copy_context().run, args=(work,))
    t.start(); t.join()
    assert a.seconds_since_beat() < 5 and a.stage == "a:stage"
    assert b.seconds_since_beat() >= 100            # B did not get A's beat


def test_a_cancelled_run_cannot_look_alive_again():
    r = heartbeat.Run("x")
    r.at -= 50
    r.cancel("hung")
    r.beat("late")
    assert r.seconds_since_beat() >= 50 and r.stage != "late"


def test_hung_thread_is_abandoned_cannot_kill_later_and_blocks_new_gui_runs(monkeypatch):
    release = threading.Event()
    kills = []
    later = {}
    monkeypatch.setattr(main, "kill_sap_processes", lambda *a, **k: kills.append(threading.current_thread().name))
    monkeypatch.setattr(main, "PIPELINE_STALL_SECONDS", 0.2)

    def hung_pipeline(cfg):
        heartbeat.beat("SYS:tcode-evidence")
        release.wait(10)                             # a COM call that never returns
        later["killed"] = main._kill_sap("SYS", "after the sweep")
        later["stop"] = main._stop_requested()
        return True
    monkeypatch.setattr(main, "_run_pipeline_for_system_once", hung_pipeline)

    cfg = {"name": "SYS", "has_gui_access": True}
    ok, err = main._run_with_watchdog(cfg)
    assert ok is False and "hung" in str(err)
    assert cfg["_run_ctx"]["result"] == "abandoned"
    assert "no progress" in cfg["_run_ctx"]["failure_reason"]
    assert len(kills) == 1                           # the watchdog's own kill
    zombie = main.abandoned_gui_alive()
    assert zombie and zombie["system"] == "SYS"

    # No new SAP GUI run while the abandoned thread lives.
    monkeypatch.setattr(main, "GUI_IMPORT_ERROR", None)
    started = []
    monkeypatch.setattr(main, "_run_pipeline_with_retries", lambda c: started.append(1) or True)
    other = {"name": "OTHER", "has_gui_access": True}
    assert main.run_pipeline_for_system(other) is False
    assert started == [] and other["_run_ctx"]["result"] == "skipped"
    assert "previous run still alive" in other["_run_ctx"]["failure_reason"]

    # When it finally wakes up it may not close anyone's SAP GUI.
    release.set()
    for _ in range(100):
        if "killed" in later:
            break
        time.sleep(0.02)
    assert later == {"killed": False, "stop": True}
    assert len(kills) == 1
    time.sleep(0.05)
    assert main.abandoned_gui_alive() is None


def test_hang_that_unwinds_after_the_kill_is_hung_killed(monkeypatch):
    unblock = threading.Event()
    monkeypatch.setattr(main, "kill_sap_processes", lambda *a, **k: unblock.set())
    monkeypatch.setattr(main, "PIPELINE_STALL_SECONDS", 0.2)

    def pipeline(cfg):
        unblock.wait(10)
        main._checkpoint("SYS", "after-com-call")      # raises: the run was cancelled
        raise AssertionError("must not get here")
    monkeypatch.setattr(main, "_run_pipeline_for_system_once", pipeline)
    cfg = {"name": "SYS"}
    ok, _err = main._run_with_watchdog(cfg)
    assert ok is False and cfg["_run_ctx"]["result"] == "hung-killed"
    assert main.abandoned_gui_alive() is None


# ---------------------------------------------------------------- finding 8

def _retry_setup(monkeypatch, behaviour):
    calls = []
    monkeypatch.setattr(main, "kill_sap_processes", lambda *a, **k: None)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    mails = []
    monkeypatch.setattr(main, "send_failure_alert", lambda *a, **k: mails.append(a))
    monkeypatch.setattr(main, "_email_enabled", lambda: True)

    def attempt(cfg):
        calls.append(1)
        return behaviour(cfg, len(calls))
    monkeypatch.setattr(main, "_run_with_watchdog", attempt)
    return calls, mails


def test_failure_before_collection_is_retried_without_mails(monkeypatch):
    def b(cfg, n):
        raise RuntimeError("SAP Logon crashed")
    calls, mails = _retry_setup(monkeypatch, b)
    cfg = {"name": "SYS"}
    assert main._run_pipeline_with_retries(cfg) is False
    assert len(calls) == 2 and mails == []
    assert cfg["_run_ctx"]["attempt"] == 2 and cfg["_run_ctx"]["result"] == "failed"


def test_failure_after_collection_is_not_retried(monkeypatch):
    def b(cfg, n):
        cfg["_run_ctx"]["collected"] = True
        raise RuntimeError("PDF builder failed")
    calls, mails = _retry_setup(monkeypatch, b)
    cfg = {"name": "SYS"}
    assert main._run_pipeline_with_retries(cfg) is False
    assert len(calls) == 1 and mails == []


def test_parked_down_system_is_not_retried(monkeypatch):
    def b(cfg, n):
        cfg["_run_ctx"].update(result="parked")
        return False, RuntimeError("down")
    calls, mails = _retry_setup(monkeypatch, b)
    assert main._run_pipeline_with_retries({"name": "SYS"}) is False
    assert len(calls) == 1 and mails == []


def test_nothing_collected_on_a_down_system_parks_it_without_mail(monkeypatch):
    from core import alert_control, reachability
    parked = []
    monkeypatch.setattr(reachability, "current_failures",
                        lambda: {"SYS": {"state": reachability.SYSTEM_DOWN}})
    monkeypatch.setattr(alert_control, "park", lambda s, **k: parked.append((s, k)))
    ctx = {}
    main._handle_nothing_collected({"name": "SYS"}, SimpleNamespace(errors=["rfc: timeout"]), ctx)
    assert parked and parked[0][0] == "SYS" and parked[0][1]["auto"] is True
    assert "SYSTEM DOWN" in parked[0][1]["reason"]
    assert ctx["result"] == "parked" and ctx["next_run_reason"] == "parked until cleared"


def test_nothing_collected_when_not_down_is_logged_not_parked(monkeypatch):
    from core import alert_control, reachability
    monkeypatch.setattr(reachability, "current_failures", lambda: {})
    monkeypatch.setattr(alert_control, "park", lambda *a, **k: pytest.fail("must not park"))
    ctx = {}
    main._handle_nothing_collected({"name": "SYS"}, SimpleNamespace(errors=[]), ctx)
    assert "no monitoring data" in ctx["failure_reason"] and "result" not in ctx


def test_incomplete_report_is_held_quietly(monkeypatch):
    from core import report_gate, timeline
    planned = []
    monkeypatch.setattr(report_gate, "scheduler_alive", lambda now=None: True)
    monkeypatch.setattr(report_gate, "next_attempt", lambda s, manual=False: 1)
    monkeypatch.setattr(report_gate, "manual_run", lambda: False)
    from datetime import datetime, timedelta
    monkeypatch.setattr(report_gate, "plan_retry",
                        lambda s, a, r, **k: planned.append((s, a)) or datetime.now() + timedelta(minutes=15))
    monkeypatch.setattr(report_gate, "_tell_basis", lambda *a, **k: pytest.fail("no REPORT HELD mail"))
    monkeypatch.setattr(timeline, "add", lambda *a, **k: None)
    ctx = {}
    main._hold_report_quietly({"name": "SYS"}, {"reasons": ["screens missing"], "retry": True}, ctx)
    assert planned == [("SYS", 1)]
    assert ctx["next_run_reason"].startswith("retry in ") and ctx["report"].startswith("held")


# ---------------------------------------------------------------- finding 7

def test_outbox_delivers_on_its_own_thread_and_times_out_without_raising(monkeypatch):
    monkeypatch.setattr(outbox, "timeout_seconds", lambda: 0.2)
    slow_started = threading.Event()
    done = []
    caller = threading.current_thread().name
    e1 = outbox.enqueue("final_report", "SYS", lambda: slow_started.set() or time.sleep(2), run_id="r1")
    e2 = outbox.enqueue("critical_alert", "SYS", lambda: done.append(threading.current_thread().name) or True)
    e3 = outbox.enqueue("tcode_failure", "SYS", lambda: 1 / 0)
    assert outbox.drain(timeout=5)
    entries = {e["id"]: e for e in outbox.recent()}
    assert entries[e1["id"]]["status"] == "timeout"
    assert entries[e2["id"]]["status"] == "sent" and done and done[0] != caller
    assert entries[e3["id"]]["status"] == "failed" and "ZeroDivisionError" in entries[e3["id"]]["error"]


def test_leftover_queued_entries_are_marked_lost(tmp_path, monkeypatch):
    import json
    d = tmp_path / "ob2"
    d.mkdir()
    (d / "x.json").write_text(json.dumps({"id": "x", "kind": "final_report", "system": "S",
                                          "status": "queued"}), encoding="utf-8")
    monkeypatch.setattr(outbox, "OUTBOX_DIR", str(d))
    outbox.enqueue("noop", "S", lambda: True)
    assert outbox.drain(timeout=5)
    data = json.loads((d / "x.json").read_text(encoding="utf-8"))
    assert data["status"] == "lost"


# ---------------------------------------------------------------- finding 12

def _result():
    return SimpleNamespace(system="PRD", overall_status=SimpleNamespace(value="NORMAL"),
                           cycle_timestamp="2026-10-23 08:00:00")


def _shot(tmp_path, tcode):
    p = tmp_path / f"{tcode}.png"
    p.write_bytes(b"png")
    return SimpleNamespace(name=f"screenshot_{tcode}", tcode=tcode, display_value="captured (1)",
                           screenshot_path=str(p), screenshot_paths=[str(p)])


def test_profile_delivery_channels_are_honoured(tmp_path, monkeypatch):
    from core import profiles
    prof = {"id": "prd-quick", "label": "Quick", "deliver": ["whatsapp"], "email_to": [], "email_cc": []}
    monkeypatch.setattr(profiles, "load_profiles", lambda: [prof])
    monkeypatch.setattr(main, "_email_enabled", lambda: True)
    cfg = {"name": "PRD", "_profile_id": "prd-quick"}
    out = main._report_deliveries(cfg, _result(), [_shot(tmp_path, "SM51")], "a.pdf", "b.xlsx", True, {})
    assert [k for k, _f, _w in out] == ["whatsapp"]          # no e-mail for a WhatsApp profile

    prof["deliver"] = []
    ctx = {}
    assert main._report_deliveries(cfg, _result(), [], "a.pdf", "b.xlsx", True, ctx) == []
    assert "no delivery channel" in ctx["report"]

    prof["deliver"] = ["email"]
    prof["email_to"] = ["ops@example.com"]
    sent = {}
    monkeypatch.setattr(main, "get_smtp_config", lambda c=None: {"to_emails": ["default@example.com"],
                                                                 "cc_emails": []})
    monkeypatch.setattr(main, "send_final_report",
                        lambda r, smtp, pdf, xls: sent.update(smtp=smtp) or True)
    out = main._report_deliveries(cfg, _result(), [], "a.pdf", "b.xlsx", False, {})
    assert [k for k, _f, _w in out] == ["final_report"]
    assert out[0][1]() is True and sent["smtp"]["to_emails"] == ["ops@example.com"]


def test_run_without_profile_mails_the_report_as_before(monkeypatch):
    monkeypatch.setattr(main, "_email_enabled", lambda: True)
    out = main._report_deliveries({"name": "PRD"}, _result(), [], "a.pdf", "b.xlsx", True, {})
    assert [k for k, _f, _w in out] == ["final_report"]


def test_report_only_when_screenshots_were_captured(tmp_path, monkeypatch):
    monkeypatch.delenv("IBO_REPORT_REQUIRE_SCREENSHOTS", raising=False)
    assert main._count_screenshots([_shot(tmp_path, "SM51"),
                                    SimpleNamespace(name="screenshot_ST22", display_value="failed",
                                                    screenshot_path=None, screenshot_paths=[])]) == 1
    assert main._report_allowed({"screenshots_captured": 1})
    assert not main._report_allowed({"screenshots_captured": 0})
    monkeypatch.setenv("IBO_REPORT_REQUIRE_SCREENSHOTS", "0")
    assert main._report_allowed({"screenshots_captured": 0})


def test_last_try_with_one_missing_check_is_sent_and_others_tell_basis(monkeypatch):
    """28.10.2026 PS4 12:00: one empty ST02 row -> nobody got the sheet, nobody was told."""
    from core import report_gate, timeline
    monkeypatch.setattr(report_gate, "scheduler_alive", lambda now=None: True)
    monkeypatch.setattr(report_gate, "next_attempt", lambda s, manual=False: 3)
    monkeypatch.setattr(report_gate, "manual_run", lambda: False)
    monkeypatch.setattr(report_gate, "plan_retry", lambda s, a, r, **k: None)          # tries used up
    monkeypatch.setattr(timeline, "add", lambda *a, **k: None)
    told = []
    monkeypatch.setattr(report_gate, "_tell_basis", lambda *a, **k: told.append(a))
    gate = {"complete": False, "reasons": ["sheet not filled for 1 check(s): ST02"], "retry": True,
            "no_retry": "", "missing": ["ST02"], "details": {"ST02": "screen did not load"},
            "sheet_error": False, "gui_failed": False}
    ctx = {}
    out = main._hold_report_quietly({"name": "PS4"}, gate, ctx)
    assert out["send_partial"] is True and told == []
    assert "ST02: screen did not load" in ctx["report"]
    out = main._hold_report_quietly({"name": "PS4"}, dict(gate, missing=["ST02", "SM12", "ST22"]), {})
    assert out["send_partial"] is False and len(told) == 1                       # one "not sent" mail


def test_report_mail_carries_the_note(monkeypatch, tmp_path):
    from notifications import email_report, mailer
    got = {}
    monkeypatch.setattr(email_report, "_build_report_html", lambda r: "<html><body><p>report</p></body></html>")
    monkeypatch.setattr(mailer, "send_message", lambda msg, smtp, rcpt, **k: got.update(msg=msg) or True)
    r = type("R", (), {"system": "PS4"})()
    assert email_report.send_final_report(r, {"from_email": "ibops@infrabeat.com", "to_emails": ["a@infrabeat.com"]},
                                          "", "", note_html="<div>ST02 missing</div>")
    body = got["msg"].get_payload()[0].get_payload()[0].get_payload(decode=True).decode()
    assert body.index("ST02 missing") < body.index("report")
