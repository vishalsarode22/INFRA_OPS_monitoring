"""
The monitoring sheet goes to the client only when the run was complete
(core/report_gate.py): SAP GUI logon, every screenshot, every sheet row.
Plain unittest, no SAP.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TCODES = ["AL08", "SM12", "ST22"]


class _Env(unittest.TestCase):
    def setUp(self):
        from core import config_loader, store, timeline, report_gate as G
        self.G = G
        # These tests cover the retry chain, which is off by default now
        # (IBO_REPORT_RETRY_MAX=0); switch it on as it was configured before.
        os.environ["IBO_REPORT_RETRY_MAX"] = "3"
        self.addCleanup(lambda: os.environ.pop("IBO_REPORT_RETRY_MAX", None))
        self.tmp = tempfile.mkdtemp(prefix="ibo_gate_")
        self._saved = [(config_loader, "get_monitoring_tasks", config_loader.get_monitoring_tasks),
                       (store, "DB_PATH", store.DB_PATH), (timeline, "add", timeline.add)]
        config_loader.get_monitoring_tasks = lambda: [{"tcode": t} for t in TCODES]
        self.timeline = []
        timeline.add = lambda *a, **k: self.timeline.append(a)
        store.DB_PATH = os.path.join(self.tmp, "t.sqlite")
        if hasattr(store._local, "conn"):
            del store._local.conn
        store._initialised = False
        self.cfg = {"name": "CEAT S4P", "has_gui_access": True}

    def tearDown(self):
        from core import store
        if hasattr(store._local, "conn"):
            store._local.conn.close()
            del store._local.conn
        for obj, name, val in self._saved:
            setattr(obj, name, val)
        store._initialised = False
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sheet(self, rows):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Monitoring Sheet"
        ws.append(["CEAT S4P | S4P — SAP BASIS Monitoring"])
        ws.append(["Date", "Time", "System", "SID", "Client", "Environment", "T-Code", "Check",
                   "Actual Result", "Status", "Observation"])
        for tcode, actual, status in rows:
            ws.append(["12.10.2026", "18:16", "CEAT S4P", "S4P", "900", "", tcode, "x", actual, status, ""])
        path = os.path.join(self.tmp, "sheet.xlsx")
        wb.save(path)
        return path

    def shots(self, tcodes, failed=()):
        out = []
        for t in tcodes:
            p = os.path.join(self.tmp, f"{t}.png")
            (lambda f: (f.write(b"png"), f.close()))(open(p, "wb"))
            out.append(SimpleNamespace(name=f"screenshot_{t}", screenshot_path=p, screenshot_paths=[p],
                                       display_value="failed" if t in failed else "captured (1)"))
        return out

    def good_sheet(self):
        return self.sheet([(t, "fine", "OK") for t in TCODES])


class Check(_Env):
    def test_the_12_october_case_is_held_and_not_retried(self):
        sheet = self.sheet([("AL08", "Users logged on: 0", "OK"),
                            ("SM12", "Not captured this run -- the SAP GUI logon ...", "UNKNOWN"),
                            ("ST22", "Not captured this run", "UNKNOWN")])
        g = self.G.check(self.cfg, False, "the SAP GUI logon to SAP Logon entry 'IBOPS CEAT S4P' did not "
                                          "succeed (check the user, password and client).", [], sheet)
        self.assertFalse(g["complete"])
        self.assertFalse(g["retry"])                     # wrong password: never hammer the user
        self.assertTrue(any("SAP GUI was not used" in r for r in g["reasons"]))
        self.assertEqual(g["missing"], sorted(TCODES))

    def test_shared_desktop_is_retried(self):
        g = self.G.check(self.cfg, False, "other SAP GUI sessions were open on the monitoring VM, so the "
                                          "monitor did not log on", [], self.good_sheet())
        self.assertFalse(g["complete"])
        self.assertTrue(g["retry"])

    def test_retry_only_when_it_can_help(self):
        sheet = self.good_sheet()
        for reason, retry in (("SAP GUI could not be started for SAP Logon entry 'X' (OSError)", True),
                              ("SAP GUI scripting gave no session after the logon.", True),
                              ("SAP GUI scripting is not available on this machine (no win32)", False)):
            self.assertEqual(self.G.check(self.cfg, False, reason, [], sheet)["retry"], retry, reason)

    def test_complete_run(self):
        g = self.G.check(self.cfg, True, "", self.shots(TCODES), self.good_sheet())
        self.assertEqual((g["complete"], g["reasons"]), (True, []))

    def test_missing_screenshot_file_or_failed_capture(self):
        shots = self.shots(TCODES, failed=("SM12",))
        os.remove(os.path.join(self.tmp, "ST22.png"))
        g = self.G.check(self.cfg, True, "", shots, self.good_sheet())
        self.assertFalse(g["complete"])
        self.assertEqual(g["missing"], ["SM12", "ST22"])
        self.assertTrue(g["retry"])

    def test_sheet_not_filled(self):
        sheet = self.sheet([("AL08", "fine", "OK"), ("SM12", "", ""), ("ST22", "x", "UNKNOWN")])
        g = self.G.check(self.cfg, True, "", self.shots(TCODES), sheet)
        self.assertEqual(g["missing"], ["SM12", "ST22"])
        g = self.G.check(self.cfg, True, "", self.shots(TCODES), os.path.join(self.tmp, "nope.xlsx"))
        self.assertIn("was not created", g["reasons"][0])

    def test_not_authorized_counts_and_allow_missing(self):
        sheet = self.sheet([("AL08", "x", "OK"), ("SM12", "x", "NOT AUTHORIZED"), ("ST22", "x", "UNKNOWN")])
        cfg = dict(self.cfg, report_gate={"allow_missing": ["st22"]})
        g = self.G.check(cfg, True, "", self.shots(["AL08", "SM12"]), sheet)
        self.assertTrue(g["complete"], g["reasons"])

    def test_system_without_gui_only_needs_the_sheet(self):
        cfg = dict(self.cfg, has_gui_access=False)
        self.assertTrue(self.G.check(cfg, False, "", [], self.good_sheet())["complete"])


class Retries(_Env):
    def run_chain(self, name, t, durations, complete_at=None):
        """Simulate: first run held at t; the scheduler starts retries; each run takes d minutes."""
        G = self.G
        G.mark_scheduler_alive(t)
        self.assertEqual(G.next_attempt(name, t), 1)
        G.plan_retry(name, 1, "x", t)
        runs = 1
        for d in durations:
            row = G._row(name)
            if not row:
                break
            t = max(t, datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M:%S"))
            if name not in G.due_retries(t):
                break
            G.mark_started(name, t)
            t += timedelta(minutes=d)
            attempt = G.next_attempt(name, t)
            runs += 1
            self.assertEqual(attempt, runs)
            if complete_at == runs:
                G.clear(name)
                break
            G.plan_retry(name, attempt, "x", t)
        return runs

    def test_one_run_plus_three_retries_even_when_runs_are_slow(self):
        for d in (5, 15, 25, 60):
            name = f"S{d}"
            self.assertEqual(self.run_chain(name, datetime(2026, 10, 12, 18, 0), [d] * 10), 4)
            self.assertIsNone(self.G._row(name))

    def test_complete_retry_ends_the_chain(self):
        self.assertEqual(self.run_chain("PS4", datetime(2026, 10, 12, 18, 0), [10] * 10, complete_at=2), 2)
        self.assertIsNone(self.G._row("PS4"))

    def test_profile_runs_continue_the_chain_manual_runs_restart_it(self):
        t = datetime(2026, 10, 12, 18, 0)
        self.G.plan_retry("PS4", 3, "x", t)
        self.assertEqual(self.G.next_attempt("PS4", t + timedelta(minutes=5)), 4)            # profile sweep
        self.assertEqual(self.G.next_attempt("PS4", t + timedelta(minutes=5), manual=True), 1)
        self.assertEqual(self.G.next_attempt("PS4", t + timedelta(hours=5)), 1)              # stale chain

    def test_old_database_gets_started_at(self):
        import sqlite3
        from core import store
        path = os.path.join(self.tmp, "old.sqlite")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE report_retries (system TEXT PRIMARY KEY, attempt INTEGER NOT NULL, "
                  "due_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', first_at TEXT NOT NULL)")
        c.commit(); c.close()
        if hasattr(store._local, "conn"):
            store._local.conn.close(); del store._local.conn
        store.DB_PATH = path; store._initialised = False
        self.G.plan_retry("PS4", 1, "x", datetime.now() - timedelta(minutes=20))
        self.G.mark_started("PS4")
        self.assertTrue(self.G._row("PS4")["started_at"])

    def test_scheduler_window(self):
        t = datetime(2026, 10, 12, 18, 0)
        self.G.mark_scheduler_alive(t)
        self.assertTrue(self.G.scheduler_alive(t + timedelta(minutes=120)))     # during a long sweep
        self.assertFalse(self.G.scheduler_alive(t + timedelta(minutes=181)))
        self.G.mark_scheduler_alive(t, paused=True)
        self.assertFalse(self.G.scheduler_alive(t))

    def test_retry_far_overdue_is_ended_not_run(self):
        told = []
        saved = self.G._tell_basis
        self.G._tell_basis = lambda cfg, gate, due, attempt, now: told.append(gate["reasons"][0])
        self.addCleanup(lambda: setattr(self.G, "_tell_basis", saved))
        t = datetime(2026, 10, 12, 9, 30)
        self.G.plan_retry("PS4", 1, "x", t)
        self.assertEqual(self.G.due_retries(t + timedelta(days=1)), [])
        self.assertIn("automatic monitoring was off", told[0])

    def test_crashed_retry_counts_and_last_one_tells_basis(self):
        told = []
        saved = self.G._tell_basis
        self.G._tell_basis = lambda cfg, gate, due, attempt, now: told.append((attempt, gate["reasons"][0]))
        self.addCleanup(lambda: setattr(self.G, "_tell_basis", saved))
        t = datetime(2026, 10, 12, 18, 0)
        self.G.plan_retry("PS4", 3, "x", t)
        self.G.mark_started("PS4", t + timedelta(minutes=15))            # try 4 starts and crashes
        self.assertEqual(self.G.due_retries(t + timedelta(minutes=40)), [])
        self.assertIsNone(self.G._row("PS4"))
        self.assertIn("did not finish", told[0][1])
        self.G.plan_retry("PS4", 1, "x", t)
        self.G.mark_started("PS4", t + timedelta(minutes=15))            # try 2 crashes: try 3 is due now
        self.assertEqual(self.G.due_retries(t + timedelta(minutes=40)), ["PS4"])
        self.assertEqual(self.G._row("PS4")["attempt"], 2)

    def test_postpone(self):
        self.G.plan_retry("PS4", 1, "x", datetime.now() - timedelta(minutes=20))
        self.assertEqual(self.G.due_retries(), ["PS4"])
        self.G.postpone("PS4", 15)
        self.assertEqual(self.G.due_retries(), [])


class Mails(_Env):
    def setUp(self):
        super().setUp()
        from core import recipients, config_loader
        from notifications import mailer
        self.mails = []
        saved = [(recipients, "basis", recipients.basis), (mailer, "send_html", mailer.send_html),
                 (config_loader, "get_smtp_config", config_loader.get_smtp_config)]
        recipients.basis = lambda cfg=None: ["basis@infrabeat.com"]
        mailer.send_html = lambda **k: self.mails.append(k) or True
        config_loader.get_smtp_config = lambda cfg=None, require_recipients=True: {}
        self.addCleanup(lambda: [setattr(o, n, v) for o, n, v in saved])

    def test_hold_tells_basis_only_and_plans_retry(self):
        self.G.mark_scheduler_alive(datetime(2026, 10, 12, 17, 59))
        g = {"complete": False, "reasons": ["screenshots not captured for 1 T-code(s): SM12"],
             "retry": True, "missing": ["SM12"]}
        out = self.G.hold(self.cfg, g, 1, datetime(2026, 10, 12, 18, 0))
        self.assertEqual(out["retry_at"], "2026-10-12 18:15:00")
        self.assertEqual(self.mails[0]["to"], ["basis@infrabeat.com"])
        self.assertIn("retry at 18:15", self.mails[0]["subject"])
        self.assertNotIn("attachments", self.mails[0])

    def test_hold_without_retry_on_logon_failure(self):
        g = {"complete": False, "reasons": ["SAP GUI was not used: logon refused"], "retry": False,
             "missing": TCODES}
        out = self.G.hold(self.cfg, g, 1)
        self.assertIsNone(out["retry_at"])
        self.assertIn("could lock the SAP user", self.mails[0]["html"])
        self.assertEqual(self.G.due_retries(datetime.now() + timedelta(hours=1)), [])

    def test_no_retry_promised_without_a_running_scheduler(self):
        g = {"complete": False, "reasons": ["screens missing"], "retry": True, "no_retry": "", "missing": []}
        out = self.G.hold(self.cfg, g, 1)
        self.assertIsNone(out["retry_at"])
        self.assertIn("scheduler is not running", self.mails[0]["html"])

    def test_no_gui_wording_and_internal_only(self):
        g = {"complete": False, "reasons": ["SAP GUI was not used"], "retry": False, "no_retry": "no_gui",
             "missing": []}
        self.G.hold(self.cfg, g, 1)
        self.assertIn("not available on this machine", self.mails[0]["html"])
        from core import recipients
        recipients.basis = lambda cfg=None: ["basis@infrabeat.com", "someone@client.example"]
        os.environ["IBO_INTERNAL_DOMAINS"] = "infrabeat.com"
        self.addCleanup(lambda: os.environ.pop("IBO_INTERNAL_DOMAINS", None))
        self.assertEqual(self.G.internal_smtp(self.cfg, {"to_emails": ["c@client.example"], "cc_emails": ["x"]}),
                         {"to_emails": ["basis@infrabeat.com"], "cc_emails": [], "manager_emails": []})

    def test_sent_with_acknowledge_button_once(self):
        from core import action_links
        os.environ["IBO_PUBLIC_URL"] = "http://ibops.infrabeat.com"
        os.environ["IBO_INTERNAL_DOMAINS"] = "infrabeat.com"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("IBO_PUBLIC_URL", "IBO_INTERNAL_DOMAINS")])
        self.G.plan_retry("CEAT S4P", 2, "x")
        self.G.sent(self.cfg, "2026-10-12 18:13:06", ["client@ceat.example"])
        self.assertEqual(self.G.due_retries(datetime.now() + timedelta(hours=1)), [])   # chain cleared
        html = self.mails[0]["html"]
        self.assertIn("Acknowledge: report checked", html)
        token = html.split("/access/action?t=")[1].split('"')[0]
        self.assertIn("checked by Alex", action_links.perform(token, "Alex"))
        with self.assertRaises(ValueError):
            action_links.perform(token, "Alex")
        self.assertTrue(any("Monitoring report checked" in str(a) for a in self.timeline))


if __name__ == "__main__":
    unittest.main()


class RetriesOnByDefault(_Env):
    """28.10.2026: the 12:00 PS4 sheet was held over one empty ST02 row and never tried again."""
    def test_default_is_two_retries(self):
        os.environ.pop("IBO_REPORT_RETRY_MAX", None)
        self.assertEqual(self.G.retry_max(), 2)
        self.assertEqual(self.G.partial_max(), 2)

    def test_a_few_missing_checks_after_the_last_try_are_sent(self):
        g = {"complete": False, "retry": True, "no_retry": "", "missing": ["ST02"],
             "details": {"ST02": "the screen did not load"}, "sheet_error": False, "gui_failed": False}
        self.assertTrue(self.G.partial_ok(g))
        self.assertIn("ST02", self.G.partial_note(g))
        self.assertIn("the screen did not load", self.G.partial_note(g))
        self.assertFalse(self.G.partial_ok(dict(g, missing=["ST02", "SM12", "ST22"])))   # too many
        self.assertFalse(self.G.partial_ok(dict(g, no_retry="logon")))                   # logon refused
        self.assertFalse(self.G.partial_ok(dict(g, gui_failed=True)))                    # no SAP GUI
        self.assertFalse(self.G.partial_ok(dict(g, sheet_error=True)))                   # no sheet
        os.environ["IBO_REPORT_PARTIAL_MAX"] = "0"
        try:
            self.assertFalse(self.G.partial_ok(g))
        finally:
            os.environ.pop("IBO_REPORT_PARTIAL_MAX", None)

    def test_check_flags_the_sheet_and_gui(self):
        g = self.G.check(self.cfg, False, "no session", [], "")
        self.assertTrue(g["gui_failed"])
        self.assertFalse(self.G.partial_ok(g))
