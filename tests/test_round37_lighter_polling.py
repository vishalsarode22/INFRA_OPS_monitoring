"""
Round 37: lighter load on the monitored systems.

* SM58: the full SYSFAIL backlog (2,688 rows on PS4) is read at most every
  30 min per system; in between only today's rows are read and merged.
* CCMS: BAPI_XMI_LOGON (a TXMILOGRAW row each time) at most every 5 min per
  system, not on every live poll.
* systems.yaml `live_refresh_seconds` lets a system be polled less often.
"""
from collectors import rfc_collector, rfc_live


class _Arfc:
    ok = True

    def __init__(self, system="PS4"):
        self.system, self.reads = system, []
        self.last_error = ""
        self.cfg = {"client": "500"}

    def read_table(self, table, fields, where="", rows=500):
        if table != "ARFCSSTATE":
            return None
        self.reads.append(where)
        today = [["20260924", "WORKFLOW_LOCAL_500"]] * 2
        if "ARFCDATUM =" in where:
            return today
        return [["20250211", "WORKFLOW_LOCAL_500"]] * 5

    def call(self, *a, **k):
        return None


def _sm58(metrics, name):
    return next(m for m in metrics if m.name == name)


def test_backlog_read_once_then_only_today():
    s = _Arfc()
    rows1, _ = rfc_collector._sm58_rows(s, "20260924", 20000)
    rows2, _ = rfc_collector._sm58_rows(s, "20260924", 20000)
    assert len(rows1) == len(rows2) == 7
    # first call: today + older backlog; second call: today only
    assert len(s.reads) == 3
    assert "ARFCDATUM = '20260924'" in s.reads[0] and "ARFCDATUM < '20260924'" in s.reads[1]
    assert "ARFCDATUM = '20260924'" in s.reads[2]
    assert all("SYSFAIL" in w and "CPICERR" in w for w in s.reads)


def test_metrics_unchanged_by_the_cache():
    s = _Arfc()
    for _ in range(2):
        ms = rfc_collector._sm58_sysfail_metrics(*rfc_collector._sm58_rows(s, "20260924", 20000)[:1],
                                                 "20260924", 20000)
        assert _sm58(ms, "sap.sm58.stuck_entries").value == 2
        assert _sm58(ms, "sap.sm58.sysfail_backlog").value == 5


def test_backlog_reread_after_ttl(monkeypatch):
    s = _Arfc()
    rfc_collector._sm58_rows(s, "20260924", 20000)
    monkeypatch.setattr(rfc_collector, "SM58_BACKLOG_TTL_SECONDS", 0)
    rfc_collector._sm58_rows(s, "20260924", 20000)
    older = [w for w in s.reads if "ARFCDATUM <" in w]
    assert len(older) == 2 and len(s.reads) == 4


def test_backlog_cache_not_reused_on_a_new_day():
    rfc_collector._sm58_cache.clear()
    s = _Arfc("R37DAY")
    rfc_collector._sm58_rows(s, "20260924", 20000)
    rfc_collector._sm58_rows(s, "20260925", 20000)      # after midnight: backlog re-read
    assert sum("ARFCDATUM <" in w for w in s.reads) == 2
    assert "ARFCDATUM < '20260925'" in s.reads[-1]


def test_unreadable_backlog_is_not_reported_as_none():
    rfc_collector._sm58_cache.clear()

    class _NoBacklog(_Arfc):
        def read_table(self, table, fields, where="", rows=500):
            if "ARFCDATUM <" in where:
                self.reads.append(where)
                return None
            return super().read_table(table, fields, where, rows)

    rows, capped = rfc_collector._sm58_rows(_NoBacklog("R37NB"), "20260924", 20000)
    assert rows is None and capped is False


def test_ccms_read_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(rfc_live._ccms, "read_os",
                        lambda session, system: calls.append(system) or {"load_5m": 1.0})
    for _ in range(5):
        assert rfc_live._ccms_read(object(), "PS4")["load_5m"] == 1.0
    assert calls == ["PS4"]


def test_per_system_poll_interval():
    import importlib, time
    try:
        app = importlib.import_module("dashboard.app")
    except Exception:          # FastAPI not installed in this environment
        return
    app._live_cache_at.pop("R37", None)
    cfg = {"name": "R37", "live_refresh_seconds": 300}
    assert app._live_poll_due(cfg)
    app._live_cache_at["R37"] = time.monotonic()
    assert not app._live_poll_due(cfg)
    assert app._live_poll_due({"name": "R37"})         # no setting: global cadence
    app._live_cache_at.pop("R37", None)


def test_sm58_entries_displayed_like_sm58():
    class S:
        system = "R37E"

        def read_table(self, table, fields, where="", rows=500):
            assert table == "ARFCSSTATE" and "ARFCDATUM = '20261016'" in where
            return ([["RECORDED", "NONE"]] * 50 + [["RECORDED", "WORKFLOW_LOCAL_900"]] * 4
                    + [["SENDED", "NONE"]] * 6 + [["SYSFAIL", "NONE"]] * 2 + [["CPICERR", "WS_SRV"]]
                    + [["VBRECORD", "NONE"]] * 3)
    m = rfc_collector._sm58_entries_today(S(), "20261016", 20000)
    assert m.name == "sap.sm58.entries_today" and m.value == 16          # 66 rows minus 50 queued qRFC
    assert m.detail.startswith("failed 3, in execution 6")
    assert "not counted: 50 qRFC entries" in m.detail
    assert m.extra_data["failed"] == 3 and m.extra_data["in_execution"] == 6

    class Empty(S):
        def read_table(self, *a, **k):
            return []
    assert rfc_collector._sm58_entries_today(Empty(), "20261016", 20000).detail == "no entries in SM58 today"

    class Refused(S):
        def read_table(self, *a, **k):
            return None
    assert rfc_collector._sm58_entries_today(Refused(), "20261016", 20000) is None


def test_read_table_puts_columns_in_the_asked_order():
    class Fake(rfc_collector.SapSession):
        def __init__(self):
            pass

        def call(self, fm, **kw):
            # S4P: the columns come back in the table's own order
            return {"FIELDS": [{"FIELDNAME": "ARFCDEST"}, {"FIELDNAME": "ARFCSTATE"}],
                    "DATA": [{"WA": "NONE|RECORDED"}, {"WA": "WS_SRV|SYSFAIL"}]}
    rows = Fake().read_table("ARFCSSTATE", ["ARFCSTATE", "ARFCDEST"], "", 10)
    assert rows == [["RECORDED", "NONE"], ["SYSFAIL", "WS_SRV"]]
