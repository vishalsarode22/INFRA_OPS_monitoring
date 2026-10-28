"""
Per-value alert mails: Acknowledge / Snooze / Ignore buttons for our own
addresses (a separate copy when a customer is also on the list), buttons that
act on one value only, and the ABAP dump breakdown (last 10 minutes by user
and team, busiest 10 minutes, today by hour). Plain unittest, no SAP.
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

ENV = ("IBO_PUBLIC_URL", "IBO_INTERNAL_DOMAINS", "IBO_INTERNAL_ADDRESSES", "IBO_MAIL_BUTTONS_ALWAYS")


def dump(t, user, err, comp="", date="20261012"):
    return {"date": date, "time": t, "user": user, "error": err, "exception": "", "component": comp,
            "program": "ZP", "host": "h"}


class _Mail(_Env):
    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        from notifications import mailer
        from core import config_loader
        self.got = []
        self._msaved = (mailer.send_html_now, mailer.email_enabled, config_loader.get_smtp_config)
        mailer.send_html_now = lambda subject, html, to, smtp, cc=None, text_fallback=None, **k: \
            self.got.append({"subject": subject, "html": html, "to": list(to), "cc": list(cc or []),
                             "reply_to": k.get("reply_to")}) or True
        mailer.email_enabled = lambda: True
        config_loader.get_smtp_config = lambda cfg, require_recipients=True: {"host": "h", "from_email": "alerts@x.com"}

    def tearDown(self):
        from notifications import mailer
        from core import config_loader
        (mailer.send_html_now, mailer.email_enabled, config_loader.get_smtp_config) = self._msaved
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def job(self, **kw):
        j = {"system": "CEAT S4P", "metric": "sap.st22.dumps", "instance": "", "skey": "sap.st22.dumps",
             "label": "ABAP dumps today", "level": "WARNING", "kind": "alert", "value": "129",
             "limits": "warning ≥ 120", "channels": ["email"]}
        j.update(kw)
        return j

    def send(self, to, cc=(), **kw):
        return self.E._send_email({"name": "CEAT S4P"}, self.job(**kw), {"to": list(to), "cc": list(cc)})


class Buttons(_Mail):
    RCP = (["basis1@client-a.example", "staff.d@infrabeat.com"],
           ["staff.f@infrabeat.com", "someone@gmail.com"])

    def test_mixed_list_gets_two_copies(self):
        os.environ.update(IBO_PUBLIC_URL="http://10.1.0.50:8000", IBO_INTERNAL_DOMAINS="infrabeat.com")
        ok, who, note = self.send(*self.RCP)
        self.assertTrue(ok, note)
        self.assertEqual(len(self.got), 2)
        ours, theirs = self.got
        self.assertEqual(ours["to"], ["staff.d@infrabeat.com"])
        self.assertEqual(ours["cc"], ["staff.f@infrabeat.com"])
        for label in ("Acknowledge (I&#x27;m on it)", "Snooze 1 h", "Snooze 4 h", "Ignore until back to normal"):
            self.assertIn(label, ours["html"])
        self.assertIn("Also sent, without the buttons, to: basis1@client-a.example, someone@gmail.com", ours["html"])
        self.assertEqual(theirs["to"], ["basis1@client-a.example"])
        self.assertEqual(theirs["cc"], ["someone@gmail.com"])
        self.assertNotIn("/access/action", theirs["html"])
        self.assertEqual(ours["subject"], theirs["subject"])
        self.assertIsNone(ours["reply_to"])
        self.assertEqual(theirs["reply_to"], ["alerts@x.com", "staff.d@infrabeat.com", "staff.f@infrabeat.com"])
        self.assertIn("basis1@client-a.example", who)

    def test_single_address_counts_as_ours(self):
        os.environ.update(IBO_PUBLIC_URL="http://x", IBO_INTERNAL_DOMAINS="infrabeat.com",
                          IBO_INTERNAL_ADDRESSES="someone@gmail.com")
        self.send(*self.RCP)
        self.assertEqual(self.got[0]["cc"], ["staff.f@infrabeat.com", "someone@gmail.com"])
        self.assertEqual(self.got[1]["to"], ["basis1@client-a.example"])

    def test_no_public_url_one_mail_no_buttons(self):
        os.environ.update(IBO_INTERNAL_DOMAINS="infrabeat.com")
        self.send(*self.RCP)
        self.assertEqual(len(self.got), 1)
        self.assertNotIn("/access/action", self.got[0]["html"])
        self.assertEqual(self.got[0]["to"], self.RCP[0])

    def test_no_domains_no_buttons(self):
        os.environ.update(IBO_PUBLIC_URL="http://x")
        self.send(*self.RCP)
        self.assertEqual(len(self.got), 1)
        self.assertNotIn("/access/action", self.got[0]["html"])

    def test_all_ours_one_mail_with_buttons(self):
        os.environ.update(IBO_PUBLIC_URL="http://x", IBO_INTERNAL_DOMAINS="infrabeat.com")
        self.send(["a@infrabeat.com"], ["b@infrabeat.com"])
        self.assertEqual(len(self.got), 1)
        self.assertIn("Snooze 1 h", self.got[0]["html"])
        self.assertNotIn("Also sent", self.got[0]["html"])

    def test_recovery_has_no_buttons(self):
        os.environ.update(IBO_PUBLIC_URL="http://x", IBO_INTERNAL_DOMAINS="infrabeat.com")
        self.send(["a@infrabeat.com"], kind="recovery", level="NORMAL")
        self.assertNotIn("/access/action", self.got[0]["html"])

    def test_one_copy_failing_is_reported(self):
        from notifications import mailer
        os.environ.update(IBO_PUBLIC_URL="http://x", IBO_INTERNAL_DOMAINS="infrabeat.com")
        mailer.send_html_now = lambda subject, html, to, smtp, cc=None, **k: "client-a" in to[0]
        ok, _, note = self.send(*self.RCP)
        self.assertFalse(ok)
        self.assertIn("our copy not sent", note)


class ValueOnly(_Mail):
    def _token(self, action, key="sap.st22.dumps", level="WARNING", minutes=None):
        from core import action_links
        return action_links.make(action, "CEAT S4P", f"metric:{key}", minutes=minutes,
                                 extra={"lb": "ABAP dumps today", "l": level})

    def test_snooze_holds_this_value_only(self):
        from core import action_links, alert_control as AC
        msg = action_links.perform(self._token("snooze", minutes=60), actor="Kiran")
        self.assertIn("'ABAP dumps today' alert", msg)
        self.assertTrue(AC.gate("CEAT S4P", "metric", "sap.st22.dumps", level="WARNING"))
        self.assertIsNone(AC.gate("CEAT S4P", "metric", "sap.smq2.entries", level="WARNING"))
        self.assertIsNone(AC.gate("CEAT S4P", "metric", "sap.st22.dumps", level="CRITICAL"))   # rise
        self.assertIsNone(AC.gate("CEAT S4P", "system_down"))

    def test_ignore_does_not_park_and_ends_when_normal(self):
        from core import action_links, alert_control as AC
        action_links.perform(self._token("until_fixed"))
        self.assertFalse(AC.parked("CEAT S4P"))
        self.assertIn("back to normal", AC.gate("CEAT S4P", "metric", "sap.st22.dumps", level="WARNING"))
        self.E._clear_value_silences("CEAT S4P", "sap.st22.dumps")
        self.assertIsNone(AC.gate("CEAT S4P", "metric", "sap.st22.dumps", level="WARNING"))

    def test_per_user_ack(self):
        from core import action_links, alert_control as AC
        action_links.perform(self._token("ack", key="sap.st22.user_dumps_10min@U1"))
        self.assertTrue(AC.gate("CEAT S4P", "metric", "sap.st22.user_dumps_10min@U1", level="WARNING"))
        self.assertIsNone(AC.gate("CEAT S4P", "metric", "sap.st22.user_dumps_10min@U2", level="WARNING"))
        self.E._clear_value_silences("CEAT S4P", "sap.st22.user_dumps_10min@U1")
        self.assertIsNone(AC.gate("CEAT S4P", "metric", "sap.st22.user_dumps_10min@U1", level="WARNING"))

    def test_rule_switched_off_ends_the_value_ack(self):
        from core import action_links, alert_control as AC
        self.cfg = {"name": "CEAT S4P"}
        self.T.save_override("CEAT S4P", "sap.st22.dumps", 120, 150, alert="warning", channels=["email"])
        self.T._cache.clear()
        it = {"key": "sap.st22.dumps", "label": "ABAP dumps today", "value": 129, "status": "WARNING",
              "warning": 120, "critical": 150}
        self.E.observe(self.cfg, [it], sender=lambda c, j: None)
        action_links.perform(self._token("ack"))
        self.T.save_override("CEAT S4P", "sap.st22.dumps", 120, 150, alert="off", channels=[])
        self.T._cache.clear()
        self.clock[0] = T0 + timedelta(minutes=1)
        self.E.observe(self.cfg, [it], sender=lambda c, j: None)
        self.assertEqual(AC.active_silences("CEAT S4P"), [])

    def test_critical_ack_holds_critical(self):
        from core import action_links, alert_control as AC
        action_links.perform(self._token("ack", level="CRITICAL"))
        self.assertTrue(AC.gate("CEAT S4P", "metric", "sap.st22.dumps", level="CRITICAL"))

    def test_ack_from_mail_stops_repeats_and_recovery_clears(self):
        from core import action_links, alert_control as AC
        self.cfg = {"name": "CEAT S4P"}
        self.T.save_override("CEAT S4P", "sap.st22.dumps", 120, 150, alert="warning", channels=["email"])
        self.T._cache.clear()
        it = lambda v, st: {"key": "sap.st22.dumps", "label": "ABAP dumps today", "value": v,  # noqa: E731
                            "status": st, "warning": 120, "critical": 150}
        sent = []
        snd = lambda c, j: sent.append(j)  # noqa: E731
        self.E.observe(self.cfg, [it(129, "WARNING")], sender=snd)
        self.assertEqual(len(sent), 1)
        action_links.perform(self._token("ack"))
        self.clock[0] = T0 + timedelta(minutes=self.E.repeat_minutes("WARNING") + 1)
        jobs = self.E.observe(self.cfg, [it(130, "WARNING")], sender=snd)
        self.assertTrue(jobs and jobs[0].get("held"))               # repeat held
        self.clock[0] += timedelta(minutes=1)
        jobs = self.E.observe(self.cfg, [it(160, "CRITICAL")], sender=snd)
        self.assertFalse(jobs[0].get("held"))                       # rise to CRITICAL goes out
        self.clock[0] += timedelta(minutes=1)
        self.E.observe(self.cfg, [it(10, "NORMAL")], sender=snd)
        self.assertEqual(AC.active_silences("CEAT S4P"), [])


class Breakdown(_Env):
    # The breakdown's window is the dump alert's time window (core/alert_windows.py,
    # 15 by default); these cases were written for a 10-minute window.
    def setUp(self):
        super().setUp()
        self.cfg = {"name": "PS4"}
        from core import alert_windows
        k = "sap.st22.user_dumps_10min"
        self._win = alert_windows.WINDOWS[k]
        alert_windows.WINDOWS[k] = (10,) + tuple(self._win[1:])

    def tearDown(self):
        from core import alert_windows
        alert_windows.WINDOWS["sap.st22.user_dumps_10min"] = self._win
        super().tearDown()

    def feed(self, rows, at, sap_now=None):
        r = raw(dumps={"rows": rows, "error": ""})
        r["sap_now"] = (sap_now or at).strftime("%Y-%m-%d %H:%M:%S")
        self.CA.values("PS4", r, at)

    def test_last_10_min_users_and_teams(self):
        old = [dump(f"011{m}{s:02d}", "NIGHT", "MESSAGE_TYPE_X") for m in range(10) for s in (0, 20, 40)]
        old += [dump(f"03{m:02d}00", "NIGHT", "MESSAGE_TYPE_X") for m in range(0, 5)]
        self.feed(old, T0)                                          # baseline: known
        new = ([dump(f"1004{s:02d}", "U1", "DBIF_RSQL_SQL_ERROR") for s in range(12)]
               + [dump(f"1003{s:02d}", "U2", "MESSAGE_TYPE_X") for s in range(8)])
        t1 = T0 + timedelta(minutes=5)
        self.feed(old + new, t1)
        rows = dict(self.CA.dump_breakdown("PS4", now=t1))
        last = rows["Last 10 minutes"]
        self.assertIn("20 new dump(s) by 2 user(s): U1 12, U2 8", last)
        self.assertIn("teams: Basis 12, ABAP 8", last)
        self.assertIn("01:09-01:19 SAP time: 30 dump(s) (NIGHT 30)", rows["Busiest 10 minutes today"])  # night spike
        self.assertIn("01h 30 (peak)", rows["Today by hour"])
        self.assertIn("03h 5", rows["Today by hour"])
        self.assertIn("10h 20", rows["Today by hour"])
        self.assertIn("55 today, first 01:10", rows["Today by hour"])
        self.assertEqual(rows["Today by team"], "ABAP 43, Basis 12")
        self.assertIn("NIGHT 35", rows["Top users today"])
        # one user's alert
        u = dict(self.CA.dump_breakdown("PS4", user="U2", now=t1))
        self.assertEqual(u["Last 10 minutes"].split(" · ")[0], "8 new dump(s) by U2")
        self.assertNotIn("Top users today", u)
        # 15 minutes later: nothing new
        later = dict(self.CA.dump_breakdown("PS4", now=t1 + timedelta(minutes=15)))
        self.assertEqual(later["Last 10 minutes"], "no new dumps")

    def test_dump_log_in_utc_is_moved_to_sap_time(self):
        self.feed([], T0)
        utc = lambda d: (d - timedelta(hours=5, minutes=30)).strftime("%H%M%S")  # noqa: E731
        new = [dump(utc(T0 + timedelta(minutes=4, seconds=s)), "U1", "MESSAGE_TYPE_X") for s in range(3)]
        t1 = T0 + timedelta(minutes=5)
        self.feed(new, t1)
        rows = dict(self.CA.dump_breakdown("PS4", now=t1))
        self.assertIn("10h 3", rows["Today by hour"])
        self.assertIn("moved +5.5 h onto SAP time", rows["Busiest 10 minutes today"])

    def test_stale_or_unread_gives_nothing(self):
        self.assertEqual(self.CA.dump_breakdown("PS4", now=T0), [])
        self.feed([], T0)
        self.assertEqual(self.CA.dump_breakdown("PS4", now=T0 + timedelta(hours=1)), [])

    def test_mail_shows_breakdown(self):
        self.feed([], T0)
        t1 = T0 + timedelta(minutes=5)
        self.feed([dump("100400", "U1", "MESSAGE_TYPE_X")] * 1, t1)
        self.clock[0] = t1
        self.T.save_override("PS4", "sap.st22.dumps", 1, 150, alert="warning", channels=["email"])
        self.T._cache.clear()
        sent = []
        self.E.observe(self.cfg, [{"key": "sap.st22.dumps", "label": "ABAP dumps today", "value": 1,
                                   "status": "WARNING", "warning": 1, "critical": 150}],
                       sender=lambda c, j: sent.append(j))
        self.assertTrue(sent[0].get("breakdown"))
        subject, html, text = self.E.render_email(sent[0])
        self.assertIn("Last 10 minutes", html)
        self.assertIn("1 new dump(s) by 1 user(s): U1 1", text)


if __name__ == "__main__":
    unittest.main()
