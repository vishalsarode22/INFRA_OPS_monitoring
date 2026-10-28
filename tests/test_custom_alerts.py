"""
Custom alerts (core/custom_alerts.py, core/alert_teams.py, core/job_user_mail.py):
background WP saturation, dumps per user routed to teams, cancelled jobs per
user with "approve: mail the user", SM12 a/b/c, SM58 today, SMQ2 waiting,
long background jobs, longest work process. Plain unittest, no SAP.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SYSTEMS = ("PS4", "CEAT S4P")
T0 = datetime(2026, 10, 12, 10, 0)


def raw(**parts):
    base = {"sap_now": T0.strftime("%Y-%m-%d %H:%M:%S"),
            "wp": {"instances": [], "error": "x"}, "dumps": {"rows": [], "error": ""},
            "cancelled": {"rows": [], "emails": {}, "error": ""}, "running": {"rows": [], "error": ""},
            "locks": {"rows": [], "error": ""}, "sm58": {"count": 0, "dests": {}, "error": ""},
            "smq2": {"rows": [], "id_kind": "entry", "error": ""}}
    base.update(parts)
    return base


HYST_ENV = {"IBO_ALERT_RAISE_AFTER": "1", "IBO_ALERT_CLEAR_AFTER": "1",
            "IBO_ALERT_RECOVERY_MIN_MINUTES": "0", "IBO_ALERT_ESCALATE_WINDOW_MIN": "1"}


class _Env(unittest.TestCase):
    def setUp(self):
        from core import config_loader, thresholds as T, timeline, store, alert_engine as E
        from core import custom_alerts as CA
        self.T, self.E, self.CA = T, E, CA
        self.tmp = tempfile.mkdtemp(prefix="ibo_custom_")
        with open(os.path.join(self.tmp, "thresholds.yaml"), "w", encoding="utf-8") as fh:
            fh.write("cpu:\n  warning: 80\n  critical: 90\n")
        self._saved = [(config_loader, "CONFIG_DIR", config_loader.CONFIG_DIR),
                       (T, "_known_system", T._known_system), (timeline, "add", timeline.add),
                       (store, "DB_PATH", store.DB_PATH), (E, "_now", E._now),
                       (E, "recipients", E.recipients)]
        config_loader.CONFIG_DIR = self.tmp
        T._known_system = lambda n: n in SYSTEMS
        timeline.add = lambda *a, **k: None
        store.DB_PATH = os.path.join(self.tmp, "t.sqlite")
        if hasattr(store._local, "conn"):
            del store._local.conn
        store._initialised = False
        T._cache.clear()
        self.clock = [T0]
        E._now = lambda: self.clock[0]
        E.recipients = lambda cfg: {"email": {"to": ["basis-default@x.com"], "cc": []},
                                    "whatsapp": [], "sms": [], "call": []}
        E._live_seen.clear()
        self.sent = []
        self.cfg = {"name": "PS4"}
        # These tests cover routing, teams and recovery, written when every
        # reading alerted at once. Since Oct 2026 the owner wants hysteresis
        # (3 readings to raise / clear, no recovery mail under 30 min) --
        # tested in tests/test_fix_alerts_hysteresis.py -- so it is switched
        # off here to keep testing what these tests are about.
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

    def run_once(self, r, at=None):
        at = at or self.clock[0]
        self.clock[0] = at
        return self.CA.process(self.cfg, r, now=at, sender=lambda c, j: self.sent.append(j))

    def alert_on(self, key, level="warning"):
        self.T.save_override("PS4", key, None, None, alert=level, channels=["email"])
        self.T._cache.clear()

    def by_key(self, items, key, inst=None):
        return [i for i in items if i["key"] == key and (inst is None or i.get("instance") == inst)]


class Teams(_Env):
    def test_classify(self):
        from core import alert_teams as AT
        self.assertEqual(AT.classify_dump("TSV_TNEW_PAGE_ALLOC_FAILED"), "basis")
        self.assertEqual(AT.classify_dump("DBIF_RSQL_SQL_ERROR"), "basis")
        self.assertEqual(AT.classify_dump("CALL_FUNCTION_REMOTE_ERROR"), "basis")
        self.assertEqual(AT.classify_dump("CONVT_NO_NUMBER"), "program")
        self.assertEqual(AT.classify_dump("UNCAUGHT_EXCEPTION", "CX_SY_NO_MEMORY"), "basis")
        self.assertEqual(AT.teams_for_dump("PS4", "SYSTEM_NO_ROLL", "", "FI-GL"), ["Basis"])
        self.assertEqual(AT.teams_for_dump("PS4", "CONVT_NO_NUMBER", "", "FI-GL-GL"), ["ABAP", "FI", "Basis"])
        self.assertEqual(AT.teams_for_dump("PS4", "MESSAGE_TYPE_X", "", "SD"), ["ABAP", "SD", "Basis"])
        self.assertEqual(AT.teams_for_dump("PS4", "MESSAGE_TYPE_X", "", "FIN-XX"), ["ABAP"])   # not FI
        self.assertEqual(AT.teams_for_dump("PS4", "MESSAGE_TYPE_X", "", ""), ["ABAP"])

    def test_save_resolve_and_fallback(self):
        from core import alert_teams as AT
        AT.save(None, {"Basis": "basis@x.com", "ABAP": "abap@x.com", "FI": "", "SD": "", "MM": ""})
        AT.save("PS4", {"ABAP": "ps4-abap@x.com", "PP": "pp@x.com"})
        self.assertIn("PP", AT.team_names("PS4"))
        self.assertNotIn("PP", AT.team_names("CEAT S4P"))
        self.assertEqual(AT.resolve({"name": "PS4"}, ["ABAP"])[0], ["ps4-abap@x.com"])
        self.assertEqual(AT.resolve({"name": "CEAT S4P"}, ["ABAP"])[0], ["abap@x.com"])
        to, notes = AT.resolve({"name": "PS4"}, ["FI"])
        self.assertEqual(to, ["basis@x.com"])                     # empty team -> Basis
        self.assertTrue(notes and "FI" in notes[0])
        to, _ = AT.resolve({"name": "PS4"}, ["ABAP", "FI", "Basis"])
        self.assertEqual(to, ["ps4-abap@x.com", "basis@x.com"])   # no duplicates

    def test_basis_empty_uses_system_receivers_and_validation(self):
        from core import alert_teams as AT
        self.assertEqual(AT.resolve({"name": "PS4"}, ["SD"])[0], ["basis-default@x.com"])
        with self.assertRaises(ValueError):
            AT.save("PS4", {"ABAP": "not-an-address"})
        with self.assertRaises(ValueError):
            AT.save("PS4", {"bad name!": "a@x.com"})


class NotChecked(unittest.TestCase):
    def test_parked_system_says_why(self):
        from unittest import mock
        from core import custom_alerts as CA, config_loader, check_alerts, alert_control
        cfg = {"name": "CEAT S4P", "rfc": {"ashost": "127.0.0.1"}}
        with mock.patch.object(config_loader, "get_all_systems", lambda: [cfg]), \
                mock.patch.object(check_alerts, "_eligible", lambda c: False), \
                mock.patch.object(alert_control, "sweep_block_reason", lambda n: "parked since 14.10 18:41 (logon refused)"):
            CA.run_all()
        got = CA.readings("CEAT S4P")
        self.assertEqual(got["items"], [])
        self.assertIn("parked since 14.10 18:41", got["skipped"])


class Subjects(unittest.TestCase):
    def test_per_user_alert_does_not_look_like_the_daily_count(self):
        from core import alert_engine as E
        base = {"system": "CEAT S4P", "level": "CRITICAL", "kind": "alert", "value": "33", "limits": "", "instance": ""}
        daily, _h, _t = E.render_email(dict(base, metric="sap.st22.dumps", label="ABAP dumps today", value="1358"))
        user, _h, _t = E.render_email(dict(base, metric="sap.st22.user_dumps_10min", instance="EY_SD",
                                           label="ABAP dumps by one user (15 min)"))
        self.assertEqual(daily, "[InfraBeatOps] CRITICAL CEAT S4P: ABAP dumps today = 1358")
        self.assertEqual(user, "[InfraBeatOps] CRITICAL CEAT S4P: ABAP dumps by user EY_SD (15 min) = 33")


class Values(_Env):
    def test_ten_dumps_do_not_alert(self):
        d = lambda t: {"date": "20261012", "time": t, "user": "U9", "error": "X", "exception": "",  # noqa: E731
                       "component": "", "program": "ZP", "host": "h"}
        self.CA.grade("PS4", self.CA.values("PS4", raw(dumps={"rows": [], "error": ""}), T0))
        items = self.CA.grade("PS4", self.CA.values(
            "PS4", raw(dumps={"rows": [d(f"10{i:02d}00") for i in range(10)], "error": ""}), T0 + timedelta(minutes=5)))
        u = self.by_key(items, "sap.st22.user_dumps_10min", "U9")[0]
        self.assertEqual((u["value"], u["status"]), (10, "NORMAL"))               # 10 is not "more than 10"

    def test_bgd_and_longest_wp(self):
        r = raw(wp={"error": "", "instances": [
            {"name": "app1", "bgd_total": 10, "bgd_busy": 7,
             "longest": {"seconds": 3900, "type": "BGD", "wp_no": "12", "report": "ZREP", "user": "U1", "action": ""}},
            {"name": "app2", "bgd_total": 10, "bgd_busy": 8, "longest": None}]})
        items = self.CA.grade("PS4", self.CA.values("PS4", r, T0))
        bgd = self.by_key(items, "sap.sm66.bgd_saturation_pct")[0]
        self.assertEqual((bgd["value"], bgd["status"]), (75, "WARNING"))          # 15/20, 70/80
        wp = self.by_key(items, "sap.sm66.longest_wp_min")[0]
        self.assertEqual((wp["value"], wp["status"]), (65.0, "WARNING"))          # 60/90
        self.assertIn("ZREP", wp["detail"])

    def test_dumps_baseline_then_window_and_teams(self):
        d = lambda t, user, err, comp="": {"date": "20261012", "time": t, "user": user, "error": err,  # noqa: E731
                                           "exception": "", "component": comp, "program": "ZP", "host": "h"}
        first = [d("090000", "OLD", "CONVT_NO_NUMBER")] * 1
        items = self.CA.grade("PS4", self.CA.values("PS4", raw(dumps={"rows": first, "error": ""}), T0))
        self.assertEqual(self.by_key(items, "sap.st22.user_dumps_10min", "OLD"), [])   # baseline: known
        new = first + [d(f"10{i:02d}00", "U1", "CONVT_NO_NUMBER", "FI-GL") for i in range(10)] + \
            [d("101100", "U1", "TSV_TNEW_PAGE_ALLOC_FAILED")]
        t1 = T0 + timedelta(minutes=5)
        items = self.CA.grade("PS4", self.CA.values("PS4", raw(dumps={"rows": new, "error": ""}), t1))
        u1 = self.by_key(items, "sap.st22.user_dumps_10min", "U1")[0]
        self.assertEqual((u1["value"], u1["status"]), (11, "CRITICAL"))      # more than 10 in 15 min
        self.assertEqual(u1["teams"], ["ABAP", "FI", "Basis"])
        # 16 minutes later nothing new: out of the 15-minute window
        items = self.CA.grade("PS4", self.CA.values("PS4", raw(dumps={"rows": new, "error": ""}),
                                                      t1 + timedelta(minutes=16)))
        self.assertEqual(self.by_key(items, "sap.st22.user_dumps_10min", "U1"), [])

    def test_dumps_gap_rebaselines(self):
        d = {"date": "20261012", "time": "100000", "user": "U1", "error": "X", "exception": "",
             "component": "", "program": "", "host": ""}
        self.CA.values("PS4", raw(), T0)
        late = T0 + timedelta(minutes=40)                       # dashboard was down
        items = self.CA.values("PS4", raw(dumps={"rows": [d] * 1, "error": ""}), late)
        self.assertEqual(self.by_key(items, "sap.st22.user_dumps_10min", "U1"), [])

    def test_cancelled_jobs_per_user_by_sap_time(self):
        rows = [{"job": f"J{i}", "count": str(i), "user": "ALICE",
                 "end": (T0 - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")} for i in range(12)]
        rows += [{"job": "OLD", "count": "1", "user": "BOB", "end": "2026-10-12 08:00:00"}]
        items = self.CA.grade("PS4", self.CA.values(
            "PS4", raw(cancelled={"rows": rows, "emails": {"ALICE": "alice@x.com"}, "error": ""}), T0))
        a = self.by_key(items, "sap.sm37.user_cancelled_15min", "ALICE")[0]
        self.assertEqual((a["value"], a["status"], a["email"]), (12, "WARNING", "alice@x.com"))
        self.assertEqual(self.by_key(items, "sap.sm37.user_cancelled_15min", "BOB"), [])

    def test_locks_abc(self):
        rows = ([{"user": "U1", "table": "VBAK", "arg": "1", "age_min": 20.0, "standing": False}] * 55
                + [{"user": f"N{i}", "table": "MARA", "arg": str(i), "age_min": 3.0, "standing": False}
                   for i in range(4)]
                + [{"user": "SYS", "table": "/SDF/X", "arg": "", "age_min": 900.0, "standing": True}])
        items = self.CA.grade("PS4", self.CA.values("PS4", raw(locks={"rows": rows, "error": ""}), T0))
        a = self.by_key(items, "sap.sm12.user_old_locks", "U1")[0]
        self.assertEqual((a["value"], a["status"]), (55, "WARNING"))             # 50/100
        b = self.by_key(items, "sap.sm12.lock_held_min")[0]
        self.assertEqual((b["value"], b["status"]), (20, "WARNING"))             # standing lock ignored
        c = self.by_key(items, "sap.sm12.users_same_table_15min", "MARA")[0]
        self.assertEqual((c["value"], c["status"]), (4, "WARNING"))              # 3/5

    def test_lock_details_name_users_tables_and_times(self):
        rows = ([{"user": "NGPCFA02", "table": "VBAK", "arg": str(i), "age_min": 20.0 + i, "standing": False}
                 for i in range(3)]
                + [{"user": u, "table": "MARA", "arg": "1", "age_min": a, "standing": False}
                   for u, a in (("U1", 3.0), ("U2", 7.0), ("U3", 12.0))])
        items = self.CA.values("PS4", raw(locks={"rows": rows, "error": ""}), T0)
        a = self.by_key(items, "sap.sm12.user_old_locks", "NGPCFA02")[0]["detail"]
        self.assertIn("on VBAK 3", a)
        self.assertIn("oldest set at 09:38 (22 min ago)", a)
        c = self.by_key(items, "sap.sm12.users_same_table_15min", "MARA")[0]["detail"]
        self.assertIn("U3 at 09:48 (12 min ago), U2 at 09:53 (7 min ago), U1 at 09:57 (3 min ago)", c)

    def test_combined_critical_mail_is_off_unless_asked(self):
        from notifications import email_alert
        from core.models import MonitoringResult, Status
        r = MonitoringResult(system="PS4", client="100")
        r.overall_status = Status.CRITICAL
        os.environ.pop("IBO_LEGACY_VALUE_MAILS", None)
        self.assertFalse(email_alert.send_critical_alert(r, {}))

    def test_locks_without_time_are_unknown(self):
        rows = [{"user": "U1", "table": "VBAK", "arg": "1", "age_min": None, "standing": False}]
        items = self.CA.grade("PS4", self.CA.values("PS4", raw(locks={"rows": rows, "error": ""}), T0))
        self.assertTrue(all(i["status"] == "UNKNOWN" for i in items if i["key"].startswith("sap.sm12.")))

    def test_sm58_and_running_jobs(self):
        r = raw(sm58={"count": 11, "dests": {"BWCLNT": 11}, "error": ""},
                running={"rows": [{"job": "ZBIG", "count": "1", "user": "U", "start": "x", "minutes": 95.0},
                                  {"job": "ZOK", "count": "2", "user": "U", "start": "x", "minutes": 5.0}],
                         "error": ""})
        items = self.CA.grade("PS4", self.CA.values("PS4", r, T0))
        self.assertEqual(self.by_key(items, "sap.sm58.failed_today")[0]["status"], "WARNING")     # 11/25
        self.assertEqual(self.by_key(items, "sap.sm37.longest_active_job_min", "ZBIG")[0]["status"], "CRITICAL")
        self.assertEqual(self.by_key(items, "sap.sm37.longest_active_job_min", "ZOK")[0]["status"], "NORMAL")

    def test_smq2_waits_counted_from_first_seen(self):
        q = lambda ids, st="READY": {"rows": [{"id": i, "queue": "Q1", "state": st} for i in ids],  # noqa: E731
                                     "id_kind": "entry", "error": ""}
        it = self.CA.values("PS4", raw(smq2=q(["a", "b"])), T0)
        self.assertEqual(self.by_key(it, "sap.smq2.waiting_10min")[0]["value"], 0)
        it = self.CA.values("PS4", raw(smq2=q(["a", "b", "c"], "SYSFAIL")), T0 + timedelta(minutes=10))
        self.assertEqual(self.by_key(it, "sap.smq2.waiting_10min")[0]["value"], 2)
        self.assertEqual(self.by_key(it, "sap.smq2.error_10min")[0]["value"], 2)
        it = self.CA.values("PS4", raw(smq2=q(["c"])), T0 + timedelta(minutes=15))   # a, b processed
        self.assertEqual(self.by_key(it, "sap.smq2.waiting_10min")[0]["value"], 0)

    def test_errors_become_unknown(self):
        r = raw(dumps={"rows": [], "error": "dump details need /SDF/GET_DUMP_LOG"},
                sm58={"count": None, "error": "ARFCSSTATE: no auth"})
        items = self.CA.grade("PS4", self.CA.values("PS4", r, T0))
        self.assertEqual(self.by_key(items, "sap.st22.user_dumps_10min")[0]["status"], "UNKNOWN")
        self.assertEqual(self.by_key(items, "sap.sm58.failed_today")[0]["status"], "UNKNOWN")


class Flow(_Env):
    def test_dump_alert_goes_to_teams_and_recovers(self):
        self.alert_on("sap.st22.user_dumps_10min")
        d = lambda t: {"date": "20261012", "time": t, "user": "U1", "error": "CONVT_NO_NUMBER",  # noqa: E731
                       "exception": "", "component": "SD-SLS", "program": "ZP", "host": "h"}
        self.run_once(raw(), T0)
        self.run_once(raw(dumps={"rows": [d(f"10{i:02d}00") for i in range(12)], "error": ""}),
                      T0 + timedelta(minutes=5))
        self.assertEqual(len(self.sent), 1)
        job = self.sent[0]
        self.assertEqual((job["level"], job["instance"], job["teams"]), ("CRITICAL", "U1", ["ABAP", "SD", "Basis"]))
        self.assertIn("12 dump(s) by U1", job["detail"])
        # window passes: U1 no longer listed -> recovery
        self.run_once(raw(dumps={"rows": [d(f"10{i:02d}00") for i in range(12)], "error": ""}),
                      T0 + timedelta(minutes=25))
        self.assertEqual(self.sent[-1]["kind"], "recovery")

    def test_cancelled_jobs_raise_approval_once(self):
        self.alert_on("sap.sm37.user_cancelled_15min", "critical")
        rows = [{"job": f"J{i}", "count": str(i), "user": "ALICE",
                 "end": (T0 - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")} for i in range(15)]
        r = raw(cancelled={"rows": rows, "emails": {"ALICE": "alice@x.com"}, "error": ""})
        self.run_once(r, T0)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].get("approval_id"))
        from core import job_user_mail
        self.run_once(r, T0 + timedelta(minutes=5))
        self.assertEqual(len(job_user_mail.waiting()), 1)                # one request per user

    def test_system_account_gets_no_approval(self):
        self.alert_on("sap.sm37.user_cancelled_15min", "critical")
        rows = [{"job": f"J{i}", "count": str(i), "user": "DDIC",
                 "end": (T0 - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")} for i in range(15)]
        self.run_once(raw(cancelled={"rows": rows, "emails": {}, "error": ""}), T0)
        self.assertFalse(self.sent[0].get("approval_id"))
        self.assertIn("system account", self.sent[0]["detail"])

    def test_off_by_default_nothing_sent(self):
        r = raw(sm58={"count": 99, "dests": {}, "error": ""})
        items = self.run_once(r, T0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.by_key(items, "sap.sm58.failed_today")[0]["status"], "CRITICAL")
        self.assertTrue(self.CA.readings("PS4")["items"])


class Approval(_Env):
    def _req(self, email="alice@x.com", user="ALICE"):
        from core import job_user_mail
        return job_user_mail.raise_request("PS4", user, email, [{"job": "J1", "count": "1", "end": "x"}])

    def _mailer(self, ok=True):
        from notifications import mailer
        calls = []
        saved = (mailer.email_enabled, mailer.send_html_now)
        mailer.email_enabled = lambda: True
        mailer.send_html_now = lambda subject, html, to, smtp, cc=None, text_fallback=None, **k: \
            calls.append((subject, to, cc)) or ok
        self.addCleanup(lambda: (setattr(mailer, "email_enabled", saved[0]),
                                 setattr(mailer, "send_html_now", saved[1])))
        from core import config_loader
        s2, s3 = config_loader.get_all_systems, config_loader.get_smtp_config
        config_loader.get_all_systems = lambda: [{"name": "PS4"}]
        config_loader.get_smtp_config = lambda cfg, require_recipients=True: {"host": "stub"}
        self.addCleanup(lambda: (setattr(config_loader, "get_all_systems", s2),
                                 setattr(config_loader, "get_smtp_config", s3)))
        return calls

    def test_approve_sends_to_user_with_basis_cc_once(self):
        from core import job_user_mail
        calls = self._mailer()
        r = self._req()
        msg = job_user_mail.approve(r["id"], by="alex")
        self.assertIn("alice@x.com", msg)
        self.assertEqual(calls[0][1], ["alice@x.com"])
        self.assertEqual(calls[0][2], ["basis-default@x.com"])
        with self.assertRaises(ValueError):
            job_user_mail.approve(r["id"], by="someone else")       # already sent
        self.assertEqual(len(calls), 1)
        hist = self.E.history("PS4")
        self.assertEqual((hist[0]["kind"], hist[0]["ok"]), ("user_mail", 1))

    def test_failed_mail_can_be_retried_and_no_address_refused(self):
        from core import job_user_mail
        self._mailer(ok=False)
        r = self._req()
        with self.assertRaises(ValueError):
            job_user_mail.approve(r["id"])
        self.assertEqual(job_user_mail.waiting()[0]["status"], "failed")
        r2 = self._req(email="", user="BOB")
        with self.assertRaises(ValueError) as ctx:
            job_user_mail.approve(r2["id"])
        self.assertIn("no mail address", str(ctx.exception))

    def test_expired_and_excluded(self):
        from core import job_user_mail, ops_db
        self._mailer()
        r = self._req()
        ops_db.execute("UPDATE job_user_requests SET created_at=? WHERE id=?",
                       ((datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
        with self.assertRaises(ValueError):
            job_user_mail.approve(r["id"])
        self.assertIsNone(self._req(user="DDIC"))
        self.assertIsNone(self._req(user="SAP_WFRT"))

    def test_signed_link_does_it_once(self):
        from core import action_links
        calls = self._mailer()
        r = self._req()
        tok = action_links.make("mail_job_user", "PS4", kind="job_user_mail", extra={"r": r["id"]})
        self.assertIn("ALICE", action_links.describe(action_links.verify(tok)))
        self.assertIn("Done", action_links.perform(tok, "Alex"))
        with self.assertRaises(ValueError):
            action_links.perform(tok, "Alex")
        self.assertEqual(len(calls), 1)

    def test_button_in_alert_mail(self):
        from core import job_user_mail
        r = self._req()
        os.environ["IBO_PUBLIC_URL"] = "http://10.1.0.50:8000"
        os.environ["IBO_INTERNAL_DOMAINS"] = "x.com"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("IBO_PUBLIC_URL", "IBO_INTERNAL_DOMAINS")])
        self.assertIn("/access/action?t=", job_user_mail.button_html(r["id"], ["basis@x.com"]))
        self.assertIn("Alert history", job_user_mail.button_html(r["id"], ["someone@customer.com"]))
        r2 = self._req(email="", user="BOB")
        self.assertIn("no mail address", job_user_mail.button_html(r2["id"], ["basis@x.com"]))


class ReviewFixes(Approval):
    def test_same_jobs_never_mailed_twice(self):
        from core import job_user_mail
        calls = self._mailer()
        jobs = [{"job": "J1", "count": "1", "end": "x"}, {"job": "J2", "count": "2", "end": "x"}]
        r = job_user_mail.raise_request("PS4", "ALICE", "alice@x.com", jobs)
        job_user_mail.approve(r["id"], by="g")
        self.assertIsNone(job_user_mail.raise_request("PS4", "ALICE", "alice@x.com", jobs))   # nothing new
        r2 = job_user_mail.raise_request("PS4", "ALICE", "alice@x.com",
                                         jobs + [{"job": "J3", "count": "3", "end": "x"}])
        self.assertEqual([j["job"] for j in json.loads(r2["jobs"])], ["J3"])                # only the new one
        job_user_mail.approve(r2["id"], by="g")
        self.assertEqual(len(calls), 2)

    def test_failed_request_is_reused_not_duplicated(self):
        from core import job_user_mail
        self._mailer(ok=False)
        r = self._req()
        with self.assertRaises(ValueError):
            job_user_mail.approve(r["id"])
        again = self._req()
        self.assertEqual(again["id"], r["id"])
        self.assertEqual(len(job_user_mail.waiting()), 1)

    def test_recovery_goes_to_the_alerts_teams(self):
        self.alert_on("sap.st22.user_dumps_10min")
        d = lambda t: {"date": "20261012", "time": t, "user": "BOB", "error": "CONVT_NO_NUMBER",  # noqa: E731
                       "exception": "", "component": "", "program": "ZP", "host": "h"}
        rows = [d(f"10{i:02d}00") for i in range(12)]
        self.run_once(raw(), T0)
        self.run_once(raw(dumps={"rows": rows, "error": ""}), T0 + timedelta(minutes=5))
        self.run_once(raw(dumps={"rows": rows, "error": ""}), T0 + timedelta(minutes=25))
        self.assertEqual([(j["kind"], j["teams"]) for j in self.sent], [("alert", ["ABAP"]), ("recovery", ["ABAP"])])

    def test_capped_smq2_keeps_first_seen(self):
        q = lambda ids, capped: {"rows": [{"id": i, "queue": "Q", "state": ""} for i in ids],  # noqa: E731
                                 "id_kind": "entry", "error": "", "capped": capped}
        self.CA.values("PS4", raw(smq2=q(["a", "b"], True)), T0)
        self.CA.values("PS4", raw(smq2=q(["a"], True)), T0 + timedelta(minutes=5))      # b not in this capped read
        it = self.CA.values("PS4", raw(smq2=q(["a", "b"], True)), T0 + timedelta(minutes=10))
        w = self.by_key(it, "sap.smq2.waiting_10min")[0]
        self.assertEqual(w["value"], 2)
        self.assertIn("at least", w["detail"])

    def test_skipped_instance_is_named(self):
        r = raw(wp={"error": "", "skipped": ["app2"], "instances": [
            {"name": "app1", "bgd_total": 4, "bgd_busy": 1, "longest": None}]})
        it = self.CA.values("PS4", r, T0)
        self.assertIn("not readable: app2", self.by_key(it, "sap.sm66.bgd_saturation_pct")[0]["detail"])

    def test_lock_age_uses_given_sap_time(self):
        from collectors import rfc_perf
        enq = [{"GUNAME": "U", "GNAME": "T", "GARG": "", "GTDATE": "20261012", "GTTIME": "093000"}]
        rfc_perf._owner_offset_min.pop("PS4", None)
        ref = rfc_perf.lock_reference(enq, "PS4", datetime(2026, 10, 12, 10, 0))
        self.assertEqual(ref, datetime(2026, 10, 12, 10, 0))


class EngineRouting(_Env):
    def test_send_all_uses_team_addresses_and_shows_detail(self):
        from core import alert_engine as E, alert_teams as AT
        AT.save(None, {"Basis": "basis@x.com", "ABAP": "abap@x.com"})
        got = {}
        saved = E._send_email
        E._send_email = lambda cfg, job, rcp: (got.update(rcp=rcp, job=job) or (True, ", ".join(rcp["to"]), ""))
        self.addCleanup(lambda: setattr(E, "_send_email", saved))
        E._send_all({"name": "PS4"}, {"system": "PS4", "metric": "sap.st22.user_dumps_10min", "instance": "U1",
                                      "label": "Dumps", "level": "WARNING", "kind": "alert", "value": "5",
                                      "limits": "", "channels": ["email"], "teams": ["ABAP", "FI"],
                                      "detail": "5 dumps"})
        self.assertEqual(got["rcp"]["to"], ["abap@x.com", "basis@x.com"])
        subject, html, text = E.render_email(got["job"])
        self.assertIn("5 dumps", html)
        self.assertIn("FI has no address", html)


if __name__ == "__main__":
    unittest.main()
