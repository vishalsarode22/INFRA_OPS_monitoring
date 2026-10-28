"""
Finding 3: SAP server time is an OFFSET learned per system, applied to the
moving host clock -- not a frozen timestamp, not the VM's own date.
"""
from datetime import datetime, timedelta, timezone

from collectors import sap_clock as sc
from collectors import rfc_perf as rp


class _Sess:
    ok = True

    def __init__(self, system, tz_seconds=None, extra=None):
        self.system, self.tz, self.calls = system, tz_seconds, []
        self.extra = extra or {}
        self.cfg = {}
        self.last_error = ""

    def call(self, name, **kw):
        self.calls.append((name, kw))
        if name == "RFC_SYSTEM_INFO" and self.tz is not None:
            return {"RFCSI_EXPORT": {"RFCTZONE": str(self.tz), "RFCSYSID": "PRD"}}
        v = self.extra.get(name)
        return v(kw) if callable(v) else v

    def read_table(self, *a, **k):
        return []


def _expected_offset(tz_seconds):
    sap = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=tz_seconds)
    return (sap - datetime.now()).total_seconds()


def test_offset_from_rfc_system_info_and_clock_moves(monkeypatch):
    s = _Sess("CLK1", tz_seconds=19800)               # IST app server
    assert sc.ensure_offset(s, "CLK1")
    assert abs(sc.offset_seconds("CLK1") - _expected_offset(19800)) < 2
    first = sc.sap_now("CLK1")
    # Not frozen: advance the host clock, SAP "now" advances with it.
    real = sc._vm_now
    monkeypatch.setattr(sc, "_vm_now", lambda: real() + timedelta(minutes=7))
    assert 419 <= (sc.sap_now("CLK1") - first).total_seconds() <= 422


def test_offset_cached_with_ttl(monkeypatch):
    s = _Sess("CLK2", tz_seconds=0)
    sc.ensure_offset(s, "CLK2")
    sc.ensure_offset(s, "CLK2")
    assert [c for c, _ in s.calls].count("RFC_SYSTEM_INFO") == 1
    monkeypatch.setattr(sc, "OFFSET_TTL_SECONDS", 0)
    sc.ensure_offset(s, "CLK2")
    assert [c for c, _ in s.calls].count("RFC_SYSTEM_INFO") == 2


def test_unlearned_system_uses_host_clock():
    assert sc.clock_source("NOPE") == "host"
    assert abs((sc.sap_now("NOPE") - datetime.now()).total_seconds()) < 2


def test_sap_today_crosses_midnight_with_the_server():
    sc.learn("CLK3", datetime.now() + timedelta(hours=12), "server")
    assert sc.sap_today("CLK3") == (datetime.now() + timedelta(hours=12)).strftime("%Y%m%d")


def test_stat_window_uses_sap_time():
    sc.learn("CLK4", datetime.now() + timedelta(hours=3), "stat")
    s = _Sess("CLK4", extra={"SWNC_GET_STATRECS_FRAME": {"ALL_STATRECS": []}})
    rp.stat_records(s, [], "CLK4")
    kw = next(kw for n, kw in s.calls if n == "SWNC_GET_STATRECS_FRAME")
    end = datetime.strptime(kw["READ_END_DATE"] + kw["READ_END_TIME"], "%Y%m%d%H%M%S")
    assert abs((end - (datetime.now() + timedelta(hours=3))).total_seconds()) < 5


def test_st03n_days_and_sqlm_window_use_sap_date():
    sc.learn("CLK5", datetime.now() + timedelta(days=1), "stat")
    s = _Sess("CLK5")
    rp.st03n_aggregate(s, [{"name": "h_CLK5_00"}])
    days = {kw["PERIODSTRT"] for n, kw in s.calls if n == "SWNC_COLLECTOR_GET_AGGREGATES"}
    assert (datetime.now() + timedelta(days=1)).strftime("%Y%m%d") in days


def test_stat_jitter_does_not_drift_the_clock():
    sc.learn("CLK6", datetime.now() + timedelta(minutes=30), "stat")
    before = sc.offset_seconds("CLK6")
    # A STAT frame 40 s "behind" (the call took 40 s) must not pull the offset.
    end_local = sc.sap_now("CLK6") - timedelta(seconds=40)
    utc = end_local - timedelta(hours=1)
    recs = [{"STARTDATE": end_local.strftime("%Y%m%d"), "STARTTIME": end_local.strftime("%H%M%S"),
             "STARTTIMESTAMP": utc.strftime("%Y%m%d%H%M%S")}]
    rp._learn_clock("CLK6", {"ALL_STATRECS": [{"ENDTIMESTAMP": utc.strftime("%Y%m%d%H%M%S")}]}, recs)
    assert abs(sc.offset_seconds("CLK6") - before) < 1


def test_legacy_view_still_works():
    rp._sap_clock["CLK7"] = datetime(2026, 9, 7, 17, 21, 41)
    assert "CLK7" in rp._sap_clock
    assert abs((rp._sap_clock.get("CLK7") - datetime(2026, 9, 7, 17, 21, 41)).total_seconds()) < 2
    with rp._sap_clock_lock:
        rp._sap_clock.clear()
    assert "CLK7" not in rp._sap_clock


def test_snap_and_tbtco_reads_use_sap_date():
    from collectors import rfc_collector as rc
    sc.learn("CLK8", datetime.now() + timedelta(hours=14), "stat")
    sap_day = sc.sap_today("CLK8")
    wheres = []

    class S(_Sess):
        def read_table(self, table, fields, where="", rows=500):
            wheres.append((table, where))
            return []

    rc._from_standard_modules(S("CLK8"))
    snap = [w for t, w in wheres if t == "SNAP"]
    tbtco = [w for t, w in wheres if t == "TBTCO"]
    assert snap and all(sap_day in w for w in snap)
    assert tbtco and all(sap_day in w for w in tbtco)
