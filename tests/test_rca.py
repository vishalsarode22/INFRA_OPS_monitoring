"""
Performance RCA: trigger, pre-emption, pipeline stages, report.

Nothing here touches SAP GUI, Gemini or SMTP. The capture actions are
exercised only for registration and for their pure helpers (database
detection, number parsing), because their navigation needs a live SAP GUI
and control IDs that must be verified on the target system.
"""

import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.rca_trigger import (Decision, RcaTrigger, TriggerConfig, preempt_running_sweep,
                              response_ms_from_payload)


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def _trigger(**kw):
    cfg = TriggerConfig(threshold_ms=1500, sustain_polls=2, cooldown_minutes=30, **kw)
    clk = Clock()
    return RcaTrigger(cfg, clock=clk), clk


def _p(ms, connected=True):
    return {"connected": connected, "dialog_response_ms": ms}


def test_one_poll_over_threshold_is_a_blip_not_a_spike():
    t, _ = _trigger()
    d = t.observe("PRD", _p(4000))
    assert d.fire is False and d.consecutive == 1


def test_two_consecutive_polls_over_threshold_fire():
    t, _ = _trigger()
    t.observe("PRD", _p(2000))
    d = t.observe("PRD", _p(2100))
    assert d.fire is True
    assert "2 consecutive polls" in d.reason and "2100 ms" in d.reason


def test_a_dip_between_breaches_resets_the_streak():
    t, _ = _trigger()
    t.observe("PRD", _p(2000))
    t.observe("PRD", _p(300))
    d = t.observe("PRD", _p(2000))
    assert d.fire is False and d.consecutive == 1


def test_missing_reading_neither_breaks_nor_extends_the_streak():
    t, _ = _trigger()
    t.observe("PRD", _p(2000))
    d = t.observe("PRD", _p(None))
    assert d.fire is False and d.reason == "no reading" and d.consecutive == 1
    d = t.observe("PRD", _p(2000))
    assert d.fire is True


def test_cooldown_stops_the_same_spike_firing_twice():
    t, clk = _trigger()
    t.observe("PRD", _p(2000)); assert t.observe("PRD", _p(2000)).fire
    # spike continues
    t.observe("PRD", _p(2500))
    d = t.observe("PRD", _p(2500))
    assert d.fire is False and d.reason == "in cooldown" and d.cooldown_remaining_s > 0
    # The streak kept counting through the cooldown, so a spike that is STILL
    # going when the cooldown expires fires on the very next poll -- it has
    # already sustained. A spike that ended and came back has to re-sustain.
    clk.advance(30 * 60 + 1)
    assert t.observe("PRD", _p(2500)).fire is True
    assert t.observe("PRD", _p(2500)).fire is False      # 1/2 again after firing


def test_manual_run_shares_the_cooldown():
    t, _ = _trigger()
    t.mark_fired("PRD", "manual")
    t.observe("PRD", _p(2000))
    assert t.observe("PRD", _p(2000)).reason == "in cooldown"


def test_systems_are_independent():
    t, _ = _trigger()
    t.observe("PRD", _p(2000))
    assert t.observe("QAS", _p(2000)).fire is False   # QAS has its own streak


def test_disabled_never_fires():
    t, _ = _trigger(enabled=False)
    t.observe("PRD", _p(9000))
    assert t.observe("PRD", _p(9000)).fire is False


def test_response_reading_prefers_card_figure_then_worst_instance():
    assert response_ms_from_payload({"connected": True, "dialog_response_ms": 320}, "x") == 320
    p = {"connected": True, "perf_response_by_instance": {"a": 200, "b": 4100}}
    assert response_ms_from_payload(p, "sap.st03.dialog_resp_ms") == 4100
    p = {"connected": True, "rfc_metrics": [{"metric": "sap.st03.dialog_resp_ms", "value": 999}]}
    assert response_ms_from_payload(p, "sap.st03.dialog_resp_ms") == 999
    assert response_ms_from_payload({"connected": False, "dialog_response_ms": 5000}, "x") is None


def test_status_reports_streak_and_cooldown():
    t, _ = _trigger()
    t.observe("PRD", _p(2000))
    st = t.status("PRD")
    assert st["consecutive_over"] == 1 and st["last_value_ms"] == 2000 and st["cooldown_remaining_s"] == 0


