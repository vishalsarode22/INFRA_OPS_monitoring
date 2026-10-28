"""
CCMS (RZ20) OS reader and the live-tile fallbacks it feeds. Fakes only.

Covers: XMI logon failure is reported not guessed; node discovery by
substring across hosts; worst-host aggregation; memory derived only when
free <= total; tree cache; logoff always runs; and in read_live -- CCMS
fills only tiles SMON left empty, users come from TH_USER_LIST, server
time from the learned SAP clock, instance response from the perf read.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import ccms_os as cc
from collectors import rfc_live as rl
from collectors import rfc_perf as rp
from core.models import MetricResult, Status


def _node(name, host, uid):
    return {"MTNAMESHRT": name, "TID": {"MTSYSID": "QAS", "MTMCNAME": host, "MTUID": uid}}


class FakeSession:
    ok = True
    error = None

    def __init__(self, tree=None, values=None, logon_ok=True, monitors=None):
        self.tree = tree or []
        self.values = values or {}          # MTUID -> LASTPERVAL
        self.logon_ok = logon_ok
        self.monitors = monitors
        self.calls = []

    def call(self, name, **kw):
        self.calls.append(name)
        if name == "BAPI_XMI_LOGON":
            return {"RETURN": {"TYPE": "S" if self.logon_ok else "E", "ID": "XM", "NUMBER": "004",
                               "MESSAGE": "" if self.logon_ok else "No authorization for XAL"}}
        if name == "BAPI_XMI_LOGOFF":
            return {}
        if name == "BAPI_SYSTEM_MON_GETLIST":
            return {"MONITOR_NAMES": self.monitors} if self.monitors is not None else None
        if name == "BAPI_SYSTEM_MON_GETTREE":
            return {"TREE_NODES": self.tree, "RETURN": []}
        if name == "BAPI_SYSTEM_MTE_GETPERFCURVAL":
            v = self.values.get(kw["TID"]["MTUID"])
            return {"CURRENT_VALUE": {"LASTPERVAL": v}} if v is not None else {}
        return None


def setup_function(_):
    cc.reset_cache()


def test_logon_failure_is_reported_not_guessed():
    s = FakeSession(logon_ok=False)
    out = cc.read_os(s, "QAS")
    assert out["cpu"] is None and out["memory"] is None
    assert "No authorization" in out["error"]
    assert "BAPI_SYSTEM_MON_GETTREE" not in s.calls


def test_logon_not_callable_names_the_likely_cause():
    class Dead(FakeSession):
        def call(self, name, **kw):
            return None
    out = cc.read_os(Dead(), "QAS")
    assert "S_XMI_PROD" in out["error"]


def test_role_matching_is_release_tolerant():
    assert cc._role_for("CPU_Utilization") == "cpu"
    assert cc._role_for("CPU Utilization") == "cpu"
    assert cc._role_for("5minLoadAverage") == "load_5m"
    assert cc._role_for("1min Load Average") == "load_1m"
    assert cc._role_for("Physical Memory Free") == "mem_free"
    assert cc._role_for("Physical Memory Configured") == "mem_total"
    assert cc._role_for("Free Memory") == "mem_free"
    assert cc._role_for("Number of CPUs") is None
    assert cc._role_for("Page In") is None


def test_discovery_and_worst_host_aggregation():
    tree = [
        _node("CPU_Utilization", "qas01", 1), _node("5minLoadAverage", "qas01", 2),
        _node("Physical Memory Free", "qas01", 3), _node("Physical Memory Configured", "qas01", 4),
        _node("CPU_Utilization", "qas02", 5), _node("5minLoadAverage", "qas02", 6),
        _node("Physical Memory Free", "qas02", 7), _node("Physical Memory Configured", "qas02", 8),
        _node("Number of CPUs", "qas01", 9),
    ]
    values = {1: 12, 2: 0.4, 3: 4000, 4: 16000, 5: 63, 6: 2.1, 7: 1000, 8: 16000}
    out = cc.read_os(FakeSession(tree, values), "QAS")

    assert out["error"] is None
    assert out["cpu"] == 63                    # worst host, not the mean
    assert out["memory"] == 93.8               # 1 - 1000/16000 on qas02
    assert out["load_5m"] == 2.1 and out["load_1m"] is None
    assert out["per_host"]["qas01"]["memory"] == 75.0
    assert out["per_host"]["qas01"]["cpu"] == 12


def test_memory_not_derived_when_free_exceeds_total():
    tree = [_node("Physical Memory Free", "h", 1), _node("Physical Memory Configured", "h", 2)]
    out = cc.read_os(FakeSession(tree, {1: 9000, 2: 8000}), "QAS")
    assert out["memory"] is None
    assert out["per_host"]["h"]["mem_free"] == 9000     # raw kept for the probe


def test_cpu_derived_from_idle_when_no_utilization_node():
    tree = [_node("CPU Idle", "h", 1)]
    out = cc.read_os(FakeSession(tree, {1: 88.5}), "QAS")
    assert out["cpu"] == 11.5


def test_no_matching_nodes_reports_count_and_returns_names():
    tree = [_node("Filesystem /usr", "h", 1), _node("Page Out", "h", 2)]
    out = cc.read_os(FakeSession(tree), "QAS")
    assert out["cpu"] is None
    assert "2 nodes" in out["error"]
    assert out["seen"] == ["Filesystem /usr", "Page Out"]


def test_tree_is_cached_and_logoff_always_runs():
    tree = [_node("CPU_Utilization", "h", 1)]
    s = FakeSession(tree, {1: 5})
    cc.read_os(s, "QAS")
    cc.read_os(s, "QAS")
    assert s.calls.count("BAPI_SYSTEM_MON_GETTREE") == 1
    assert s.calls.count("BAPI_SYSTEM_MTE_GETPERFCURVAL") == 2
    assert s.calls.count("BAPI_XMI_LOGOFF") == 2


def test_prefers_template_operating_system_monitor():
    s = FakeSession(monitors=[
        {"MS_NAME": "Custom", "MONI_NAME": "Operating System"},
        {"MS_NAME": "SAP CCMS Monitor Templates", "MONI_NAME": "Operating System"},
        {"MS_NAME": "SAP CCMS Monitor Templates", "MONI_NAME": "Database"},
    ])
    assert cc._os_monitor_name(s) == ("SAP CCMS Monitor Templates", "Operating System")


# ---------------------------------------------------------------------------
# read_live fallbacks
# ---------------------------------------------------------------------------

def _m(name, value, tcode=None, extra=None):
    return MetricResult(name=name, value=value, display_value=str(value), status=Status.NORMAL,
                        tcode=tcode, extra_data=extra or {})


def _wire(monkeypatch, *, smon, ccms, checks, perf, learned_clock=None, instances=()):
    class Session:
        ok, error = True, None
        def __init__(self, system, cfg): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def call(self, name, **kw): return None

    monkeypatch.setattr(rl, "SapSession", Session)
    monkeypatch.setattr(rl, "cooldown_remaining", lambda s: (0, None))
    monkeypatch.setattr(rl, "_from_function_module", lambda s: checks)
    monkeypatch.setattr(rl, "_from_standard_modules", lambda s: [])
    monkeypatch.setattr(rl, "_perf_build_metrics", lambda sy, se, c: perf)
    monkeypatch.setattr(rl, "_smon", lambda *a: smon)
    monkeypatch.setattr(rl._ccms, "read_os", lambda se, sy: ccms)
    for name in ("_logon_groups", "_jobs", "_locked_users"):
        monkeypatch.setattr(rl, name, lambda *a, **k: {})
    monkeypatch.setattr(rl, "_icm", lambda s: {})
    monkeypatch.setattr(rl, "_work_processes", lambda s: {"available": False})
    monkeypatch.setattr(rl, "_instances", lambda s: [dict(i) for i in instances])
    rl._cache.clear()
    rl.reset_perf_cache()
    with rp._sap_clock_lock:
        rp._sap_clock.clear()
        if learned_clock:
            rp._sap_clock["QAS"] = learned_clock


CCMS_OK = {"cpu": 9.0, "memory": 41.0, "load_1m": None, "load_5m": 0.3,
           "per_host": {"qassrv": {"cpu": 9.0}}, "source": "RFC · CCMS RZ20", "error": None}


def test_ccms_fills_only_tiles_smon_left_empty(monkeypatch):
    _wire(monkeypatch, smon={"cpu": 55.0}, ccms=CCMS_OK, checks=[], perf=[])
    p = rl.read_live("QAS", {"rfc": {"client": "100"}}, use_cache=False)
    assert p["cpu"] == 55.0                         # SMON wins
    assert p["memory"] == 41.0                      # CCMS fills the gap
    assert p["load_1m"] is None and p["load_5m"] == 0.3
    assert p["fallback_sources"]["memory"]["source"] == "RFC · CCMS RZ20"
    assert "cpu" not in p["fallback_sources"]
    assert p["fallback_sources"]["load_5m"]["source"] == "RFC · CCMS RZ20"


def test_ccms_not_consulted_when_smon_has_everything(monkeypatch):
    called = []
    _wire(monkeypatch, smon={"cpu": 1.0, "memory": 2.0, "load_1m": 0.1}, ccms=None, checks=[], perf=[])
    monkeypatch.setattr(rl._ccms, "read_os", lambda se, sy: called.append(1) or CCMS_OK)
    rl.read_live("QAS", {"rfc": {"client": "100"}}, use_cache=False)
    assert called == []


def test_ccms_error_is_surfaced_not_fatal(monkeypatch):
    _wire(monkeypatch, smon={}, ccms={"error": "XMI logon: no auth", "cpu": None},
          checks=[], perf=[])
    p = rl.read_live("QAS", {"rfc": {"client": "100"}}, use_cache=False)
    assert p["connected"] and p["cpu"] is None
    assert p["ccms"]["error"] == "XMI logon: no auth"


def test_users_come_from_th_user_list_count(monkeypatch):
    _wire(monkeypatch, smon={}, ccms=CCMS_OK,
          checks=[_m("sap.al08.user_logons", 0, "AL08")], perf=[])
    p = rl.read_live("QAS", {"rfc": {"client": "100"}}, use_cache=False)
    assert p["users"] == 0                          # 0 is a reading on an idle QAS
    assert p["fallback_sources"]["users"]["source"] == "RFC · TH_USER_LIST"


def test_server_time_only_when_sap_clock_was_learned(monkeypatch):
    _wire(monkeypatch, smon={}, ccms=CCMS_OK, checks=[], perf=[])
    p = rl.read_live("QAS", {"rfc": {"client": "100"}}, use_cache=False)
    assert p.get("server_time") is None             # never the host clock

    _wire(monkeypatch, smon={}, ccms=CCMS_OK, checks=[], perf=[],
          learned_clock=datetime(2026, 9, 8, 17, 24, 41))
    p = rl.read_live("QAS", {"rfc": {"client": "100"}}, use_cache=False)
    assert p["server_time"] == "17:24:41"
    assert p["server_time_source"] == "RFC · STAT timestamps"


def test_instance_response_from_perf_or_idle_note(monkeypatch):
    inst = [{"name": "qassrv_QAS_00", "host": "qassrv"}, {"name": "qassrv_QAS_01", "host": "qassrv"}]
    perf = [_m("sap.st03.dialog_resp_ms", 307, "ST03",
               extra={"collector": "RFC_PERF", "per_instance": {"qassrv_QAS_00": 307},
                      "window": "last 2 min"})]
    _wire(monkeypatch, smon={}, ccms=CCMS_OK, checks=[], perf=perf, instances=inst)
    p = rl.read_live("QAS", {"rfc": {"client": "100"}}, use_cache=False)
    i0, i1 = p["instances"]
    assert i0["response_ms"] == 307 and i0["response_source"] == "STAT · last 2 min"
    assert i1["response_ms"] is None and "idle" in i1["response_note"]

    # perf read failed entirely -> no idle claim, plain "not measured"
    _wire(monkeypatch, smon={}, ccms=CCMS_OK, checks=[], perf=[], instances=inst[:1])
    monkeypatch.setattr(rl, "_perf_build_metrics", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    p = rl.read_live("QAS", {"rfc": {"client": "100"}}, use_cache=False)
    assert p["instances"][0]["response_ms"] is None
    assert "response_note" not in p["instances"][0]
