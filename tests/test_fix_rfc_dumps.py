"""
Finding 6: dump counting -- SEQNO '000' only, SAP date, no silent 2000 cap,
and dumps per user in a recent SAP-time window.
"""
from datetime import datetime, timedelta

from collectors import rfc_collector as rc
from collectors import sap_clock as sc


class _Snap:
    """SNAP with `n` dumps today (SEQNO 000) plus 3 follow-up rows each."""
    ok = True

    def __init__(self, n, system="DMP", log_rows=None):
        self.system, self.n, self.calls = system, n, []
        self.cfg = {"client": "100"}
        self.last_error = ""
        self.last_read = {}
        self.log_rows = log_rows

    def call(self, name, **kw):
        self.calls.append((name, kw))
        if name == "/SDF/GET_DUMP_LOG" and self.log_rows is not None:
            return {"ET_E2E_LOG": self.log_rows(kw)}
        self.last_error = f"{name}: FU_NOT_FOUND"
        return None

    def read_table(self, table, fields, where="", rows=500):
        if table != "SNAP":
            self.last_read = {"partial": False}
            return []
        head_only = "SEQNO = '000'" in where
        total = self.n if head_only else self.n * 4
        got = min(total, rows)
        self.last_read = {"partial": total > rows, "row_cap": rows}
        self.calls.append(("READ", where, rows))
        return [["101500"]] * got


def _st22(s):
    rc._dump_log_missing.discard(s.system)
    return next(m for m in rc._from_standard_modules(s) if m.name == "sap.st22.dumps")


def test_a_busy_day_is_not_capped_at_2000():
    m = _st22(_Snap(3480))
    assert m.value == 3480 and m.extra_data["partial"] is False
    assert m.display_value == "3480 count"


def test_cap_reached_is_flagged_partial(monkeypatch):
    monkeypatch.setattr(rc, "_SNAP_READ_LIMIT", 1000)
    m = _st22(_Snap(3480, system="DMP2"))
    assert m.value == 1000 and m.extra_data["partial"] is True
    assert m.display_value == "1000+ count" and "at least" in m.detail


def test_only_header_rows_are_counted():
    s = _Snap(7, system="DMP3")
    assert _st22(s).value == 7
    assert any("SEQNO = '000'" in c[1] for c in s.calls if c[0] == "READ")


def test_snap_read_uses_sap_date():
    sc.learn("DMP4", datetime.now() + timedelta(hours=10), "stat")
    s = _Snap(1, system="DMP4")
    _st22(s)
    reads = [c for c in s.calls if c[0] == "READ"]
    assert reads and sc.sap_today("DMP4") in reads[0][1]


def test_dumps_by_user_window_from_dump_log():
    sc.learn("DMP5", datetime(2026, 10, 23, 14, 30, 0), "stat")

    def rows(kw):
        assert kw["DATE_FROM"] == "20261023"
        assert kw["TIME_FROM"] <= "141500" and kw["TIME_TO"] >= "142959"
        return ([{"E2E_DATE": "20261023", "E2E_TIME": "142000", "E2E_USER": "BOB_100"}] * 11
                + [{"E2E_DATE": "20261023", "E2E_TIME": "142500", "E2E_USER": "ANN_100"}] * 2
                + [{"E2E_DATE": "20261023", "E2E_TIME": "100000", "E2E_USER": "OLD_100"}])
    s = _Snap(0, system="DMP5", log_rows=rows)
    got = rc.dumps_by_user_window("DMP5", 15, session=s)
    assert got == {"BOB": 11, "ANN": 2}


def test_dumps_by_user_window_crosses_sap_midnight_and_falls_back_to_snap():
    sc.learn("DMP6", datetime(2026, 10, 24, 0, 5, 0), "stat")
    wheres = []

    class S(_Snap):
        def read_table(self, table, fields, where="", rows=500):
            wheres.append(where)
            return [["U1"]] * 3 if "20261023" in where else [["U1"], ["U2"]]

    got = rc.dumps_by_user_window("DMP6", 15, session=S(0, system="DMP6"))
    assert got == {"U1": 4, "U2": 1}
    assert any("20261023" in w and "235959" in w for w in wheres)
    assert any("20261024" in w and "000000" in w for w in wheres)
    assert all("SEQNO = '000'" in w for w in wheres)


def test_dumps_by_user_window_unknown_is_none():
    class S(_Snap):
        def read_table(self, *a, **k):
            return None
    assert rc.dumps_by_user_window("DMP7", 15, session=S(0, system="DMP7")) is None


def test_dumps_by_user_window_without_session_uses_guarded_logon(monkeypatch):
    # No config -> unknown, and no logon attempted.
    assert rc.dumps_by_user_window("NOSUCH", 15, cfg=None) is None