def test_config_from_yaml_reads_trigger_block():
    tc = TriggerConfig.from_yaml({"enabled": True, "trigger": {"threshold_ms": 900, "sustain_polls": 3,
                                                                "cooldown_minutes": 5, "preempt_running_sweep": False}})
    assert (tc.threshold_ms, tc.sustain_polls, tc.cooldown_minutes, tc.preempt_running_sweep) == (900, 3, 5, False)
    assert TriggerConfig.from_yaml({}).sustain_polls == 2


# ---------------------------------------------------------------------------
# Pre-emption
# ---------------------------------------------------------------------------

def test_preempt_returns_immediately_when_nothing_is_running():
    ok, note = preempt_running_sweep({}, request_cancel=lambda: None, is_running=lambda: False)
    assert ok and "no sweep" in note


def test_preempt_requests_cancel_and_waits_for_the_sweep_to_yield():
    state = {"current_system": "PRD", "cancel_requested": False}
    ticks = {"n": 0}
    def is_running():
        ticks["n"] += 1
        return ticks["n"] < 4       # yields on the 4th check
    clk = Clock()
    ok, note = preempt_running_sweep(
        state, request_cancel=lambda: state.__setitem__("cancel_requested", True),
        is_running=is_running, wait_s=60, poll_s=1, clock=clk, sleep=lambda s: clk.advance(s))
    assert state["cancel_requested"] is True
    assert ok and "PRD" in note


def test_preempt_gives_up_rather_than_share_the_gui_session():
    clk = Clock()
    ok, note = preempt_running_sweep(
        {"current_system": "PRD"}, request_cancel=lambda: None, is_running=lambda: True,
        wait_s=10, poll_s=1, clock=clk, sleep=lambda s: clk.advance(s))
    assert ok is False and "still running" in note and "collide" in note


# ---------------------------------------------------------------------------
# Pipeline stages (no GUI, no model, no SMTP)
# ---------------------------------------------------------------------------

