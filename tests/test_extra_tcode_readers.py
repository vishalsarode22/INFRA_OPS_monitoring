"""
The seven new screen readers (ST03N workload, SMICM, ST02, ST04, ST05, STMS,
STRUST) run against screens recorded on a real S/4HANA system by the
read-only probe (hostnames anonymised). Proves the values come out right
and that nothing but reading is done on the system.
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_sapgui as F  # noqa: E402
from sap_gui import tcode_actions_extra as X  # noqa: E402

S = F.screens()
F.install(X, {})

# The only things the readers may press / choose. Anything else fails the test.
ALLOWED = {
    "press": {"System Overview", "Connection Test", "Apply Selection",
              "Display <-> Change"},          # STRUST opened in change mode -> display, before any PSE is opened
    "menu": {("Goto", "Services"), ("Overview", "Systems")},
    "vkey": {2},                 # F2 = show certificate details (display only)
    "popup_vkey": {12},          # F12 = Cancel on any popup
}


def read_only(test, session):
    for kind, value in session.log:
        if kind in ("goto", "open", "tab"):
            continue
        test.assertIn(value, ALLOWED.get(kind, set()), f"not allowed: {kind} {value}")


class Shots(list):
    def __call__(self, suffix=""):
        self.append(suffix)
        return f"/tmp/{suffix}.png"


def go(session, screen_name_or_screen, status=("", "")):
    return lambda s, v: s.show(screen_name_or_screen if isinstance(screen_name_or_screen, dict)
                               else F.clone(S[screen_name_or_screen]), status)


class ST03NWorkload(unittest.TestCase):
    def _start(self, instance_shown="host1_S4Q_10", rows=None, second_instance=False):
        tree = F.clone(S["st03n_total"]["trees"][0])
        if second_instance:
            extra = []
            for n in tree["nodes"]:
                if n["key"].startswith("B.001"):
                    m = dict(n, key=n["key"].replace("B.001", "B.002", 1),
                             parent=n["parent"].replace("B.001", "B.002", 1) if n["parent"] != "B" else "B",
                             text=n["text"].replace("S4Q_10", "S4Q_11"))
                    extra.append(m)
            i = next(i for i, n in enumerate(tree["nodes"]) if n["key"] == "B.999")
            tree["nodes"][i:i] = extra
        start = F.clone(S["st03n_start"])
        start["trees"] = [tree]
        start["objects"] = [o for o in start["objects"] if o.get("subtype") != "Tree"]
        sess = F.Session(start)
        sess.on("goto", "ST03N", lambda s, v: None)

        def today_screen(inst, factor):
            sc = F.clone(S["st03n_total"])
            sc["trees"] = [tree]
            sc["objects"] = [o for o in sc["objects"] if o.get("subtype") != "Tree"]
            F.set_obj(sc, "G_WL_DYNPRO-INSTANCE", text=inst)
            F.set_obj(sc, "G_WL_DYNPRO-PERIOD_TYPE", text=f"{date.today():%d.%m.%Y}")
            grid = next(g for g in sc["grids"] if any(c["id"] == "TASKTYPE" for c in g["columns"]))
            for r in grid["rows"]:
                if r["TASKTYPE"] == "DIALOG":
                    r.update(DIASTEPCNT=f"{1000 * factor:,}", MRESPTIME=f"{500 * factor:,.1f}", MCPUTI="100.0")
                if r["TASKTYPE"] == "BACKGROUND" and rows == "no_background":
                    r["TASKTYPE"] = "BACKGROUNDX"
            return sc

        def opened(s, v):
            text, key = v
            if text == "Today" and key.startswith("B.001"):
                s.show(today_screen(instance_shown, 1))
            elif text == "Today" and key.startswith("B.002"):
                s.show(today_screen("host1_S4Q_11", 3))
            elif key.startswith("B.999.1."):
                sc = F.clone(S["st03n_total"])
                sc["trees"] = [tree]
                sc["objects"] = [o for o in sc["objects"] if o.get("subtype") != "Tree"]
                s.show(sc)
        sess.on("open", None, opened)
        return sess

    def test_today_one_instance(self):
        sess, shots = self._start(), Shots()
        X.SETTINGS = {}
        f = X.action_st03n_workload(sess, shots)
        self.assertEqual(f["instance"], "all instances")
        self.assertIn("Today", f["period"])
        d = f["task_types"]["DIALOG"]
        self.assertEqual((d["steps"], d["avg_ms"], d["cpu_ms"]), (1000, 500.0, 100.0))
        self.assertEqual(d["total_s"], 500.0)
        self.assertEqual(set(f["task_types"]), {"DIALOG", "BACKGROUND", "WS-RFC"})
        self.assertNotIn("RFC", f["task_types"])                              # WS-RFC only, as chosen
        self.assertIn("DIALOG 1,000 steps, total 8m 20s, avg 500 ms, CPU 100 ms", f["report_summary"])
        self.assertEqual(shots, ["today_host1_S4Q_10"])
        read_only(self, sess)

    def test_task_type_not_on_screen(self):
        sess = self._start()
        X.SETTINGS = {"task_types": ["DIALOG", "SPOOL-XYZ"]}
        f = X.action_st03n_workload(sess, Shots())
        self.assertIn(["SPOOL-XYZ", "no steps in this period"], f["report_rows"])
        X.SETTINGS = {}

    def test_two_instances_are_combined_weighted_by_steps(self):
        sess = self._start(second_instance=True)
        X.SETTINGS = {"task_types": ["dialog"]}
        f = X.action_st03n_workload(sess, Shots())
        d = f["task_types"]["DIALOG"]
        # 1000 steps at 500 ms + 3000 steps at 1500 ms = 4000 steps, avg 1250 ms
        self.assertEqual((d["steps"], d["avg_ms"]), (4000, 1250.0))
        self.assertEqual(f["st03_instances"], ["host1_S4Q_10", "host1_S4Q_11"])
        X.SETTINGS = {}

    def test_screen_that_did_not_change_is_not_used(self):
        sess = self._start(instance_shown="TOTAL")      # double-click did not land: old screen still shown
        X.SETTINGS = {}
        f = X.action_st03n_workload(sess, Shots())
        self.assertEqual(f["instance"], "TOTAL")
        self.assertIn("latest full day", f["period"])
        self.assertIn("12.10.2026", f["period"])
        self.assertIn("could not be read", f["note"])
        d = f["task_types"]["DIALOG"]
        self.assertEqual((d["steps"], d["avg_ms"], d["cpu_ms"]), (10483, 2784.4, 127.4))   # the real recorded row
        self.assertEqual(f["task_types"]["BACKGROUND"]["steps"], 17808)

    def test_previous_day_setting(self):
        sess = self._start()
        X.SETTINGS = {"period": "previous_day"}
        f = X.action_st03n_workload(sess, Shots())
        self.assertEqual(f["instance"], "TOTAL")
        self.assertNotIn("note", f)
        X.SETTINGS = {}


class SMICM(unittest.TestCase):
    def _run(self, icons):
        sess = F.Session(F.clone(S["smicm_start"]))
        svc = F.clone(S["smicm_services"])
        for y, icon in zip((5, 6, 7), icons):
            svc["objects"].append(F.label(93, y, "", icon))
        sess.on("menu", ("Goto", "Services"), go(sess, svc))
        f = X.action_smicm_services(sess, Shots())
        read_only(self, sess)
        return f

    def test_all_active(self):
        f = self._run(["S_OKAY", "S_OKAY", "S_OKAY"])
        self.assertEqual([(s["protocol"], s["port"], s["active"]) for s in f["services"]],
                         [("HTTP", "8010", True), ("SMTP", "2510", True), ("HTTPS", "5210", True)])
        self.assertEqual(f["inactive"], 0)
        self.assertEqual(f["report_summary"], "3 service(s): HTTP 8010 active, SMTP 2510 active, HTTPS 5210 active")

    def test_inactive_and_unknown(self):
        f = self._run(["S_OKAY", "S_NONO", "S_SOMETHING"])
        self.assertEqual(f["inactive"], 1)
        self.assertEqual(f["unknown_state"], 1)
        self.assertIn(["SMTP 2510", "NOT active"], f["report_rows"])
        self.assertIn(["HTTPS 5210", "state not readable (S_SOMETHING)"], f["report_rows"])

    def test_real_layout_of_14_october(self):
        # Seen on CEAT S4_QAS: empty labels at 92, 97, 98 and the icon S_OKAY at 93.
        sess = F.Session(F.clone(S["smicm_start"]))
        svc = F.clone(S["smicm_services"])
        for y in (5, 6, 7):
            svc["objects"] += [F.label(92, y), F.label(93, y, "", "S_OKAY"), F.label(97, y), F.label(98, y)]
        sess.on("menu", ("Goto", "Services"), go(sess, svc))
        f = X.action_smicm_services(sess, Shots())
        self.assertEqual([s["active"] for s in f["services"]], [True, True, True])
        self.assertEqual(f["unknown_state"], 0)
        self.assertNotIn("actv_debug", f)

    def test_unreadable_cell_is_described(self):
        sess = F.Session(F.clone(S["smicm_start"]))
        svc = F.clone(S["smicm_services"])
        for y in (5, 6, 7):
            svc["objects"].append(F.label(93, y, "", ""))         # what the real screen gave on 14 Oct
        sess.on("menu", ("Goto", "Services"), go(sess, svc))
        f = X.action_smicm_services(sess, Shots())
        self.assertEqual(f["unknown_state"], 3)
        self.assertEqual(len(f["actv_debug"]), 3)
        self.assertIn("lbl[93,5]", f["actv_debug"][0]["objects"][0])
        self.assertTrue(any("diagnostics" in r[0] for r in f["report_rows"]))

    def test_actv_as_list_checkbox(self):
        sess = F.Session(F.clone(S["smicm_start"]))
        svc = F.clone(S["smicm_services"])
        for y, sel in zip((5, 6, 7), (True, False, True)):
            svc["objects"].append({"id": f"/usr/chk[93,{y}]", "type": "GuiCheckBox", "name": "", "text": "",
                                   "selected": sel})
        sess.on("menu", ("Goto", "Services"), go(sess, svc))
        f = X.action_smicm_services(sess, Shots())
        self.assertEqual([s["active"] for s in f["services"]], [True, False, True])


class ST02(unittest.TestCase):
    def test_buffers_and_memory(self):
        sess = F.Session(F.clone(S["st02"]))
        f = X.action_st02_tune(sess, Shots())
        b = {x["buffer"]: x for x in f["buffers"]}
        self.assertEqual(f["snapshot"], "13.10.2026 16:28:47")
        self.assertEqual(f["startup"], "22.08.2026 04:31:00")
        self.assertEqual(f["instance"], "host1_S4Q_10")
        self.assertEqual(b["CUA"]["swaps"], 7165804)
        self.assertEqual(b["CUA"]["hit_ratio"], 96.26)
        self.assertEqual(b["Screen"]["free_pct"], 0.12)
        self.assertEqual(b["program"]["hit_ratio"], 99.78)
        self.assertEqual(b["Nametab Table definition"]["hit_ratio"], 99.95)
        self.assertEqual(b["Tables Buffer"]["swaps"], 20)
        self.assertEqual(b["Export/import"]["swaps"], 265585)
        self.assertNotIn("Nametab (NTAB)", b)
        m = {x["area"]: x for x in f["memory"]}
        self.assertEqual(m["Extended memory"]["use_pct"], 16.87)
        self.assertEqual(m["Page area"]["on_disk_kb"], 1035200)        # not the hit-ratio column next to it
        self.assertEqual(m["Heap memory"]["use_kb"], 63820)
        self.assertNotIn("Call Stati", m)
        read_only(self, sess)


class ST04(unittest.TestCase):
    def _large_rows(self):
        rows = []
        for i in range(15):
            rows.append({"TABLE_NAME": f"TAB{i:02d}", "SCHEMA_NAME": "SAPABAP1",
                         "MEMORY_SIZE_IN_TOTAL": f"{(20 - i) * 1024 ** 3:,}",
                         "RECORD_COUNT": f"{(i * 7 % 15) * 1000000:,}"})
        return rows

    def _run(self, apply_fills=True):
        start = F.clone(S["st04_start"])
        tree = F.clone(S["st04_large"]["trees"][0])
        start["trees"] = [tree]
        sess = F.Session(start)
        large = F.clone(S["st04_large"])
        large["trees"] = [tree]
        filled = F.clone(large)
        g = next(g for g in filled["grids"] if any(c["id"] == "TABLE_NAME" for c in g["columns"]))
        g["rows"] = self._large_rows()
        sess.on("open", lambda v: v[0] == "Large Tables", go(sess, large))
        sess.on("press", "Apply Selection", go(sess, filled if apply_fills else large))
        X.SETTINGS = {"top_n": 10}
        f = X.action_st04_db(sess, Shots())
        X.SETTINGS = {}
        read_only(self, sess)
        return f, sess

    def test_hana_overview_and_large_tables(self):
        f, sess = self._run()
        self.assertEqual(f["database"], "HANA")
        self.assertEqual(f["memory"], {"used_gb": 1689.6, "limit_gb": 1884.16, "pct": 89.7})
        self.assertEqual(f["tenant_memory"]["pct"], 90.6)
        self.assertEqual(f["data_volume"]["pct"], 73.9)
        self.assertEqual(f["log_volume"]["pct"], 5.9)
        self.assertEqual(f["state"], "All services are started")
        self.assertEqual(f["alerts"], ["5 warnings with MEDIUM priority", "3 warnings with LOW priority"])
        top = f["large_tables"]["top_by_memory"]
        self.assertEqual(len(top), 10)
        self.assertEqual((top[0]["table"], top[0]["memory_gb"]), ("TAB00", 20.0))
        rec = f["large_tables"]["top_by_records"]
        self.assertEqual(rec[0]["records"], 14000000)
        self.assertTrue(all(rec[i]["records"] >= rec[i + 1]["records"] for i in range(9)))
        self.assertIn("no record sort offered", f["large_tables"]["note"])
        self.assertIn("HANA memory 89.7%", f["report_summary"])
        self.assertLessEqual(sess.cell_reads, 15 * 4 + 10)          # only the 4 columns needed, not all 33

    def test_large_tables_empty(self):
        f, _ = self._run(apply_fills=False)
        self.assertEqual(f["large_tables"]["top_by_memory"], [])


class ST05(unittest.TestCase):
    def _screen(self, state_text, sql=False, progress=True):
        sc = F.clone(S["st05"])
        F.set_obj(sc, "SQL_TRACE", selected=sql)
        F.set_obj(sc, "PROGRESS_ON", selected=progress)
        F.set_obj(sc, "PROGRESS_OFF", selected=not progress)
        F.set_obj(sc, "STACK_ON", selected=True)
        F.set_obj(sc, "TEXT", text=state_text)
        return sc

    def test_all_off(self):
        sess = F.Session(self._screen("All traces are switched off"))
        f = X.action_st05_status(sess, Shots())
        self.assertTrue(f["all_off"])
        self.assertEqual(f["traces_on"], 0)
        self.assertEqual(f["progress_display"], True)
        self.assertEqual(f["stack_trace"], True)
        self.assertEqual(f["report_summary"], "All traces are switched off; stack trace On, progress display On")
        self.assertIn(["SQL trace type", "not ticked"], f["report_rows"])
        read_only(self, sess)

    def test_trace_on(self):
        sess = F.Session(self._screen("SQL Trace switched on for user BASIS1", sql=True, progress=False))
        f = X.action_st05_status(sess, Shots())
        self.assertFalse(f["all_off"])
        self.assertEqual(f["traces_on"], 1)
        self.assertIn("1 trace(s) ON: SQL Trace switched on for user BASIS1", f["report_summary"])
        self.assertIn("progress display Off", f["report_summary"])


class STMS(unittest.TestCase):
    def _run(self, fail=None, mute=None):
        sess = F.Session(F.clone(S["stms_start"]))
        sess.on("press", "System Overview", go(sess, "stms_overview"))

        def test_conn(s, v):
            g = next(o for o in s.by_id.values() if isinstance(o, F.Grid))
            row = g.current[0]
            name = S["stms_overview"]["grids"][0]["rows"][row]["SYSNAM"]
            if name == mute:
                s.show(F.clone(S["stms_overview"]), (s.status[0], s.status[1]))
            elif name == fail:
                s.show(F.clone(S["stms_overview"]), (f"Connection to system {name} failed (RFC error)", "E"))
            else:
                s.show(F.clone(S["stms_overview"]), (f"Connection to system {name} (host h) OK", "S"))
        sess.on("press", "Connection Test", test_conn)
        f = X.action_stms_systems(sess, Shots())
        read_only(self, sess)
        return f

    def test_all_available(self):
        f = self._run()
        self.assertEqual(f["domain"], "DOMAIN_S4D")
        self.assertEqual([s["system"] for s in f["systems"]], ["M4D", "M4P", "S4D", "S4P", "S4Q"])
        self.assertTrue(all(s["connection_ok"] for s in f["systems"]))
        self.assertEqual(f["unavailable"], 0)
        self.assertEqual(f["report_summary"], "Domain DOMAIN_S4D: 5 systems, all available")
        self.assertIn(["S4D (controller)", "Active; connection OK"], f["report_rows"])

    def test_failed_and_unjudged(self):
        f = self._run(fail="S4P", mute="M4D")
        self.assertEqual(f["unavailable"], 1)
        self.assertEqual(f["unjudged"], ["M4D"])
        self.assertIn("NOT available: S4P", f["report_summary"])
        self.assertIn("connection not judged for M4D", f["report_summary"])

    def test_stale_ok_of_another_system_is_not_taken(self):
        f = self._run(mute="M4P")          # M4P's test leaves M4D's "OK" in the status bar
        m4p = next(s for s in f["systems"] if s["system"] == "M4P")
        self.assertIsNone(m4p["connection_ok"])


class STRUST(unittest.TestCase):
    def _run(self, title=None, toggle=False, stale_for=None, other_subject_for=None):
        start = F.clone(S["strust_start"])
        tree = F.clone(S["strust_pses"]["SSLSDFAULT1"]["trees"][0])
        start["trees"] = [tree]
        if title:
            start["title"] = title
        sess = F.Session(start)
        missing = F.clone(S["strust_pses"]["WSSEDFAULT"])
        shown = {"prev": None}

        def opened(s, v):
            _text, key = v
            sc = F.clone(S["strust_pses"].get(key, missing))
            sc["trees"] = [tree]
            if key == stale_for and shown["prev"]:          # F2 does not refresh: the previous details stay
                for name in ("CERT-SUBJECT", "CERT-SNUMBER", "CERT-DATETO", "CERT-DATEFROM"):
                    F.set_obj(sc, name, text=next(o["text"] for o in shown["prev"]["objects"]
                                                  if o.get("name") == name))
            if key == other_subject_for:
                F.set_obj(sc, "CERT-SUBJECT", text="CN=SOMETHING ELSE")
            shown["prev"] = sc
            st = sc.get("status") or ""
            s.show(sc, (st, "E" if st else ""))
        sess.on("open", None, opened)
        if toggle:
            def to_display(s, v):
                sc = F.clone(s.screen)
                sc["title"] = "Trust Manager: Display"
                s.show(sc)
            sess.on("press", "Display <-> Change", to_display)
        f = X.action_strust_certs(sess, Shots())
        read_only(self, sess)
        return f

    def test_own_certificates(self):
        f = self._run()
        by = {c["pse"]: c for c in f["certificates"]}
        self.assertIn("SSL server Standard / System-wide / host1_S4Q_10", by)
        c = by["SSL server Standard / System-wide / host1_S4Q_10"]
        self.assertEqual(c["valid_to"], "01.01.2038")
        self.assertEqual(c["days_left"], (date(2038, 1, 1) - date.today()).days)
        self.assertFalse(by["WS Security Standard"]["configured"])
        self.assertNotIn("valid_to", by["WS Security Standard"])          # stale details of the previous PSE ignored
        self.assertNotIn("File", " ".join(by))
        configured = [c for c in f["certificates"] if c.get("configured")]
        self.assertEqual(len(configured), 8)          # system, SNC, 2 SSL server, 2+2 SSL client
        self.assertTrue(f["report_summary"].startswith("8 own certificate(s); soonest expiry 01.01.2038"))

    def test_change_mode_is_not_touched(self):
        f = self._run(title="Trust Manager: Change")          # the switch did not work: nothing opened
        self.assertIn("not in display mode", f["report_summary"])
        self.assertEqual(f["report_rows"], [])

    def test_change_mode_switched_to_display_first(self):
        f = self._run(title="Trust Manager: Change", toggle=True)
        self.assertTrue(f["switched_to_display"])
        self.assertEqual(len([c for c in f["certificates"] if c.get("days_left") is not None]), 8)

    def test_details_of_the_previous_pse_are_not_used(self):
        f = self._run(stale_for="PROG<SNCS>1")              # SNC shows the System PSE's certificate (same subject)
        snc = next(c for c in f["certificates"] if c["pse"].startswith("SNC"))
        # F2 was tried twice; the row says it is the same certificate as the System PSE rather than
        # holding the sheet every day for a certificate that may really be shared.
        self.assertEqual(snc["note"], "same certificate (serial 0A:20:26:06:22:05:48:01) as System PSE / host1_S4Q_10")
        self.assertIn(["SNC SAPCryptolib / host1_S4Q_10",
                       f"valid to 01.01.2038 ({snc['days_left']} days left); {snc['note']}"], f["report_rows"])
        # the same certificate on the second instance of one PSE type is normal
        ssl2 = next(c for c in f["certificates"] if c["pse"].endswith("System-wide / host1_S4Q_11"))
        self.assertEqual(ssl2["valid_to"], "01.01.2038")

    def test_other_certificate_below_is_not_used(self):
        f = self._run(other_subject_for="SSLSDFAULT1")
        c = next(c for c in f["certificates"] if c["pse"].endswith("System-wide / host1_S4Q_10"))
        self.assertIsNone(c["days_left"])


class Settings(unittest.TestCase):
    def test_setting_defaults(self):
        X.SETTINGS = {}
        self.assertEqual(X.setting("top_n", 10), 10)
        X.SETTINGS = {"top_n": 5, "task_types": []}
        self.assertEqual(X.setting("top_n", 10), 5)
        self.assertEqual(X.setting("task_types", ["A"]), ["A"])
        X.SETTINGS = {}

    def test_registered(self):
        from sap_gui.tcode_actions import ACTIONS
        for a in ("st03n_workload", "smicm_services", "st02_tune", "st04_db", "st05_status",
                  "stms_systems", "strust_certs"):
            self.assertIn(a, ACTIONS)


class Grading(unittest.TestCase):
    """core/gui_checks.py: values, limits (catalogue defaults) and the sheet's status/observation."""

    _db = None

    def setUp(self):
        import tempfile
        from core import store
        self._old = store.DB_PATH
        if Grading._db is None:                       # one database for the class: ST02 keeps state across steps
            Grading._db = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        store.DB_PATH = Grading._db

    def tearDown(self):
        from core import store
        store.DB_PATH = self._old

    def _res(self, tcode, facts):
        from core.models import MetricResult, Status
        return MetricResult(name=f"sap.{tcode.lower()}.evidence", value=None, display_value="", status=Status.UNKNOWN,
                            tcode=tcode, extra_data=facts)

    def _grade(self, tcode, facts, system="TEST SYS"):
        from core import gui_checks
        r = self._res(tcode, facts)
        ms = gui_checks.metrics_for(r, system)
        return {m.name: m for m in ms}, r.extra_data

    def test_st04_from_recorded_screen(self):
        f, _ = ST04()._run.__func__(ST04())
        f["reader"] = "st04_db"
        ms, ex = self._grade("ST04", f)
        self.assertEqual(ms["sap.st04.memory_used_pct"].status.value, "WARNING")       # 89.7 %
        self.assertEqual(ms["sap.st04.tenant_memory_pct"].status.value, "CRITICAL")    # 90.6 %
        self.assertEqual(ms["sap.st04.data_volume_pct"].status.value, "NORMAL")
        self.assertEqual(ex["check_status"], "CRITICAL")
        self.assertIn("HANA tenant memory used (ST04) 90.6% (CRITICAL)", ex["check_observation"])

    def test_st02_swaps_per_day(self):
        from core import gui_checks
        base = {"reader": "st02_tune", "instance": "i1", "startup": "01.10.2026 00:00:00",
                "snapshot": "11.10.2026 00:00:00",
                "buffers": [{"buffer": "CUA", "hit_ratio": 99.5, "swaps": 10000.0},
                            {"buffer": "program", "hit_ratio": 97.0, "swaps": 0.0},
                            {"buffer": "OTR", "hit_ratio": 50.0, "swaps": 9e9}]}      # not graded by default
        ms, ex = self._grade("ST02", dict(base))
        self.assertEqual(ms["sap.st02.swaps_per_day"].value, 1000.0)       # first reading: 10,000 in 10 days
        self.assertEqual(ms["sap.st02.swaps_per_day"].status.value, "WARNING")
        self.assertEqual(ms["sap.st02.min_hit_ratio"].value, 97.0)          # 'program' matches 'Program'
        self.assertEqual(ms["sap.st02.min_hit_ratio"].status.value, "WARNING")
        nxt = dict(base, snapshot="11.10.2026 12:00:00",
                   buffers=[{"buffer": "CUA", "hit_ratio": 99.5, "swaps": 16000.0},
                            {"buffer": "program", "hit_ratio": 99.0, "swaps": 0.0}])
        ms, ex = self._grade("ST02", nxt)
        self.assertEqual(ms["sap.st02.swaps_per_day"].value, 12000.0)       # 6,000 in 12 h
        self.assertEqual(ms["sap.st02.swaps_per_day"].status.value, "CRITICAL")
        soon = dict(nxt, snapshot="11.10.2026 12:10:00",
                    buffers=[{"buffer": "CUA", "hit_ratio": 99.5, "swaps": 16010.0},
                             {"buffer": "program", "hit_ratio": 99.0, "swaps": 0.0}])
        ms, _ = self._grade("ST02", soon)
        self.assertEqual(ms["sap.st02.swaps_per_day"].value, 12000.0)       # 10 min later: last result kept
        restart = dict(nxt, startup="11.10.2026 18:00:00", snapshot="12.10.2026 18:00:00",
                       buffers=[{"buffer": "CUA", "hit_ratio": 99.5, "swaps": 50.0}])
        ms, ex = self._grade("ST02", restart)
        self.assertEqual(ms["sap.st02.swaps_per_day"].value, 50.0)          # restarted: since startup
        self.assertEqual(ms["sap.st02.swaps_per_day"].status.value, "NORMAL")
        self.assertIn("not on the screen: Program", ex["check_observation"])

    def test_smicm_unknown_is_not_normal(self):
        ms, ex = self._grade("SMICM", {"reader": "smicm_services", "inactive": 0, "unknown_state": 1,
                                       "services": [{"protocol": "HTTP", "port": "1", "active": None}]})
        self.assertEqual(ms["sap.smicm.inactive_services"].status.value, "UNKNOWN")
        self.assertEqual(ex["check_status"], "UNKNOWN")
        ms, ex = self._grade("SMICM", {"reader": "smicm_services", "inactive": 1, "unknown_state": 0,
                                       "services": [{"protocol": "HTTP", "port": "1", "active": False}]})
        self.assertEqual(ex["check_status"], "CRITICAL")
        self.assertIn("not active: HTTP 1", ex["check_observation"])

    def test_strust_st05_stms_st03n(self):
        _, ex = self._grade("STRUST", {"reader": "strust_certs", "min_days_left": 20,
                                       "soonest": {"pse": "SSL", "valid_to": "01.11.2026"}})
        self.assertEqual(ex["check_status"], "WARNING")
        _, ex = self._grade("STRUST", {"reader": "strust_certs", "min_days_left": -3, "soonest": {}})
        self.assertEqual(ex["check_status"], "CRITICAL")
        _, ex = self._grade("ST05", {"reader": "st05_status", "traces_on": 1})
        self.assertEqual(ex["check_status"], "WARNING")
        _, ex = self._grade("ST05", {"reader": "st05_status", "traces_on": 0})
        self.assertEqual((ex["check_status"], ex["check_observation"]), ("NORMAL", "All within limits"))
        _, ex = self._grade("STMS", {"reader": "stms_systems", "unavailable": 1, "systems": [{}]})
        self.assertEqual(ex["check_status"], "CRITICAL")
        ms, ex = self._grade("ST03N", {"reader": "st03n_workload", "wanted": ["DIALOG", "BACKGROUND", "WS-RFC"],
                                       "all_task_types": ["DIALOG", "BACKGROUND"],
                                       "task_types": {"DIALOG": {"avg_ms": 2784.4},
                                                      "BACKGROUND": {"avg_ms": 99999.0}}})
        self.assertEqual(ms["sap.st03n.dialog_avg_ms"].status.value, "CRITICAL")
        self.assertEqual(ms["sap.st03n.background_avg_ms"].status.value, "NORMAL")   # shown, not graded
        self.assertNotIn("sap.st03n.wsrfc_avg_ms", ms)                               # no WS-RFC steps
        self.assertEqual(ex["check_status"], "CRITICAL")

    def test_partly_read_is_never_ok(self):
        _, ex = self._grade("STMS", {"reader": "stms_systems", "unavailable": 0, "systems": [{}, {}],
                                     "unjudged": ["M4D"]})
        self.assertEqual(ex["check_status"], "UNKNOWN")
        _, ex = self._grade("STRUST", {"reader": "strust_certs", "min_days_left": 400, "soonest": {},
                                       "unreadable": ["SNC"]})
        self.assertEqual(ex["check_status"], "UNKNOWN")
        _, ex = self._grade("STRUST", {"reader": "strust_certs", "min_days_left": 3, "soonest": {},
                                       "unreadable": ["SNC"]})
        self.assertEqual(ex["check_status"], "CRITICAL")                  # what was read and is bad stands
        _, ex = self._grade("ST04", {"reader": "st04_db", "database": "not recognised"})
        self.assertEqual(ex["check_status"], "UNKNOWN")
        _, ex = self._grade("ST04", {"reader": "st04_db", "database": "SAP ASE", "report_rows": [["a", "b"]]})
        self.assertEqual(ex["check_status"], "NOT APPLICABLE")
        self.assertIn("SAP ASE", ex["check_observation"])
        _, ex = self._grade("ST05", {"reader": "st05_status", "traces_on": None})
        self.assertEqual(ex["check_status"], "UNKNOWN")
        _, ex = self._grade("SMICM", {"reader": "smicm_services", "error": "menu Goto > Services not found"})
        self.assertEqual(ex["check_status"], "UNKNOWN")

    def test_st02_counters_reset_without_restart(self):
        base = {"reader": "st02_tune", "instance": "i9", "startup": "01.10.2026 00:00:00",
                "snapshot": "11.10.2026 00:00:00", "buffers": [{"buffer": "CUA", "hit_ratio": 99.5, "swaps": 1000.0}]}
        self._grade("ST02", dict(base))
        ms, _ = self._grade("ST02", dict(base, snapshot="12.10.2026 00:00:00",
                                         buffers=[{"buffer": "CUA", "hit_ratio": 99.5, "swaps": 20.0}]))
        self.assertEqual(ms["sap.st02.swaps_per_day"].value, 1.8)     # 20 since startup over 11 days

    def test_st03n_unread_and_ungraded(self):
        ms, ex = self._grade("ST03N", {"reader": "st03n_workload", "wanted": ["DIALOG", "BACKGROUND"],
                                       "all_task_types": ["DIALOG", "BACKGROUND"],
                                       "task_types": {"BACKGROUND": {"avg_ms": 500.0}}, "report_rows": [["a", "b"]]})
        self.assertEqual(ex["check_status"], "UNKNOWN")                    # dialog on screen but not read
        self.assertIn("DIALOG figures not readable", ex["check_observation"])
        ms, ex = self._grade("ST03N", {"reader": "st03n_workload", "wanted": ["HTTP", "RFC"],
                                       "all_task_types": ["HTTP", "RFC"],
                                       "task_types": {"HTTP": {"avg_ms": 30.0}, "RFC": {"avg_ms": 700.0}},
                                       "report_rows": [["a", "b"]]})
        self.assertEqual(ex["check_status"], "NORMAL")                     # read, nothing has a limit
        self.assertIn("shown, not graded", ex["check_observation"])
        ms, ex = self._grade("ST03N", {"reader": "st03n_workload", "wanted": ["WS-RFC"],
                                       "all_task_types": ["DIALOG"], "task_types": {}, "report_rows": [["a", "b"]]})
        self.assertEqual(ex["check_status"], "NORMAL")                     # no WS-RFC steps yet today
        from sap_gui.tcode_actions_extra import _combine
        c = _combine({"i1": {"DIALOG": {"steps": 10, "avg_ms": 100.0, "cpu_ms": 10.0, "db_ms": 1.0}},
                      "i2": {"DIALOG": {"steps": 10, "avg_ms": None, "cpu_ms": 10.0, "db_ms": 1.0}}})
        self.assertIsNone(c["DIALOG"]["avg_ms"])                            # never averaged with a 0

    def test_st04_configured_hana_is_not_waved_through(self):
        from core import config_loader
        old = config_loader.get_systems
        config_loader.get_systems = lambda: [{"name": "H1", "hana": {"host": "x"}}, {"name": "A1", "db": {"type": "ase", "host": "y"}}]
        try:
            _, ex = self._grade("ST04", {"reader": "st04_db", "database": "SAP ASE"}, system="H1")
            self.assertEqual(ex["check_status"], "UNKNOWN")
            self.assertIn("HANA overview did not open", ex["check_observation"])
            _, ex = self._grade("ST04", {"reader": "st04_db", "database": "not recognised"}, system="A1")
            self.assertEqual(ex["check_status"], "NOT APPLICABLE")
            _, ex = self._grade("ST04", {"reader": "st04_db", "database": "SAP ASE", "mentions_hana": True},
                                system="ZZ")
            self.assertEqual(ex["check_status"], "UNKNOWN")
        finally:
            config_loader.get_systems = old

    def test_reader_error_is_unknown(self):
        _, ex = self._grade("STRUST", {"reader": "strust_certs", "error": "STRUST tree not found"})
        self.assertEqual(ex["check_status"], "UNKNOWN")
        self.assertIn("STRUST tree not found", ex["check_observation"])

    def test_tagged_through_register(self):
        from sap_gui.tcode_actions import ACTIONS
        sess = F.Session(F.clone(S["st02"]))
        X.SETTINGS = {"graded_buffers": ["CUA"]}
        f = ACTIONS["st02_tune"](sess, Shots())
        X.SETTINGS = {}
        self.assertEqual((f["reader"], f["settings"]), ("st02_tune", {"graded_buffers": ["CUA"]}))


if __name__ == "__main__":
    unittest.main()
