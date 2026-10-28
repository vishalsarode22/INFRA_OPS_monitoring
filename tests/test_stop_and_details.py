"""
13 Oct: the Stop button reaches into the sweep (no further T-code, no second
attempt, no sheet, no retry); custom alert details say who and when; a
scheduled run that did not happen is mailed to Basis. Plain unittest, no SAP.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_custom_alerts import _Env, raw, T0  # noqa: E402


class Details(_Env):
    def val(self, items, key, inst=None):
        return [i for i in items if i["key"] == key and (inst is None or i.get("instance") == inst)][0]

    def test_background_wps_name_users_and_times(self):
        wp = {"instances": [{"name": "app1", "bgd_total": 4, "bgd_busy": 3, "longest": {
            "seconds": 5400, "type": "BGD", "wp_no": "12", "report": "ZBIG", "user": "U1", "action": "Sequential read"},
            "busy": [{"wp_no": "12", "report": "ZBIG", "user": "U1", "seconds": 5400},
                     {"wp_no": "13", "report": "ZSMALL", "user": "U2", "seconds": 600},
                     {"wp_no": "14", "report": "ZBIG", "user": "U1", "seconds": 300}]}], "skipped": [], "error": ""}
        items = self.CA.values("PS4", raw(wp=wp), T0)
        d = self.val(items, "sap.sm66.bgd_saturation_pct")["detail"]
        self.assertIn("3 of 4 background work processes busy at 10:00 SAP time", d)
        self.assertIn("by user: U1 2, U2 1", d)
        self.assertIn("ZBIG (U1, app1 WP 12) since 08:30 (1 h 30 min ago)", d)
        lw = self.val(items, "sap.sm66.longest_wp_min")["detail"]
        self.assertIn("busy since 08:30 (1 h 30 min ago), Sequential read", lw)

    def test_cancelled_jobs_with_end_times(self):
        rows = [{"job": "ZJOB", "count": str(i), "user": "U1", "end": f"2026-10-12 09:{50 + i}:00"} for i in range(3)]
        items = self.CA.values("PS4", raw(cancelled={"rows": rows, "emails": {}, "error": ""}), T0)
        d = self.val(items, "sap.sm37.user_cancelled_15min", "U1")["detail"]
        self.assertIn("ZJOB at 09:50, ZJOB at 09:51, ZJOB at 09:52", d)

    def test_sm58_who_when_and_error(self):
        sm58 = {"count": 3, "dests": {"BWCLNT": 2, "CRM": 1}, "error": "", "rows": [
            {"dest": "BWCLNT", "state": "SYSFAIL", "user": "BATCH", "time": "081500", "function": "Z_SEND",
             "message": ""},
            {"dest": "BWCLNT", "state": "CPICERR", "user": "BATCH", "time": "093000", "function": "Z_SEND",
             "message": "Connection closed"},
            {"dest": "CRM", "state": "SYSFAIL", "user": "U9", "time": "095500", "function": "", "message": ""}]}
        d = self.val(self.CA.values("PS4", raw(sm58=sm58), T0), "sap.sm58.failed_today")["detail"]
        for part in ("by destination: BWCLNT 2, CRM 1", "SYSFAIL 2, CPICERR 1", "by user: BATCH 2, U9 1",
                     "function: Z_SEND 2", "first 08:15, latest 09:55 SAP time", "error: Connection closed"):
            self.assertIn(part, d)

    def test_sm58_old_release_without_extra_fields(self):
        sm58 = {"count": 2, "dests": {"BWCLNT": 2}, "error": ""}
        d = self.val(self.CA.values("PS4", raw(sm58=sm58), T0), "sap.sm58.failed_today")["detail"]
        self.assertEqual(d, "by destination: BWCLNT 2")

    def test_smq2_waiting_since_user_and_error(self):
        rows = [{"id": "Q1|1", "queue": "Q1", "state": "SYSFAIL", "user": "RFCUSER", "message": "No authorization"},
                {"id": "Q2|1", "queue": "Q2", "state": "READY", "user": "", "message": ""}]
        self.CA.values("PS4", raw(smq2={"rows": rows, "id_kind": "entry", "error": ""}), T0)
        items = self.CA.values("PS4", raw(smq2={"rows": rows, "id_kind": "entry", "error": ""}),
                               T0 + timedelta(minutes=25))
        e = self.val(items, "sap.smq2.error_10min")["detail"]
        self.assertIn("queues: Q1 (SYSFAIL) 1; waiting since 10:00 (25 min ago); by user: RFCUSER 1; "
                      "error: No authorization", e)
        w = self.val(items, "sap.smq2.waiting_10min")["detail"]
        self.assertIn("waiting since 10:00 (25 min ago)", w)

    def test_long_job_duration(self):
        run = {"rows": [{"job": "ZLONG", "user": "U1", "start": "2026-10-12 08:00:00", "minutes": 120.0},
                        {"job": "ZLONG", "user": "U1", "start": "2026-10-12 09:30:00", "minutes": 30.0}], "error": ""}
        d = self.val(self.CA.values("PS4", raw(running=run), T0), "sap.sm37.longest_active_job_min", "ZLONG")["detail"]
        self.assertIn("running since 2026-10-12 08:00 SAP time (2 h); 2 runs of it active", d)


class Reads(unittest.TestCase):
    class S:
        system = "X"; last_error = ""

        def __init__(self, fields, refuse_extra=False):
            self.fields, self.refuse, self.asked = fields, refuse_extra, []

        def call(self, fm, **kw):
            if fm == "DDIF_FIELDINFO_GET":
                return {"DFIES_TAB": [{"FIELDNAME": f} for f in self.fields.get(kw["TABNAME"], ())]}
            return None

        def read_table(self, table, fields, where="", rows=500):
            self.asked.append(list(fields))
            if self.refuse and len(fields) > 2:
                return None
            data = {"ARFCDEST": "BW", "ARFCSTATE": "SYSFAIL", "ARFCUSER": "batch", "ARFCUZEIT": "081500",
                    "ARFCFNAM": "Z_F", "ARFCMSG": "boom"}
            return [[data.get(f, "") for f in fields]]

    def setUp(self):
        from collectors import extra_checks as xc, custom_reads as cr
        xc._fields_cache.clear()
        cr._WIDE_REFUSED.clear()

    def test_refusal_is_remembered(self):
        from collectors import custom_reads as cr
        s = self.S({"ARFCSSTATE": ("ARFCDEST", "ARFCSTATE", "ARFCUSER")}, refuse_extra=True)
        cr.read_sm58(s, datetime(2026, 10, 12, 10))
        cr.read_sm58(s, datetime(2026, 10, 12, 10))
        self.assertEqual(len(s.asked), 3)          # wide, narrow, then narrow only

    def test_sm58_reads_extra_fields_when_present(self):
        from collectors import custom_reads as cr
        s = self.S({"ARFCSSTATE": ("ARFCDEST", "ARFCSTATE", "ARFCUSER", "ARFCUZEIT", "ARFCFNAM", "ARFCMSG")})
        out = cr.read_sm58(s, datetime(2026, 10, 12, 10))
        self.assertEqual(out["rows"][0]["user"], "BATCH")
        self.assertEqual(out["rows"][0]["message"], "boom")
        self.assertEqual(out["count"], 1)

    def test_sm58_falls_back_when_extra_refused(self):
        from collectors import custom_reads as cr
        s = self.S({"ARFCSSTATE": ("ARFCDEST", "ARFCSTATE", "ARFCUSER")}, refuse_extra=True)
        out = cr.read_sm58(s, datetime(2026, 10, 12, 10))
        self.assertEqual((out["count"], out["error"]), (1, ""))
        self.assertEqual(s.asked[-1], ["ARFCDEST", "ARFCSTATE"])


class StopInsideSweep(unittest.TestCase):
    def test_no_second_attempt_after_stop(self):
        try:
            import main
        except ImportError as exc:          # paramiko etc. exist on the VMs
            self.skipTest(f"main not importable here: {exc}")
        calls = []
        saved = (main._run_with_watchdog, main._email_enabled, main.STOP_CHECK, main.kill_sap_processes)

        def boom(cfg):
            calls.append(1)
            raise RuntimeError("hung")
        main._run_with_watchdog = boom
        main._email_enabled = lambda: False
        main.kill_sap_processes = lambda *a, **k: None
        main.STOP_CHECK = lambda: True
        try:
            self.assertFalse(main._run_pipeline_with_retries({"name": "CEAT S4P"}))
        finally:
            (main._run_with_watchdog, main._email_enabled, main.STOP_CHECK, main.kill_sap_processes) = saved
        self.assertEqual(len(calls), 1)

    def test_sweep_rca_obeys_the_dashboard_holds(self):
        try:
            import main
        except ImportError as exc:
            self.skipTest(f"main not importable here: {exc}")
        from core import rca_trigger
        from evaluation import rca_pipeline
        ran = []
        saved = (rca_pipeline.load_rca_config, rca_pipeline.rca_enabled, rca_pipeline.run_rca,
                 rca_trigger.sweep_decision, rca_trigger.mark_fired_on_disk, main.RCA_BLOCK)
        rca_pipeline.load_rca_config = lambda: {}
        rca_pipeline.rca_enabled = lambda cfg: True
        rca_pipeline.run_rca = lambda *a, **k: ran.append(k.get("should_stop"))
        rca_trigger.sweep_decision = lambda *a: (True, "SMLG 3000 ms")
        rca_trigger.mark_fired_on_disk = lambda *a: None
        metric = type("M", (), {"tcode": "SMLG",
                                "extra_data": {"instances": [{"response_time_ms": 3000, "instance": "a"}]}})()
        try:
            main.RCA_BLOCK = lambda s: "parked since 12.10 (logon refused)"
            main._rca_from_sweep("CEAT S4P", {"name": "CEAT S4P"}, [metric])
            self.assertEqual(ran, [])
            main.RCA_BLOCK = lambda s: None
            main._rca_from_sweep("CEAT S4P", {"name": "CEAT S4P"}, [metric])
            self.assertEqual(len(ran), 1)
            self.assertIs(ran[0], main._stop_requested)
        finally:
            (rca_pipeline.load_rca_config, rca_pipeline.rca_enabled, rca_pipeline.run_rca,
             rca_trigger.sweep_decision, rca_trigger.mark_fired_on_disk, main.RCA_BLOCK) = saved

    def test_evidence_stops_between_tcodes(self):
        from collectors import sap_gui_collector as g
        saved = g.get_scripting_session
        g.get_scripting_session = lambda: object()
        try:
            out = g.collect_tcode_evidence([{"tcode": "SM50"}, {"tcode": "ST22"}], system="X",
                                           should_stop=lambda: True)
        finally:
            g.get_scripting_session = saved
        self.assertEqual(out, [])


class NotRunNotice(unittest.TestCase):
    def setUp(self):
        import tempfile
        from core import store, schedules as sch
        self.tmp = tempfile.mkdtemp()
        self._saved = (store.DB_PATH, sch._PATH, sch.STATE_PATH)
        store.DB_PATH = os.path.join(self.tmp, "t.sqlite")
        if hasattr(store._local, "conn"):
            del store._local.conn
        store._initialised = False
        sch._PATH = os.path.join(self.tmp, "s.json")
        sch.STATE_PATH = os.path.join(self.tmp, "ss.json")

    def tearDown(self):
        from core import store, schedules as sch
        if hasattr(store._local, "conn"):
            store._local.conn.close()
            del store._local.conn
        store.DB_PATH, sch._PATH, sch.STATE_PATH = self._saved
        store._initialised = False

    def test_daily_slot_due_while_paused(self):
        from core import schedules as sch, schedule_notice as sn
        now = datetime(2026, 10, 13, 8, 5)
        sch.set_schedule("MDG", {"enabled": True, "mode": "daily", "at": "08:00"})
        sch.mark_run("MDG", now - timedelta(days=1))
        sch.set_schedule("FIORI", {"enabled": True, "mode": "daily", "at": "11:00"})
        sch.mark_run("FIORI", now - timedelta(days=1))
        self.assertEqual(sn.due_while_paused(["MDG", "FIORI"], now), [("MDG", datetime(2026, 10, 13, 8, 0))])
        sch.mark_run("MDG", now)
        self.assertEqual(sn.due_while_paused(["MDG", "FIORI"], now), [])

    def test_once_per_slot(self):
        from core import schedule_notice as sn, report_gate as rg, timeline
        sent = []
        saved = (rg._send_internal, timeline.add)
        rg._send_internal = lambda cfg, subject, html, kind, key, sev: sent.append(subject) or True
        timeline.add = lambda *a, **k: None
        try:
            slot = datetime(2026, 10, 13, 8, 0)
            self.assertTrue(sn.not_run({"name": "MDG"}, slot, "paused", "resume"))
            self.assertFalse(sn.not_run({"name": "MDG"}, slot, "paused", "resume"))
            self.assertTrue(sn.not_run({"name": "MDG"}, slot + timedelta(days=1), "paused", "resume"))
        finally:
            rg._send_internal, timeline.add = saved
        self.assertEqual(len(sent), 2)
        self.assertIn("scheduled monitoring of 08:00 did not run", sent[0])


if __name__ == "__main__":
    unittest.main()