def _evidence():
    return [
        {"tcode": "SM50", "screen": "SM50_active_long_running", "path": "", "ocr": "", "captured_at": "2026-09-13T12:00:00", "evidence_id": "EV-1",
         "facts": {"priv_count": 1, "priv": [{"wp": "3", "user": "USER1", "program": "ZBIGREPORT", "elapsed": "912", "type": "DIA"}],
                   "long_running": [{"wp": "77", "type": "DIA", "user": "USER1", "program": "ZBIGREPORT", "elapsed": "912", "action": "Sequential read"}],
                   "program_user": [{"user": "USER1", "processes": 6, "programs": ["ZBIGREPORT"]}],
                   "focus": ["PRIV mode: 1 -- WP 3 USER1 ZBIGREPORT", "Long-running (>=60s on current step): WP 77 DIA USER1 ZBIGREPORT 912s", "Program / user: USER1 x6 (ZBIGREPORT)"]}},
        {"tcode": "ST03N", "screen": "ST03N_workload_time_breakdown", "path": "", "ocr": "", "captured_at": "", "evidence_id": "",
         "facts": {"instance": "host_PS4_00", "window": "19:52-20:07", "dialog_avg_resp_ms": 2600.0, "dialog_steps": 340.0,
                   "breakdown": {"db_pct": 73, "cpu_pct": 12, "wait_ms": 20.0, "roll_ms": 5.0, "lock_ms": 0.0,
                                 "verdicts": ["DB time 1900 ms is 73% of response -- bottleneck is SQL-side (indexes, locks, scans)"]},
                   "task_types": {"verdicts": ["task types: no abnormal split"]},
                   "top_users": [{"user": "USER1", "steps": "40", "total_resp_ms": "120.000", "avg_resp_ms": "3.000", "db_ms": "90.000"}],
                   "top_transactions": [{"tcode": "ZMM01", "steps": "12", "total_resp_ms": "80.000", "avg_resp_ms": "6.666", "db_ms": "70.000"}],
                   "focus": ["host_PS4_00 19:52-20:07: dialog avg 2600 ms over 340 steps (DB 73%, CPU 12%, wait 20 ms, roll 5 ms, enqueue 0 ms)",
                             "DB time 1900 ms is 73% of response -- bottleneck is SQL-side (indexes, locks, scans)"]}},
        {"tcode": "SM12", "screen": "SM12_all_clients_all_users", "path": "", "ocr": "", "captured_at": "", "evidence_id": "",
         "facts": {"lock_count": 353, "user_count": 4,
                   "contended_keys": [{"table": "MARA", "argument": "800MAT1", "users": ["USER1", "USER2", "USER3"]}],
                   "contended_key_count": 1, "top_users": [{"user": "IB_TAPAS", "locks": 138}],
                   "stale_locks": [{"user": "SAP_WFRT", "table": "FDC_TIMERDAEMON_LOCK", "argument": "500X", "age_min": 16851}], "stale_count": 1,
                   "rfc": {"lock_count": 251, "most_locks_detail": "IB_TAPAS 138, SAP_WFRT 12"},
                   "tables": [{"title": "Lock table size", "columns": ["Scope", "Locks"], "rows": [["All clients / all users", "353"], ["RFC live read (all clients)", "251"]]},
                              {"title": "Same user, many locks (GUI)", "columns": ["User", "Locks"], "rows": [["IB_TAPAS", "138"]]}],
                   "focus": ["Locks: 353 (all clients), 353 in client 500; RFC read 251", "Same user, many locks: IB_TAPAS 138"]}},
        {"tcode": "STAD", "screen": "STAD_records", "path": "", "ocr": "", "captured_at": "", "evidence_id": "",
         "facts": {"lookback_minutes": 30, "components": {"db_pct": 60, "cpu_pct": 20, "max_wait_ms": 120.0, "max_lock_ms": 0.0},
                   "top_by_response": [{"user": "USER1", "steps": 6, "total_resp_ms": 41000, "worst_ms": 9800, "max_memory": 0, "programs": ["ZMM01"]}],
                   "top_programs": [{"program": "ZMM01", "steps": 6, "total_resp_ms": 41000, "db_ms": 30000, "cpu_ms": 5000}],
                   "top_by_memory": [{"user": "USER9", "steps": 1, "total_resp_ms": 800, "worst_ms": 800, "max_memory": 2100000, "programs": ["ZBIG"]}],
                   "tables": [{"title": "Users by total response time (last 30 min)", "columns": ["User", "Programs", "Steps", "Total response (ms)", "Worst step (ms)", "DB (ms)", "CPU (ms)", "Peak memory"],
                               "rows": [["USER1", "ZMM01", "6", "41,000", "9,800", "30,000", "5,000", ""]]}],
                   "focus": ["6 steps in last 30 min: DB 60% / CPU 20% of response; max wait 120 ms; max enqueue 0 ms"]}},
    ]


def test_rules_based_analysis_names_priv_locks_and_users_with_evidence():
    from evaluation.rca_pipeline import rules_based_analysis
    a = rules_based_analysis(_evidence(), {"dialog_response_ms": 2600})
    types = {c["type"] for c in a["culprits"]}
    assert {"workprocess", "lock", "user", "report"} <= types
    assert all(c["evidence"] for c in a["culprits"])          # nothing without evidence
    names = " ".join(str(c["name"]) for c in a["culprits"])
    assert "IB_TAPAS" in names and "USER9" in names and "host_PS4_00" in names
    assert a["severity"] == "WARNING" and "2600 ms" in a["headline"]
    assert any("SM50" in x["tcode"] for x in a["actions_now"])
    assert any("SM12" in x["tcode"] for x in a["actions_now"])
    assert any("SQL-side" in x["step"] for x in a["actions_now"])      # the ST03N DB-share verdict became an action
    assert any("exhaustion" in x["step"] for x in a["actions_now"])    # STAD wait > 50 ms
    assert any("stale" in c["impact"] for c in a["culprits"])          # 16851-minute lock
    assert a["source"] == "rules" and a["confidence"] == "low"


def test_rules_summary_and_per_screen_come_from_focus_lines():
    from evaluation.rca_pipeline import rules_based_analysis
    a = rules_based_analysis(_evidence(), {"dialog_response_ms": 2600})
    assert "in PRIV mode" in a["summary"] and a["per_screen"]["SM12"].startswith("Locks: 353")
    assert a["per_screen"]["SM50"].startswith("SM50 shows")


def test_ms_humanised():
    from sap_gui.rca_actions import _ms_h
    assert _ms_h(725995) == "12 min 5 s" and _ms_h(6736) == "6.7 s" and _ms_h(402) == "402 ms"


