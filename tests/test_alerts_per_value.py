"""
Alerts per value (Thresholds page): off by default, critical only / warning
and critical, channels, repeat, phone call after 15 minutes, recovery, held
by acknowledge, history rows with receivers. Plain unittest.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SYSTEMS = ("PS4", "CEAT BW_PRD")
HYST_ENV = {"IBO_ALERT_RAISE_AFTER": "1", "IBO_ALERT_CLEAR_AFTER": "1",
            "IBO_ALERT_RECOVERY_MIN_MINUTES": "0", "IBO_ALERT_ESCALATE_WINDOW_MIN": "1"}


class _Env(unittest.TestCase):
    def setUp(self):
        from core import config_loader, thresholds as T, timeline, store, ops_db, alert_engine as E
        self.T, self.E, self.ops_db = T, E, ops_db
        self.tmp = tempfile.mkdtemp(prefix="ibo_alert_")
        with open(os.path.join(self.tmp, "thresholds.yaml"), "w", encoding="utf-8") as fh:
            fh.write("cpu:\n  warning: 80\n  critical: 90\n")
        self._saved = [(config_loader, "CONFIG_DIR", config_loader.CONFIG_DIR),
                       (T, "_known_system", T._known_system), (timeline, "add", timeline.add),
                       (store, "DB_PATH", store.DB_PATH), (E, "_now", E._now)]
        config_loader.CONFIG_DIR = self.tmp
        T._known_system = lambda n: n in SYSTEMS
        timeline.add = lambda *a, **k: None
        store.DB_PATH = os.path.join(self.tmp, "t.sqlite")
        if hasattr(store._local, "conn"):
            del store._local.conn
        store._initialised = False
        T._cache.clear()
        self.clock = [datetime(2026, 10, 9, 10, 0)]
        E._now = lambda: self.clock[0]
        self.sent = []
        self.cfg = {"name": "PS4"}
        E._live_seen.clear()
        # Written when every reading alerted at once; since Oct 2026 the owner
        # wants hysteresis (tests/test_fix_alerts_hysteresis.py). Switched off
        # here so these keep testing repeat / call / recovery / holds.
        self._env = {k: os.environ.get(k) for k in HYST_ENV}
        os.environ.update(HYST_ENV)

    def tearDown(self):
        from core import store
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if hasattr(store._local, "conn"):
            store._local.conn.close()
            del store._local.conn
        for obj, name, val in self._saved:
            setattr(obj, name, val)
        store._initialised = False
        self.T._cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def feed(self, status, value=95, key="cpu", **extra):
        it = {"key": key, "label": "CPU", "value": value, "status": status, "warning": 80,
              "critical": 90, "direction": "high", "unit": "%"}
        it.update(extra)
        return self.E.observe(self.cfg, [it], sender=lambda cfg, job: self.sent.append(job))

    def tick(self, minutes):
        self.clock[0] += timedelta(minutes=minutes)


class Rules(_Env):
    def test_off_by_default(self):
        self.assertEqual(self.E.rule_for("PS4", "cpu"), {"alert": "off", "channels": []})
        self.feed("CRITICAL")
        self.assertEqual(self.sent, [])

    def test_per_system_and_standard(self):
        from core import config_editor as ce
        ce.save_threshold("cpu", None, None, alert="critical", channels=["email", "whatsapp"])
        self.T._cache.clear()
        self.assertEqual(self.E.rule_for("CEAT BW_PRD", "cpu")["channels"], ["email", "whatsapp"])
        self.T.save_override("PS4", "cpu", None, None, alert="off")
        self.assertEqual(self.E.rule_for("PS4", "cpu")["alert"], "off")
        # alert-only save never touched the limits
        self.assertEqual(self.T.limits("cpu", system=""), (80, 90))
        with open(os.path.join(self.tmp, "thresholds.yaml"), encoding="utf-8") as fh:
            self.assertIn("channels: [email, whatsapp]", fh.read())

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.T.save_override("PS4", "cpu", None, None, alert="sometimes")
        with self.assertRaises(ValueError):
            self.T.save_override("PS4", "cpu", None, None, channels=["pigeon"])


class Flow(_Env):
    def setUp(self):
        super().setUp()
        self.T.save_override("PS4", "cpu", None, None, alert="critical", channels=["email", "call"])

    def test_new_repeat_call_recovery(self):
        self.feed("CRITICAL")
        self.assertEqual([(j["kind"], j["channels"]) for j in self.sent], [("alert", ["email"])])
        self.tick(5); self.feed("CRITICAL")
        self.assertEqual(len(self.sent), 1)                         # nothing new within the hour
        self.tick(11); self.feed("CRITICAL")                        # 16 min critical -> call
        self.assertEqual(self.sent[-1]["kind"], "call")
        self.tick(5); self.feed("CRITICAL")
        self.assertEqual(len(self.sent), 2)                         # call only once
        self.tick(45); self.feed("CRITICAL")                        # 61 min after first mail
        self.assertEqual(self.sent[-1]["kind"], "repeat")
        self.feed("NORMAL", 40)
        self.assertEqual((self.sent[-1]["kind"], self.sent[-1]["channels"]), ("recovery", ["email"]))
        self.feed("NORMAL", 40)
        self.assertEqual(len(self.sent), 4)

    def test_warning_ignored_when_critical_only_and_unknown_or_stale_ignored(self):
        self.feed("WARNING", 85)
        self.feed("UNKNOWN", None)
        self.feed("CRITICAL", 99, stale=True)
        self.assertEqual(self.sent, [])

    def test_warning_and_critical_level_change(self):
        self.T.save_override("PS4", "cpu", None, None, alert="warning", channels=["email"])
        self.feed("WARNING", 85)
        self.feed("CRITICAL", 95)
        self.feed("CRITICAL", 96)
        self.assertEqual([(j["kind"], j["level"]) for j in self.sent],
                         [("alert", "WARNING"), ("alert", "CRITICAL")])

    def test_per_instance_separate(self):
        self.feed("CRITICAL", 2500, key="sap.smlg.response_time", instance="app1")
        self.T.save_override("PS4", "sap.smlg.response_time", None, None, alert="critical",
                             channels=["email"])
        self.feed("CRITICAL", 2500, key="sap.smlg.response_time", instance="app1")
        self.feed("CRITICAL", 2600, key="sap.smlg.response_time", instance="app2")
        self.assertEqual([j["instance"] for j in self.sent], ["app1", "app2"])

    def test_acknowledged_is_held_and_recorded(self):
        from core import alert_control
        orig = alert_control.gate
        alert_control.gate = lambda s, k, key=None, **kw: "acknowledged by alex"
        try:
            self.feed("CRITICAL")
        finally:
            alert_control.gate = orig
        self.assertEqual(self.sent, [])
        rows = self.E.history("PS4")
        self.assertEqual(rows[0]["note"], "held: acknowledged by alex")
        self.assertEqual(rows[0]["ok"], 0)


class ReviewFixes(_Env):
    def test_off_for_all_systems_can_be_saved_and_read(self):
        from core import config_editor as ce
        ce.save_threshold("cpu", None, None, alert="critical", channels=["email"])
        ce.save_threshold("cpu", None, None, alert="off")
        ce.save_threshold("sap.icm.state", None, None, severity="off")
        self.T._cache.clear()
        self.assertEqual(self.E.rule_for("PS4", "cpu")["alert"], "off")
        from core import grading
        self.assertEqual(grading.severity_for("sap.icm.state", "PS4"), "off")

    def test_call_counts_from_critical_not_from_warning(self):
        self.T.save_override("PS4", "cpu", None, None, alert="warning", channels=["email", "call"])
        self.feed("WARNING", 85)
        self.tick(30); self.feed("CRITICAL", 95)
        self.assertEqual([j["kind"] for j in self.sent], ["alert", "alert"])
        self.tick(14); self.feed("CRITICAL", 95)
        self.assertEqual(len(self.sent), 2)
        self.tick(2); self.feed("CRITICAL", 95)
        self.assertEqual(self.sent[-1]["kind"], "call")

    def test_live_owns_a_value_sweep_does_not_flap(self):
        self.T.save_override("PS4", "cpu", None, None, alert="critical", channels=["email"])
        self.feed("NORMAL", 40)                                      # live: fine
        self.E.observe(self.cfg, [{"key": "cpu", "label": "CPU", "value": 97, "status": "CRITICAL"}],
                       source="sweep", sender=lambda c, j: self.sent.append(j))
        self.assertEqual(self.sent, [])                              # sweep ignored for live values
        self.tick(30)
        self.E.observe(self.cfg, [{"key": "cpu", "label": "CPU", "value": 97, "status": "CRITICAL"}],
                       source="sweep", sender=lambda c, j: self.sent.append(j))
        self.assertEqual(len(self.sent), 1)                          # no live read for 30 min: sweep decides

    def test_no_recovery_after_held_alert_and_rule_off_forgets(self):
        from core import alert_control
        self.T.save_override("PS4", "cpu", None, None, alert="critical", channels=["email"])
        orig = alert_control.gate
        alert_control.gate = lambda s, k, key=None, **kw: "in maintenance until 12:00"
        try:
            self.feed("CRITICAL")
            self.tick(5); self.feed("CRITICAL")
        finally:
            alert_control.gate = orig
        held = [r for r in self.E.history("PS4") if r["note"].startswith("held")]
        self.assertEqual(len(held), 1)                               # held once, not every read
        self.feed("NORMAL", 40)
        self.assertEqual(self.sent, [])                              # nothing was delivered: no recovery
        self.feed("CRITICAL")
        self.T.save_override("PS4", "cpu", None, None, alert="off")
        self.feed("NORMAL", 40)
        self.assertEqual([j["kind"] for j in self.sent], ["alert"])  # switched off: no recovery

    def test_last_alert_only_for_visible_systems(self):
        self.E.record("PS4", "cpu", "", "CPU", "CRITICAL", "95", "", "alert", "email", "a@x", True, "", "b1")
        self.E.record("OTHER CUSTOMER", "cpu", "", "CPU", "CRITICAL", "99", "", "alert", "email", "z@y", True, "", "b2")
        got = self.E.last_sends(None, systems=["PS4"])
        self.assertEqual(got["cpu"]["system"], "PS4")
        self.assertEqual(self.E.last_sends(None, systems=[]), {})
        self.assertEqual([r["system"] for r in self.E.history(None, systems=["PS4"])], ["PS4"])


class ReviewFixes2(_Env):
    def sweep(self, status, value=97):
        return self.E.observe(self.cfg, [{"key": "cpu", "label": "CPU", "value": value, "status": status}],
                              source="sweep", sender=lambda c, j: self.sent.append(j))

    def test_sweep_in_its_own_process_sees_live_ownership(self):
        self.T.save_override("PS4", "cpu", None, None, alert="critical", channels=["email"])
        self.feed("NORMAL", 40)                       # dashboard process: live read
        self.E._live_seen.clear()                     # main.py process: empty memory
        self.sweep("CRITICAL")
        self.tick(3); self.feed("NORMAL", 40)
        self.E._live_seen.clear()
        self.tick(3); self.sweep("CRITICAL")
        self.assertEqual(self.sent, [])

    def test_recovery_not_held_by_acknowledge_but_held_by_maintenance(self):
        from core import alert_control
        self.T.save_override("PS4", "cpu", None, None, alert="critical", channels=["email"])
        self.feed("CRITICAL")
        orig = alert_control.gate
        alert_control.gate = lambda s, k, key=None, **kw: None if k == "recovered" else "acknowledged by alex"
        try:
            self.feed("NORMAL", 40)
        finally:
            alert_control.gate = orig
        self.assertEqual([j["kind"] for j in self.sent], ["alert", "recovery"])
        self.assertFalse(self.sent[-1].get("held"))

    def test_hovering_warning_critical_calls_once(self):
        self.T.save_override("PS4", "cpu", None, None, alert="warning", channels=["email", "call"])
        for _ in range(3):
            self.feed("CRITICAL", 95); self.tick(17); self.feed("CRITICAL", 95)
            self.tick(1); self.feed("WARNING", 85); self.tick(1)
        self.assertEqual([j["kind"] for j in self.sent].count("call"), 1)

    def test_old_database_gets_new_columns(self):
        import sqlite3
        from core import ops_db, store
        path = os.path.join(self.tmp, "old.sqlite")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE alert_state (system TEXT NOT NULL, skey TEXT NOT NULL, status TEXT NOT NULL, "
                  "since TEXT NOT NULL, last_sent_at TEXT, last_sent_level TEXT, escalated_at TEXT, "
                  "PRIMARY KEY (system, skey))")
        c.execute("CREATE TABLE alert_sends (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
                  "system TEXT NOT NULL, metric TEXT NOT NULL, instance TEXT NOT NULL DEFAULT '', "
                  "label TEXT NOT NULL DEFAULT '', level TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', "
                  "limits TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, channel TEXT NOT NULL, "
                  "recipients TEXT NOT NULL DEFAULT '', ok INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '')")
        c.commit(); c.close()
        if hasattr(store._local, "conn"):
            store._local.conn.close(); del store._local.conn
        store.DB_PATH = path; store._initialised = False
        self.T.save_override("PS4", "cpu", None, None, alert="critical", channels=["email"])
        self.feed("CRITICAL")
        self.assertEqual(len(self.sent), 1)
        self.E.record("PS4", "cpu", "", "CPU", "CRITICAL", "95", "", "alert", "email", "a@x", True, "", "b9")
        self.assertEqual(self.E.history("PS4")[0]["batch"], "b9")


class Sending(_Env):
    def test_email_recorded_with_receivers(self):
        from notifications import mailer
        from core import config_loader
        got = []
        saved = (mailer.send_html_now, mailer.email_enabled, config_loader.get_smtp_config,
                 mailer.with_action_footer)
        mailer.send_html_now = lambda subject, html, to, smtp, cc=None, text_fallback=None, **k: \
            got.append((subject, to, cc)) or True
        mailer.email_enabled = lambda: True
        mailer.with_action_footer = lambda html, *a, **k: html
        config_loader.get_smtp_config = lambda cfg, require_recipients=True: {
            "to_emails": ["kiran@x.com"], "cc_emails": ["alex@x.com"], "manager_emails": ["alex@x.com"],
            "host": "h", "port": 587, "username": "u", "password": "p", "from_email": "f@x.com"}
        # Updated Oct 2026: this test asserted that the FIRST alert went to the
        # report receivers (to_emails, which hold the client) with the manager
        # in Cc -- exactly what the owner does not want. A first breach now
        # goes to Basis only; the client is never mailed (see
        # tests/test_fix_alerts_gate.py for the manager / client rules).
        cfg = {"name": "PS4", "basis_emails": "kiran@x.com", "manager_receivers": "alex@x.com"}
        try:
            job = {"system": "PS4", "metric": "cpu", "instance": "", "label": "CPU used",
                   "level": "CRITICAL", "kind": "alert", "value": "95 %", "limits": "critical ≥ 90",
                   "channels": ["email", "whatsapp"]}
            self.E._send_all(cfg, job)
        finally:
            (mailer.send_html_now, mailer.email_enabled, config_loader.get_smtp_config,
             mailer.with_action_footer) = saved
        self.assertEqual(got[0][1], ["kiran@x.com"])
        self.assertEqual(got[0][2], [])                              # no manager on a first breach
        self.assertIn("CRITICAL PS4: CPU used = 95 %", got[0][0])
        last = self.E.last_sends("PS4")["cpu"]
        chans = {c["channel"]: c for c in last["channels"]}
        self.assertEqual(chans["email"]["ok"], 1)
        self.assertEqual(chans["email"]["recipients"], "kiran@x.com")
        self.assertEqual(chans["whatsapp"]["ok"], 0)                 # not set up yet: recorded, not hidden
        # no number / WhatsApp switched off: recorded with the reason, not hidden
        self.assertTrue(any(w in chans["whatsapp"]["note"] for w in ("no WhatsApp number", "switched off")))

    def test_phone_numbers_validated_and_saved(self):
        self.E.save_phone_numbers("PS4", {"whatsapp": "+919812345678, +91 98765 43210", "call": "", "sms": []})
        self.assertEqual(self.E.phone_numbers("PS4")["whatsapp"], ["+919812345678", "+919876543210"])
        with self.assertRaises(ValueError):
            self.E.save_phone_numbers("PS4", {"call": "kiran"})


if __name__ == "__main__":
    unittest.main()
