"""
Round 29: the live wall's memory figure is graded only when it excludes
reclaimable page cache. CENTOR_QAS (100%), PRD (96%) and CARFOUR QAS (88%)
were painted red on 23.09.2026 because /SDF/SMON returned no
FREE_MEM_MB_INC_FS on those systems, so the fallback counted cache as used.
"""
from datetime import datetime

import collectors.rfc_live as live


class _Session:
    """Answers one /SDF/SMON_HEADER read with the rows given."""

    def __init__(self, rows, fields):
        self.rows, self.fields = rows, fields
        self.cfg = {"client": "100"}

    def read_table(self, table, fields, where, rows=500):
        return [[row.get(f, "") for f in fields] for row in self.rows]


def _smon(monkeypatch, row, fields):
    monkeypatch.setattr(live, "_supported_fields", lambda *a, **k: fields)
    today = datetime.now().strftime("%Y%m%d")
    row = {**row, "DATUM": today, "TIME": "143122", "SERVER": "qassrv_QAS_00"}
    return live._smon(_Session([row], fields), "CENTOR_QAS")


_FIELDS = live._SMON_FIELDS


def test_cache_inclusive_memory_is_reported_but_marked_untrusted(monkeypatch):
    # No FREE_MEM_MB_INC_FS: only "free RAM excluding cache" is known.
    out = _smon(monkeypatch, {"IDLE_TOTAL": "77", "FREE_MEM_PERC": "0", "FREE_MEM_MB": "900",
                              "FREE_MEM_MB_INC_FS": "", "AVAILCPUS": "8"}, _FIELDS)
    assert out["memory"] == 100.0
    assert out["memory_basis"] == "incl. cache as used"
    assert out["memory_trusted"] is False


def test_the_free_including_cache_figure_is_published_for_the_tile(monkeypatch):
    # SMON does not export total RAM, so no cache-inclusive PERCENTAGE can be
    # derived from it -- the wall shows the free MB instead of a bare 100%.
    out = _smon(monkeypatch, {"IDLE_TOTAL": "77", "FREE_MEM_PERC": "5", "FREE_MEM_MB": "819",
                              "FREE_MEM_MB_INC_FS": "4096", "AVAILCPUS": "8"}, _FIELDS)
    assert out["total_mem_mb"] is None and out["memory_incl_cache"] is None
    assert out["free_mem_mb_inc_fs"] == 4096
    assert out["memory_trusted"] is False
    wall = open("dashboard/static/wall.html", encoding="utf-8").read()
    assert "free incl. cache" in wall


def test_the_wall_only_grades_a_trusted_figure():
    wall = open("dashboard/static/wall.html", encoding="utf-8").read()
    assert "function gradeMemory(s)" in wall
    assert "gradeCpu(s.cpu), gradeMemory(s)," in wall, "the card's status uses the guarded grade"
    assert wall.count("gradeCpu(s.memory)") == 1, "memory is graded only inside gradeMemory"