def test_rows_page_through_the_grid():
    from sap_gui.rca_actions import _rows
    class G:
        RowCount, VisibleRowCount, FirstVisibleRow = 45, 20, 0
        def GetCellValue(self, r, c):
            # SAP only serves rendered rows: blank unless within the visible window
            return f"u{r}" if self.FirstVisibleRow <= r < self.FirstVisibleRow + self.VisibleRowCount else ""
    rows = _rows(G(), {"user": "GUNAME"})
    assert len(rows) == 45 and all(r["user"] == f"u{i}" for i, r in enumerate(rows))


def test_rules_based_analysis_on_clean_screens_is_normal():
    from evaluation.rca_pipeline import rules_based_analysis
    a = rules_based_analysis([{"tcode": "SM50", "screen": "x", "path": "", "ocr": "", "facts": {},
                               "captured_at": "", "evidence_id": ""}], {})
    assert a["severity"] == "NORMAL" and a["culprits"] == []


def test_prompt_demands_evidence_and_includes_every_screen():
    from evaluation.rca_pipeline import build_prompt, RCA_SCHEMA
    p = build_prompt("PRD", "dialog 2600 ms", _evidence(), {"dialog_response_ms": 2600})
    assert "Principal SAP Basis Engineer" in p
    for tcode in ("SM50", "ST03N", "SM12", "STAD"):
        assert f"=== {tcode} ::" in p
    assert "not_supported" in p and "ONLY a JSON object" in p
    assert "OCR text is imperfect" in p
    assert json.dumps(RCA_SCHEMA, indent=2) in p


def test_analysis_falls_back_to_rules_when_ai_is_off(monkeypatch):
    from evaluation import rca_pipeline as rp
    monkeypatch.setenv("IBO_ENABLE_AI", "0")
    a = rp.analyze("PRD", "t", _evidence(), {}, {"analysis": {}})
    assert a["source"] == "rules"


def test_analysis_falls_back_to_rules_when_the_model_fails(monkeypatch):
    from evaluation import rca_pipeline as rp
    monkeypatch.setenv("IBO_ENABLE_AI", "1")
    class Boom:
        name = "boom"
        def generate(self, prompt): raise RuntimeError("quota")
    import evaluation.ai_analyzer as aa
    monkeypatch.setattr(aa, "get_provider", lambda: Boom())
    a = rp.analyze("PRD", "t", _evidence(), {}, {"analysis": {"send_images": False}})
    assert a["source"] == "rules" and "RuntimeError" in a["confidence_reason"]


def test_analysis_uses_the_model_output_when_it_parses(monkeypatch):
    from evaluation import rca_pipeline as rp
    monkeypatch.setenv("IBO_ENABLE_AI", "1")
    class Fake:
        name = "fake"
        def generate(self, prompt):
            return '```json\n{"severity":"CRITICAL","headline":"USER1 running ZBIGREPORT","summary":"USER1 is hammering MARA.","per_screen":{"SM12":"Three users wait on MARA 800MAT1."},"culprits":[]}\n```'
    import evaluation.ai_analyzer as aa
    monkeypatch.setattr(aa, "get_provider", lambda: Fake())
    a = rp.analyze("PRD", "t", _evidence(), {}, {"analysis": {"send_images": False}})
    assert a["severity"] == "CRITICAL" and a["source"] == "fake" and a["summary"] == "USER1 is hammering MARA."
    # the prompt carries the consultant's thresholds and asks for the summary field
    from evaluation.rca_pipeline import build_prompt
    ptxt = build_prompt("PRD", "t", _evidence(), {})
    assert "DB time > 40%" in ptxt and '"summary"' in ptxt and '"per_screen"' in ptxt


def test_dispatch_is_silent_when_email_is_off(monkeypatch):
    from evaluation import rca_pipeline as rp
    monkeypatch.setenv("IBO_ENABLE_EMAIL", "0")
    assert rp.dispatch({"name": "PRD"}, {"deliver": {"email": True}}, "/nope.pdf", {}, "t") is False


def test_rca_tasks_and_db_hint_come_from_config():
    from evaluation.rca_pipeline import rca_tasks, db_hint_for
    cfg = {"tasks": [{"tcode": "SM50", "action": "rca_sm50", "what": "x"}],
           "database": {"default": "detect", "systems": {"PRD": "ase"}}}
    assert rca_tasks(cfg) == [{"tcode": "SM50", "action": "rca_sm50"}]
    assert db_hint_for(cfg, "PRD") == "ase" and db_hint_for(cfg, "QAS") == "detect"


