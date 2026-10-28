"""
Finding 7 and related clear bugs: CCMS memory units, SQLM ranking from a
capped subset, the synthetic-logon path and the extra-checks clock.
"""
import pytest

from collectors import ccms_os
from collectors import rfc_collector as rc
from collectors import rfc_perf as rp


class _Ccms:
    ok = True
    system = "CCU"

    def __init__(self, free, free_unit, total, total_unit):
        self.vals = {"F": free, "T": total}
        self.units = {"F": free_unit, "T": total_unit}

    def call(self, name, **kw):
        if name in ("BAPI_XMI_LOGON", "BAPI_XMI_LOGOFF"):
            return {}
        if name == "BAPI_SYSTEM_MON_GETLIST":
            return {"MONITOR_NAMES": [{"MS_NAME": "SAP CCMS Monitor Templates",
                                       "MONI_NAME": "Operating System"}]}
        if name == "BAPI_SYSTEM_MON_GETTREE":
            return {"TREE_NODES": [
                {"MTNAMESHRT": "Free Memory", "TID": {"MTMCNAME": "h1", "ID": "F"}},
                {"MTNAMESHRT": "Physical Mem Configured", "TID": {"MTMCNAME": "h1", "ID": "T"}}]}
        if name == "BAPI_SYSTEM_MTE_GETPERFCURVAL":
            return {"CURRENT_VALUE": {"LASTPERVAL": self.vals[kw["TID"]["ID"]]}}
        if name == "BAPI_SYSTEM_MTE_GETPERFPROP":
            return {"PROPERTIES": {"MTUNIT": self.units[kw["TID"]["ID"]]}}
        return None


def test_ccms_memory_percentage_converts_mixed_units():
    ccms_os.reset_cache()
    # 4 GB free reported in KB, 16 GB configured reported in MB -> 75 % used.
    out = ccms_os.read_os(_Ccms(4 * 1024 * 1024, "KB", 16384, "MB"), "CCU")
    assert out["memory"] == pytest.approx(75.0, abs=0.1)


def test_ccms_same_units_unchanged():
    ccms_os.reset_cache()
    out = ccms_os.read_os(_Ccms(4096, "MB", 16384, "MB"), "CCU")
    assert out["memory"] == pytest.approx(75.0, abs=0.1)


class _Sqlm:
    ok = True
    system = "SQ1"
    cfg = {}

    def __init__(self, total_rows):
        self.total_rows, self.wheres = total_rows, []
        self.last_read = {}
        self.last_error = ""

    def call(self, name, **kw):
        if name == "DDIF_FIELDINFO_GET":
            return {"DFIES_TAB": [{"FIELDNAME": f} for f in
                                  ("PROGNAME", "XCNT", "DBSUM", "DDATE")]}
        return None

    def read_table(self, table, fields, where="", rows=500):
        self.wheres.append(where)
        heavy = "DBSUM >=" in where
        data = ([["ZHEAVY", "10", "900000000", "20261020"]] if heavy else
                [[f"ZSMALL{i}", "1", "100", "20261020"] for i in range(min(self.total_rows, rows))])
        self.last_read = {"partial": (not heavy) and self.total_rows > rows}
        return data


def test_sqlm_over_the_cap_ranks_from_heavy_statements(monkeypatch):
    rp.reset_sqlm_off("SQ1")
    monkeypatch.setattr(rp, "_shape_cache", {})
    monkeypatch.setattr(rp, "SQLM_ROWS", 50)
    s = _Sqlm(500)
    out = rp.sqlm_summary(s, "SQ1")
    assert out["top"][0]["program"] == "ZHEAVY"
    assert out["filtered_min_stmt_s"] == pytest.approx(1.0)
    assert any("DBSUM >=" in w for w in s.wheres)


def test_sqlm_under_the_cap_is_not_partial(monkeypatch):
    rp.reset_sqlm_off("SQ1")
    monkeypatch.setattr(rp, "_shape_cache", {})
    out = rp.sqlm_summary(_Sqlm(5), "SQ1")
    assert out["partial"] is False and out["filtered_min_stmt_s"] is None


def test_synthetic_logon_goes_through_the_guard(monkeypatch):
    from core import synthetic
    from collectors import rfc_logon_guard as guard
    attempts = []

    class LogonError(Exception):
        key = "RFC_LOGON_FAILURE"

    class Conn:
        def __init__(self, config=None, **params):
            attempts.append(params)
            raise LogonError("Name or password is incorrect")

    monkeypatch.setattr(rc, "Connection", Conn)
    cfg = {"rfc": {"ashost": "h", "sysnr": "00", "client": "100",
                   "username": "MON", "password": "bad"}}
    for _ in range(3):
        r = synthetic.measure("SYN", cfg)
        assert not r["ok"]
    assert len(attempts) == 1
    assert guard.logon_block_status("SYN")["blocked"]


def test_extra_checks_clock_is_not_frozen(monkeypatch):
    from datetime import datetime, timedelta
    from collectors import extra_checks as xc, sap_clock as sc
    sc.learn("XC1", datetime.now() + timedelta(hours=2), "stat")
    real = sc._vm_now
    monkeypatch.setattr(sc, "_vm_now", lambda: real() + timedelta(minutes=30))
    got = xc._sap_now("XC1")
    assert abs((got - (datetime.now() + timedelta(hours=2, minutes=30))).total_seconds()) < 5


def test_ase_query_timeout_is_set(monkeypatch):
    import sys
    from collectors import ase_collector as ase

    class C:
        timeout = 0

        def close(self):
            pass

    class FakePyodbc:
        @staticmethod
        def connect(dsn, timeout=None, autocommit=None):
            return C()

    monkeypatch.setitem(sys.modules, "pyodbc", FakePyodbc)
    p = ase.build_ase_params({"db": {"type": "ase", "host": "h", "port": 1, "username": "u",
                                     "password": "p", "query_timeout": 33}})
    with ase.AseSession("AS1", p) as s:
        assert s.ok and s.conn.timeout == 33
