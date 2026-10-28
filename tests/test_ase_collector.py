"""
Regression tests for the ASE collector. Fakes only -- no database.

The failure modes covered here are the ones the RFC collector's history
taught: a metric that reads as 0 when the source was unreadable, a ratio
that grades the wrong way round, and a first poll that fabricates a CPU
number from cumulative counters.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import ase_collector as ac
from core.models import Status


class FakeSession:
    """Routes each SQL to a canned result by matching a fragment."""

    def __init__(self, table: dict, ok: bool = True, error=None):
        self.table = table
        self.ok = ok
        self.error = error
        self.calls = []

    def query(self, sql: str):
        self.calls.append(sql)
        for frag, rows in self.table.items():
            if frag in sql:
                return rows
        return None


ACTIVE_ROW = (
    17, "SAPSR3", "SAP-WP", "SELECT", 0, 125, 900, 30, 480_000, 12_000,
    "12345", "sapprd01", "SELECT * FROM SAPSR3.VBAP WHERE ...",
)
ACTIVE_FAST = (18, "SAPSR3", "SAP-WP", "SELECT", 0, 2, 5, 0, 40, 0, "12346", "sapprd01", "SELECT 1")


def _by_key(session, system="TST"):
    return {m.name: m for m in ac.build_metrics(system, session)}


def test_unreadable_source_emits_no_metric():
    s = FakeSession({})
    assert ac.build_metrics("TST", s) == []


def test_active_and_long_running_counted_separately():
    s = FakeSession({"monProcessStatement": [ACTIVE_ROW, ACTIVE_FAST]})
    m = _by_key(s)
    assert m["db.ase.active_statements"].value == 2
    assert m["db.ase.long_running_statements"].value == 1
    long = m["db.ase.long_running_statements"]
    assert long.status == Status.WARNING
    assert "wp_pid 12345" in long.detail
    assert long.extra_data["statements"][0]["wp_pid"] == "12345"


def test_blocked_sessions_detail_names_the_blocker():
    s = FakeSession({"BlockingSPID > 0": [(22, 17, "SAPSR3", "SAP-WP", "UPDATE", 45)]})
    m = _by_key(s)
    b = m["db.ase.blocked_sessions"]
    assert b.value == 1 and b.status == Status.WARNING
    assert "on SPID 17" in b.detail


def test_expensive_statements_ranked_and_text_fetched():
    s = FakeSession({
        "monCachedStatement": [
            (901, 5000, 120_000, 300, 850, 400, "2026-09-07"),
            (902, 90_000, 10, 0, 1, 0, "2026-09-07"),
        ],
        "show_cached_text": [("SELECT MATNR FROM SAPSR3.MARA WHERE ...",)],
    })
    m = _by_key(s)
    assert m["db.ase.expensive_statement_count"].value == 1
    assert m["db.ase.top_statement_avg_lio"].value == 120_000
    assert m["db.ase.top_statement_avg_lio"].status == Status.WARNING
    top = m["db.ase.expensive_statement_count"].extra_data["statements"][0]
    assert top["ssqlid"] == 901 and "MARA" in top["sql"]
    assert top["total_lio_est"] == 5000 * 120_000


def test_cache_hit_grades_low_as_bad():
    ac._prev.clear()
    s = FakeSession({"monDataCache": [("default data cache", 1_000_000, 200_000)]})
    m = _by_key(s, system="A")
    hit = m["db.ase.data_cache_hit_pct"]
    assert hit.value == 80.0
    assert hit.status == Status.CRITICAL
    assert "cumulative" in hit.detail


def test_cache_hit_uses_delta_on_second_poll():
    ac._prev.clear()
    first = FakeSession({"monDataCache": [("default data cache", 1_000_000, 200_000)]})
    _by_key(first, system="B")
    second = FakeSession({"monDataCache": [("default data cache", 1_100_000, 201_000)]})
    m = _by_key(second, system="B")
    hit = m["db.ase.data_cache_hit_pct"]
    # 1000 physical / 100000 searches in the interval -> 99% hit
    assert hit.value == 99.0 and hit.status == Status.NORMAL
    assert "since last poll" in hit.detail


def test_engine_cpu_absent_on_first_poll_and_delta_after():
    ac._prev.clear()
    first = FakeSession({"monEngine": [(0, 5000, 5000), (1, 5000, 5000)]})
    assert "db.ase.engine_cpu_pct" not in _by_key(first, system="C")
    second = FakeSession({"monEngine": [(0, 5090, 5010), (1, 5090, 5010)]})
    m = _by_key(second, system="C")
    # 180 busy / 200 total -> 90%
    assert m["db.ase.engine_cpu_pct"].value == 90.0
    assert m["db.ase.engine_cpu_pct"].status == Status.CRITICAL


def test_memory_pools_reports_hottest_pool():
    s = FakeSession({"sp_monitorconfig": [
        ("number of open objects", 100, 900, "90.00", 950, 0),
        ("procedure cache size", 5000, 1000, "16.67", 1500, 0),
    ]})
    m = _by_key(s)
    p = m["db.ase.memory_pool_max_used_pct"]
    assert p.value == 90.0 and p.status == Status.WARNING
    assert p.detail.startswith("number of open objects 90%")


def test_build_params_requires_ase_block():
    assert ac.build_ase_params({}) is None
    assert ac.build_ase_params({"db": {"type": "hana"}}) is None
    assert ac.build_ase_params({"db": {"type": "ase", "host": "h"}}) is None
    p = ac.build_ase_params({"db": {"type": "ase", "host": "h", "port": "5000",
                                    "username": "u", "password": "p", "database": "TST"}})
    assert p["port"] == 5000 and p["database"] == "TST"


def test_collect_reports_connect_failure_not_health(monkeypatch):
    class Dead:
        def __init__(self, *a, **k):
            self.ok = False
            self.error = "ASE connect failed: boom"
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(ac, "AseSession", Dead)
    cfg = {"db": {"type": "ase", "host": "h", "port": 5000, "username": "u", "password": "p"}}
    metrics, err = ac.collect_ase_metrics("TST", cfg)
    assert metrics == [] and "connect failed" in err


def test_all_metrics_carry_st04_and_collector_tag():
    s = FakeSession({"monProcessStatement": [ACTIVE_FAST]})
    for m in ac.build_metrics("TST", s):
        assert m.tcode == "ST04"
        assert m.source == "ase_collector"
        assert m.extra_data["collector"] == "ASE_MDA"
        assert m.category == "database"