def test_shipped_rca_yaml_is_valid_and_names_registered_actions():
    from evaluation.rca_pipeline import load_rca_config, rca_tasks
    from sap_gui import rca_actions
    from sap_gui.tcode_actions import ACTIONS
    cfg = load_rca_config()
    rca_actions.register(ACTIONS)
    missing = [t["action"] for t in rca_tasks(cfg) if t["action"] not in ACTIONS]
    assert missing == [], missing
    assert cfg["trigger"]["threshold_ms"] > 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def test_rca_pdf_builds_with_screens_and_without():
    from reporting.rca_report import build_rca_pdf
    from evaluation.rca_pipeline import rules_based_analysis
    from PIL import Image
    d = tempfile.mkdtemp()
    shot = os.path.join(d, "SM50_active.png")
    Image.new("RGB", (1280, 720), (240, 244, 248)).save(shot)
    ev = _evidence()
    ev[0]["path"] = shot
    a = rules_based_analysis(ev, {"dialog_response_ms": 2600})
    out = os.path.join(d, "rca.pdf")
    build_rca_pdf(out, system="PRD", client="800", started=datetime(2026, 9, 13, 12, 0),
                  trigger_reason="dialog 2600 ms for 2 polls", analysis=a, evidence=ev,
                  rfc={"dialog_response_ms": 2600})
    assert os.path.getsize(out) > 3000
    try:
        import pypdf
        text = "\n".join(pg.extract_text() for pg in pypdf.PdfReader(out).pages)
        for needle in ("Performance RCA", "Who / what is causing it", "ZBIGREPORT", "MARA",
                       "Screen evidence", "SM50_active_long_running", "2. Summary", "PRIV mode: 1"):
            assert needle in text, needle
        assert text.index("1. Screen evidence") < text.index("2. Summary") < text.index("3. Who / what is causing it")
        assert "Extracted metrics" not in text and "evidence_id" not in text and "OCR text" not in text
        # tables render above the screenshot: the SM12 table title precedes the SM12 screenshot caption
        assert "Users by total response time" in text and "SM12_all_clients_all_users" in text
        assert text.count("PRIV mode: 1") == 1      # one paragraph per T-code, not one per screenshot
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Actions: pure parts only
# ---------------------------------------------------------------------------

def test_database_detection_from_titles_and_tree():
    from sap_gui.rca_actions import detect_database
    assert detect_database("DBA Cockpit: Performance - SAP ASE") == "ase"
    assert detect_database("DBA Cockpit (Sybase ASE)") == "ase"
    assert detect_database("DBA Cockpit: SAP HANA") == "hana"
    assert detect_database("Oracle: Performance Overview") == "oracle"
    assert detect_database("Something else") == "generic"
    # PS4: the title was just "Overview"; the database name was in the tree root
    assert detect_database("Overview", "", ["SAP HANA database: Dat...", "Current Status", "Overview"]) == "hana"
    # "release" contains "ase" -- must not match on a substring inside a word
    assert detect_database("Release notes overview") == "generic"


def test_elapsed_parsing():
    from sap_gui.rca_actions import _secs
    assert _secs("01:30:43") == 5443
    assert _secs("00:00:51") == 51
    assert _secs("51") == 51
    assert _secs("") == 0.0


# ---- fake SAP GUI object model, enough to drive the discovery helpers ----

class _Obj:
    def __init__(self, Type, Id="", Text="", Tooltip="", SubType="", ScreenTop=0, ScreenLeft=0, children=()):
        self.Type, self.Id, self.Text, self.Tooltip, self.SubType = Type, Id, Text, Tooltip, SubType
        self.ScreenTop, self.ScreenLeft = ScreenTop, ScreenLeft
        self._kids = list(children)
        self.pressed = False
    @property
    def ContainerType(self): return bool(self._kids)
    @property
    def Children(self):
        kids = self._kids
        class C:
            Count = len(kids)
            def Item(self, i): return kids[i]
        return C()
    def press(self): self.pressed = True


class _Session:
    def __init__(self, root): self.root = root
    def findById(self, cid):
        for o in _walk_all(self.root):
            if o.Id == cid: return o
        raise Exception(f"not found {cid}")


