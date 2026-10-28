"""
Stage 1 (09 Oct 2026): every value on the wall graded with the system's own
limits; lower-is-worse values; services (state); switching a value off;
response time = 5-min STAT average per instance; automatic RCA per instance;
live readings stored with their numbers.

Plain unittest (runs without pytest); also collected by pytest.
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

STANDARD = """# company standard
cpu:
  warning: 80
  critical: 90
memory:
  warning: 80
  critical: 90
sap.sm50.wp_utilization:
  warning: 70
  critical: 90
sap.smlg.response_time:   # SMLG
  warning: 1500
  critical: 2000
  emergency: 2500
sap.st22.dumps:
  warning: 5
  critical: 10
  inclusive: true
sap.st22.dump_count:
  warning: 5
  critical: 10
  inclusive: true
"""

SYSTEMS = ("PS4", "CEAT BW_PRD", "CEAT S4P")


class _Cfg(unittest.TestCase):
    def setUp(self):
        from core import config_loader, thresholds as T, timeline
        self.T = T
        self.tmp = tempfile.mkdtemp(prefix="ibo_lim_")
        with open(os.path.join(self.tmp, "thresholds.yaml"), "w", encoding="utf-8") as fh:
            fh.write(STANDARD)
        self._saved = [(config_loader, "CONFIG_DIR", config_loader.CONFIG_DIR),
                       (T, "_known_system", T._known_system),
                       (timeline, "add", timeline.add)]
        config_loader.CONFIG_DIR = self.tmp
        T._known_system = lambda n: n in SYSTEMS
        timeline.add = lambda *a, **k: None
        T._cache.clear()

    def tearDown(self):
        for obj, name, val in self._saved:
            setattr(obj, name, val)
        self.T._cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)


class Grading(_Cfg):
    def test_high_is_worse_with_system_override(self):
        from core import grading as g
        self.assertEqual(g.grade_number("cpu", 85, "PS4")["status"], "WARNING")
        self.T.save_override("PS4", "cpu", 90, 95)
        self.assertEqual(g.grade_number("cpu", 85, "PS4")["status"], "NORMAL")
        self.assertEqual(g.grade_number("cpu", 85, "CEAT BW_PRD")["status"], "WARNING")
        self.assertEqual(g.grade_number("cpu", 95, "PS4")["status"], "CRITICAL")
        self.assertEqual(g.grade_number("cpu", None, "PS4")["status"], "UNKNOWN")

    def test_inclusive_default_and_off(self):
        from core import grading as g
        self.assertEqual(g.grade_number("cpu", 90, "PS4")["status"], "CRITICAL")      # limit counts
        self.T.save_override("PS4", "cpu", None, 90, False)
        self.assertEqual(g.grade_number("cpu", 90, "PS4")["status"], "WARNING")
        self.T.save_override("PS4", "cpu", None, None, None, enabled=False)
        got = g.grade_number("cpu", 99, "PS4")
        self.assertEqual((got["status"], got["enabled"]), ("NORMAL", False))

    def test_lower_is_worse(self):
        from core import grading as g
        # built-in default: warning at 30 min since restart, no critical
        self.assertEqual(g.grade_number("sap.uptime_minutes", 12, "PS4")["status"], "WARNING")
        self.assertEqual(g.grade_number("sap.uptime_minutes", 600, "PS4")["status"], "NORMAL")
        self.T.save_override("PS4", "sap.strust.cert_days_left", 30, 7)
        self.assertEqual(g.grade_number("sap.strust.cert_days_left", 20, "PS4")["status"], "WARNING")
        self.assertEqual(g.grade_number("sap.strust.cert_days_left", 7, "PS4")["status"], "CRITICAL")
        with self.assertRaises(ValueError):                     # wrong way round for "low"
            self.T.save_override("PS4", "sap.strust.cert_days_left", 7, 30)
        with self.assertRaises(ValueError):                     # and for "high"
            self.T.save_override("PS4", "cpu", 95, 90)

    def test_state_severity(self):
        from core import grading as g
        self.assertEqual(g.grade_state("sap.icm.state", "Running", "PS4")["status"], "NORMAL")
        self.assertEqual(g.grade_state("sap.icm.state", "Stopped", "PS4")["status"], "CRITICAL")
        self.assertEqual(g.grade_state("sap.icm.state", "Unknown", "PS4")["status"], "UNKNOWN")
        self.T.save_override("PS4", "sap.icm.state", None, None, severity="warning")
        self.assertEqual(g.grade_state("sap.icm.state", "Stopped", "PS4")["status"], "WARNING")
        self.T.save_override("PS4", "sap.icm.state", None, None, severity="off")
        self.assertEqual(g.grade_state("sap.icm.state", "Stopped", "PS4")["status"], "NORMAL")
        self.assertEqual(g.grade_state("sap.icm.state", "Stopped", "CEAT BW_PRD")["status"], "CRITICAL")

    def test_transitional_and_reported_states(self):
        from core import grading as g
        self.assertEqual(g.grade_state("sap.icm.state", "Starting", "PS4")["status"], "WARNING")
        self.assertEqual(g.grade_state("sap.icm.state", "Yellow", "PS4", "WARNING")["status"], "WARNING")
        self.assertEqual(g.grade_state("sap.icm.state", "Gray", "PS4", "CRITICAL")["status"], "CRITICAL")

    def test_no_default_means_not_graded(self):
        from core import grading as g
        self.assertEqual(g.grade_number("sap.instance.users", 999, "PS4")["status"], "NORMAL")


class Standard(_Cfg):
    def test_line_editor_writes_new_fields_and_keeps_comments(self):
        from core import config_editor as ce
        ce.save_threshold("sap.icm.state", None, None, severity="warning")
        ce.save_threshold("cpu", 85, 95, True, enabled=False)
        ce.save_threshold("sap.slicense.days_left", 45, 10)
        with open(os.path.join(self.tmp, "thresholds.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# company standard", text)
        self.assertIn("sap.smlg.response_time:   # SMLG", text)
        self.T._cache.clear()
        self.assertEqual(self.T.spec("sap.icm.state", system="")["severity"], "warning")
        self.assertEqual(self.T.spec("cpu", system="")["enabled"], False)
        self.assertEqual(self.T.limits("cpu", system=""), (85, 95))
        self.assertEqual(self.T.limits("sap.slicense.days_left", system=""), (45, 10))
        with self.assertRaises(ValueError):
            ce.save_threshold("sap.slicense.days_left", 5, 10)

    def test_new_field_uses_block_indent(self):
        from core import config_editor as ce
        with open(os.path.join(self.tmp, "thresholds.yaml"), "a", encoding="utf-8") as fh:
            fh.write("\nsap.sm21.errors:\n    warning: 5\n    critical: 20   # SM21\n")
        ce.save_threshold("sap.sm21.errors", 5, 20, None, enabled=False)
        self.T._cache.clear()
        self.assertEqual(self.T.spec("sap.sm21.errors", system="")["enabled"], False)

    def test_sweep_engine_skips_switched_off(self):
        from evaluation.threshold_engine import evaluate_metric
        from core.models import MetricResult, Status
        m = MetricResult(name="cpu", value=99, display_value="99", status=Status.UNKNOWN)
        got = evaluate_metric(m, {"cpu": {"warning": 80, "critical": 90, "enabled": False}})
        self.assertIs(got.status, Status.NORMAL)
        pre = MetricResult(name="cpu", value=99, display_value="99", status=Status.CRITICAL)
        self.assertIs(evaluate_metric(pre, {"cpu": {"enabled": False}}).status, Status.NORMAL)
        on = MetricResult(name="cpu", value=99, display_value="99", status=Status.CRITICAL)
        self.assertIs(evaluate_metric(on, {"cpu": {"warning": 80, "critical": 90}}).status,
                      Status.CRITICAL)                         # collector grade kept when on

    def test_st22_alias_in_standard(self):
        # editing the standard of one name grades the other name too
        text = STANDARD.replace("sap.st22.dumps:\n  warning: 5\n  critical: 10\n  inclusive: true\n", "")
        with open(os.path.join(self.tmp, "thresholds.yaml"), "w", encoding="utf-8") as fh:
            fh.write(text)
        self.T._cache.clear()
        self.assertEqual(self.T.limits("sap.st22.dumps", system=""), (5, 10))

    def test_catalogue_lists_every_value_with_description(self):
        cat = self.T.catalogue()
        for key in ("cpu", "sap.smlg.response_time", "sap.icm.state", "sap.uptime_minutes",
                    "sap.wp.free.DIA", "sap.sm50.longest_running_min", "sap.strust.cert_days_left"):
            self.assertIn(key, cat)
            self.assertTrue(cat[key]["label"] and cat[key]["group"])
        self.assertEqual(cat["sap.icm.state"]["direction"], "state")
        self.assertEqual(cat["sap.icm.state"]["severity"], "critical")
        self.assertEqual(cat["sap.uptime_minutes"]["direction"], "low")
        self.assertEqual((cat["sap.sm50.longest_running_min"]["warning"],
                          cat["sap.sm50.longest_running_min"]["critical"]), (60, 90))
        self.assertNotIn("sap.st22.dump_count", cat)        # listed under its main name
        self.assertNotIn("sap.sm58.stuck_trfc", cat)        # nothing produces it


def _payload(**over):
    p = {
        "system": "PS4", "connected": True, "cpu": 85.0, "memory": 50.0,
        "smon": {"memory_trusted": True},
        "work_processes": {"available": True, "total": 20, "in_use": 15, "free": 5,
                           "utilization_pct": 75,
                           "by_type": {"DIA": {"total": 10, "in_use": 9, "free": 1},
                                       "BGD": {"total": 10, "in_use": 6, "free": 4}},
                           "longest_running": {"seconds": 45 * 60, "wp": "3"}},
        "uptime_seconds": 600, "uptime_bounded": False,
        "dispatcher": {"label": "Running", "status": "NORMAL"},
        "icm": {"label": "Stopped", "status": "CRITICAL"},
        "gateway": {"label": "Unknown", "status": "UNKNOWN"},
        "instances": [
            {"name": "app1_PS4_00", "response_ms": 2100, "response_source": "STAT · 5min",
             "response_statistic": "average", "dialog_steps": 80, "users": 40},
            {"name": "app2_PS4_01", "response_ms": 3000, "response_source": "STAT · 5min",
             "response_statistic": "average", "response_low_confidence": True, "users": 2},
            {"name": "app3_PS4_02", "response_ms": None, "response_source": "idle"},
        ],
        "checks": [{"metric": "sap.st22.dumps", "label": "ABAP dumps", "value": "7 count",
                    "status": "WARNING", "warning": 5, "critical": 10, "tcode": "ST22"}],
    }
    p.update(over)
    return p


class WallGrades(_Cfg):
    def test_every_tile_graded_with_system_limits(self):
        from core.wall_grades import grade_live_payload
        out = grade_live_payload(_payload())
        g = out["grades"]
        self.assertEqual(g["cpu"]["status"], "WARNING")
        self.assertEqual(g["memory"]["status"], "NORMAL")
        self.assertEqual(g["wp"]["status"], "WARNING")                 # 75 % vs 70/90
        self.assertEqual(g["longest"]["status"], "NORMAL")             # 45 min vs 60/90
        self.assertEqual(g["uptime"]["status"], "WARNING")             # 10 min since restart
        self.assertEqual(g["icm"]["status"], "CRITICAL")
        self.assertEqual(g["gateway"]["status"], "UNKNOWN")
        self.assertNotIn("free_wp", g)                                 # no limit set: not graded
        inst = out["instances"]
        self.assertEqual(inst[0]["response_grade"]["status"], "CRITICAL")
        self.assertEqual(inst[1]["response_grade"]["status"], "NORMAL")    # too few steps
        self.assertEqual(inst[2]["response_grade"]["status"], "UNKNOWN")   # idle
        self.assertEqual(out["overall"], "CRITICAL")
        self.assertEqual(out["grade_reasons"][0]["status"], "CRITICAL")
        texts = " | ".join(r["text"] for r in out["grade_reasons"])
        self.assertIn("ICM Stopped", texts)
        self.assertIn("Response app1_PS4_00 2100 ms", texts)
        self.assertIn("ABAP dumps 7", texts)

    def test_system_own_limits_change_the_card(self):
        from core.wall_grades import grade_live_payload
        T = self.T
        T.save_override("PS4", "cpu", 90, 95)
        T.save_override("PS4", "sap.smlg.response_time", 2500, 3000)
        T.save_override("PS4", "sap.icm.state", None, None, severity="off")
        T.save_override("PS4", "sap.sm50.wp_utilization", 80, 95)
        T.save_override("PS4", "sap.sm50.longest_running_min", 60, 180)
        T.save_override("PS4", "sap.uptime_minutes", 5, None)
        T.save_override("PS4", "sap.wp.free.DIA", 2, 1)
        out = grade_live_payload(_payload(checks=[]))
        g = out["grades"]
        self.assertEqual([g[k]["status"] for k in ("cpu", "wp", "longest", "uptime", "icm")],
                         ["NORMAL"] * 5)
        self.assertEqual(g["free_wp"]["DIA"]["status"], "CRITICAL")    # 1 free, crit at <=1
        self.assertEqual(out["instances"][0]["response_grade"]["status"], "NORMAL")
        self.assertEqual(out["overall"], "CRITICAL")
        # the same payload on another system keeps the standard
        other = grade_live_payload(_payload(system="CEAT BW_PRD", checks=[]))
        self.assertEqual(other["grades"]["cpu"]["status"], "WARNING")

    def test_stale_sweep_state_shown_not_counted(self):
        from core.wall_grades import grade_live_payload
        out = grade_live_payload(_payload(
            checks=[], cpu=10.0, uptime_seconds=99999,
            work_processes={"available": False},
            instances=[],
            icm={"label": "Running", "status": "NORMAL"},
            gateway={"label": "Stopped", "status": "CRITICAL", "source": "sapcontrol",
                     "age_minutes": 240}))
        self.assertEqual(out["grades"]["gateway"]["status"], "CRITICAL")
        self.assertTrue(out["grades"]["gateway"]["stale"])
        self.assertEqual(out["overall"], "NORMAL")
        self.assertEqual(out["grade_reasons"], [])

    def test_untrusted_memory_and_bounded_uptime_never_colour(self):
        from core.wall_grades import grade_live_payload
        out = grade_live_payload(_payload(memory=99.0, smon={"memory_basis": "used incl. cache"},
                                          uptime_bounded=True))
        self.assertEqual(out["grades"]["memory"]["status"], "UNKNOWN")
        self.assertEqual(out["grades"]["uptime"]["status"], "NORMAL")

    def test_not_connected_is_unknown_and_input_untouched(self):
        from core.wall_grades import grade_live_payload
        p = _payload(connected=False)
        out = grade_live_payload(p)
        self.assertEqual(out["overall"], "UNKNOWN")
        self.assertNotIn("response_grade", p["instances"][0])          # caller's dict not changed
        self.assertEqual(grade_live_payload(None), None)


class LiveHistory(_Cfg):
    def test_checks_stored_with_numbers_and_new_values(self):
        from core.live_metrics import metrics_from_live
        ms = {m.name: m for m in metrics_from_live(_payload())}
        self.assertEqual(ms["sap.st22.dumps"].value, 7)               # was None before the fix
        self.assertEqual(ms["sap.st22.dumps"].status.value, "WARNING")
        self.assertEqual(ms["cpu"].status.value, "WARNING")
        self.assertEqual(ms["sap.sm50.wp_utilization"].value, 75)
        self.assertEqual(ms["sap.sm50.longest_running_min"].value, 45)
        self.assertEqual(ms["sap.uptime_minutes"].value, 10)
        self.assertEqual(ms["sap.smlg.response_time"].value, 3000)    # worst instance stored
        self.assertEqual(ms["sap.icm.state"].status.value, "CRITICAL")


class ExtraChecks(_Cfg):
    def test_set_limit_overrides_check_rule(self):
        from collectors import extra_checks as xc
        from core.models import MetricResult, Status
        m = MetricResult(name="sap.strust.cert_days_left", value=20, display_value="20",
                         status=Status.WARNING, threshold_warning=30, threshold_critical=7)
        self.assertIs(xc._apply_limits("PS4", m).status, Status.WARNING)   # no limit: own rule
        self.T.save_override("PS4", "sap.strust.cert_days_left", 15, 5)
        got = xc._apply_limits("PS4", m)
        self.assertIs(got.status, Status.NORMAL)
        self.assertIs(m.status, Status.WARNING)                            # original untouched
        # only a warning set: the check's own 7-day critical still applies
        self.T.save_override("CEAT BW_PRD", "sap.strust.cert_days_left", 45, None)
        five = MetricResult(name="sap.strust.cert_days_left", value=5, display_value="5",
                            status=Status.CRITICAL, threshold_warning=30, threshold_critical=7)
        got = xc._apply_limits("CEAT BW_PRD", five)
        self.assertIs(got.status, Status.CRITICAL)
        self.assertEqual((got.threshold_warning, got.threshold_critical), (45, 7))
        self.T.save_override("PS4", "sap.sm59.failing_destinations", None, None, enabled=False)
        f = MetricResult(name="sap.sm59.failing_destinations", value=3, display_value="3",
                         status=Status.WARNING)
        self.assertIs(xc._apply_limits("PS4", f).status, Status.NORMAL)


class RcaPerInstance(_Cfg):
    def _trigger(self):
        from core import rca_trigger as rt
        self._path = rt.RCA_STATE_PATH
        from pathlib import Path
        rt.RCA_STATE_PATH = Path(self.tmp) / "rca_last_fired.json"
        self.clock = [1000.0]
        cfg = rt.TriggerConfig.from_yaml({"enabled": True, "trigger": {"automatic": True, 
            "mode": "instance", "metric": "sap.smlg.response_time", "threshold_ms": 2000,
            "sustain_polls": 1, "cooldown_minutes": 60}})
        return rt, rt.RcaTrigger(cfg, clock=lambda: self.clock[0])

    def tearDown(self):
        from core import rca_trigger as rt
        if hasattr(self, "_path"):
            rt.RCA_STATE_PATH = self._path
        super().tearDown()

    def test_one_reading_fires_once_naming_instance_then_cooldown(self):
        rt, trig = self._trigger()
        d = trig.observe("PS4", _payload())
        self.assertTrue(d.fire)
        self.assertIn("app1_PS4_00 2100 ms", d.reason)
        self.assertNotIn("app2", d.reason)                                 # low confidence
        self.clock[0] += 30 * 60
        self.assertFalse(trig.observe("PS4", _payload()).fire)            # within the hour
        self.clock[0] += 31 * 60
        self.assertTrue(trig.observe("PS4", _payload()).fire)

    def test_below_limit_median_or_smlg_never_fires(self):
        rt, trig = self._trigger()
        p = _payload()
        p["instances"][0]["response_ms"] = 1999
        self.assertFalse(trig.observe("PS4", p).fire)
        p["instances"][0].update(response_ms=5000, response_statistic="median")
        self.assertFalse(trig.observe("PS4", p).fire)
        p["instances"][0].update(response_statistic="average", response_source="SMLG")
        self.assertFalse(trig.observe("PS4", p).fire)

    def test_system_limit_and_switch_off(self):
        rt, trig = self._trigger()
        self.T.save_override("PS4", "sap.smlg.response_time", 2500, 3000)
        self.assertFalse(trig.observe("PS4", _payload()).fire)            # 2100 < 3000
        self.assertTrue(trig.observe("CEAT BW_PRD", _payload(system="CEAT BW_PRD")).fire)
        self.T.save_override("PS4", "sap.smlg.response_time", None, None, enabled=False)
        p = _payload()
        p["instances"][0]["response_ms"] = 99999
        self.assertFalse(trig.observe("PS4", p).fire)


if __name__ == "__main__":
    unittest.main()