def _walk_all(o):
    yield o
    for k in o._kids: yield from _walk_all(k)


def _stad_screen():
    usr = _Obj("GuiUserArea", "wnd[0]/usr", children=[
        _Obj("GuiLabel", "l1", Text="Date", ScreenTop=100, ScreenLeft=10),
        _Obj("GuiCTextField", "f_date", Text="13.09.2026", ScreenTop=100, ScreenLeft=80),
        _Obj("GuiLabel", "l2", Text="Time", ScreenTop=120, ScreenLeft=10),
        _Obj("GuiTextField", "f_time", Text="19:26:00", ScreenTop=121, ScreenLeft=80),
        _Obj("GuiLabel", "l3", Text="Length", ScreenTop=120, ScreenLeft=200),
        _Obj("GuiTextField", "f_len", Text="00:10:00", ScreenTop=120, ScreenLeft=260),
        _Obj("GuiLabel", "l4", Text="Resp. time", ScreenTop=200, ScreenLeft=200),
        _Obj("GuiTextField", "f_resp", Text="", ScreenTop=200, ScreenLeft=300),
        _Obj("GuiButton", "b1", Text="Server selection", ScreenTop=300),
        _Obj("GuiShell", "grid1", SubType="GridView"),
    ])
    return _Session(_Obj("GuiMainWindow", "wnd[0]", children=[usr]))


def test_field_by_label_uses_the_same_row_to_the_right():
    from sap_gui.rca_actions import _field_by_label
    s = _stad_screen()
    assert _field_by_label(s, "time").Id == "f_time"        # not f_date, not f_len (different rows)
    assert _field_by_label(s, "length").Id == "f_len"       # same row as Time, but right of its own label
    assert _field_by_label(s, "resp. time").Id == "f_resp"
    assert _field_by_label(s, "nothing here") is None


class _Grid(_Obj):
    def __init__(self, Id, titles, rows):
        super().__init__("GuiShell", Id, SubType="GridView")
        self._titles, self._rows = titles, rows
        self.RowCount = len(rows)
        self.ColumnOrder = [f"C{i}" for i in range(len(titles))]
    def GetDisplayedColumnTitle(self, c): return self._titles[int(c[1:])]
    def GetCellValue(self, r, c): return self._rows[r][int(c[1:])]


def test_best_grid_skips_the_message_log_and_prefers_rows():
    from sap_gui.rca_actions import _grid, _read_grid_table
    log = _Grid("log", ["ICON", "MSGTIME", "DB Conn.", "Message"], [["", "20:49", "PS4", "Database connection established"]])
    empty = _Grid("empty", ["Client", "User Name", "Table Name"], [])
    data = _Grid("data", ["Lock Time", "Client", "User Name", "Table Name", "Lock Argument"],
                 [["20:46", "500", "SAP_WFRT", "DMC_JOB_LOCK_STRUCT", "/1LT/IUC_LOAD_MT_001_001"],
                  ["20:48", "500", "IB_TAPAS", "MARA", "5000000010001"]])
    usr = _Obj("GuiUserArea", "wnd[0]/usr", children=[empty, data, log])
    s = _Session(_Obj("GuiMainWindow", "wnd[0]", children=[usr]))
    assert _grid(s).Id == "data"
    t = _read_grid_table(_grid(s), prefer=("user", "table"))
    assert t["columns"][:2] == ["User Name", "Table Name"] and t["rows"][1][0] == "IB_TAPAS"


def test_shell_discovery_by_subtype_and_button_by_text():
    from sap_gui.rca_actions import _grid, _tree, _button_by_text
    s = _stad_screen()
    assert _grid(s).Id == "grid1"
    assert _tree(s) is None
    assert _button_by_text(s, "server").Id == "b1"
    assert _button_by_text(s, "continue") is None


def test_sap_number_formats_parse():
    from sap_gui.rca_actions import _num
    assert _num("1.234.567") == 1234567
    assert _num("1,234,567") == 1234567
    assert _num("1.234,56") == 1234.56
    assert _num("2.048,00 MB") == 2048.0
    assert _num("1.326,1") == 1326.1          # ST03N on PS4 -- this parsed to 0.0 before
    assert _num("1.044,2") == 1044.2
    assert _num("48.885") == 48885            # STAD response, German thousands
    assert _num("0,9") == 0.9
    assert _num("435,1") == 435.1
    assert _num("10.629") == 10629
    assert _num("") == 0.0 and _num(None) == 0.0


def test_breakdown_applies_the_consultants_thresholds():
    from sap_gui.rca_actions import _breakdown
    d = {"avg_resp_ms_n": 1044.2, "avg_db_ms_n": 367.0, "avg_cpu_ms_n": 430.4, "avg_wait_ms_n": 0.0,
         "avg_roll_in_ms_n": 0.0, "avg_roll_wait_ms_n": 201.3, "avg_lock_ms_n": 0.0, "avg_gui_ms_n": 249.7, "avg_rfc_ms_n": 0.0}
    b = _breakdown(d)
    assert b["db_pct"] == 35 and b["cpu_pct"] == 41
    v = " ".join(b["verdicts"])
    assert "CPU time" in v and "Roll" in v and "DB time" not in v      # 35% DB is under the 40% line
    b2 = _breakdown({"avg_resp_ms_n": 2000, "avg_db_ms_n": 1500, "avg_cpu_ms_n": 100, "avg_wait_ms_n": 80,
                     "avg_roll_in_ms_n": 0, "avg_roll_wait_ms_n": 0, "avg_lock_ms_n": 60, "avg_gui_ms_n": 0, "avg_rfc_ms_n": 0})
    v2 = " ".join(b2["verdicts"])
    assert "SQL-side" in v2 and "Wait time 80" in v2 and "Enqueue time 60" in v2


def test_classic_list_table_maps_cells_to_headers():
    from sap_gui.rca_actions import _list_table
    usr = _Obj("GuiUserArea", "wnd[0]/usr", children=[
        _Obj("GuiLabel", "h1", Text="Server", ScreenTop=10, ScreenLeft=0),
        _Obj("GuiLabel", "h2", Text="Program", ScreenTop=10, ScreenLeft=120),
        _Obj("GuiLabel", "h3", Text="User", ScreenTop=10, ScreenLeft=300),
        _Obj("GuiLabel", "h4", Text="Response time (ms)", ScreenTop=10, ScreenLeft=400),
        _Obj("GuiLabel", "h5", Text="DB req. time (ms)", ScreenTop=10, ScreenLeft=520),
        _Obj("GuiLabel", "r1a", Text="vhrrnps4ai01_PS4", ScreenTop=30, ScreenLeft=0),
        _Obj("GuiLabel", "r1b", Text="R_DMC_HC_RUN_CHECKS", ScreenTop=30, ScreenLeft=122),
        _Obj("GuiLabel", "r1c", Text="SAP_WFRT", ScreenTop=30, ScreenLeft=302),
        _Obj("GuiLabel", "r1d", Text="48.885", ScreenTop=30, ScreenLeft=430),
        _Obj("GuiLabel", "r1e", Text="10.116", ScreenTop=30, ScreenLeft=540),
        _Obj("GuiLabel", "r2a", Text="vhrrnps4ci_PS4_0", ScreenTop=50, ScreenLeft=0),
        _Obj("GuiLabel", "r2b", Text="RFC", ScreenTop=50, ScreenLeft=122),
        _Obj("GuiLabel", "r2c", Text="SLT_ADMIN", ScreenTop=50, ScreenLeft=302),
        _Obj("GuiLabel", "r2d", Text="7.421", ScreenTop=50, ScreenLeft=430),
        _Obj("GuiLabel", "r2e", Text="83", ScreenTop=50, ScreenLeft=540),
    ])
    rows = _list_table(_Session(_Obj("GuiMainWindow", "wnd[0]", children=[usr])), ["user", "response"])
    assert len(rows) == 2
    assert rows[0]["user"] == "SAP_WFRT" and rows[0]["response time (ms)"] == "48.885"
    assert rows[1]["program"] == "RFC" and rows[1]["db req. time (ms)"] == "83"


def test_rca_actions_register_without_clobbering():
    from sap_gui import rca_actions
    reg = {"sm66": object()}
    rca_actions.register(reg, stad_min_ms=5000, stad_lookback=10, db_hint="hana")
    assert set(reg) == {"sm66", "rca_sm50", "rca_sm66", "rca_st03n", "rca_sm12", "rca_stad", "rca_st04"}
    assert callable(reg["rca_stad"]) and callable(reg["rca_st04"])
