"""
Round 39: wall details from the 24.09.2026 review.

* Longest running WP: a real number (WP_INDEX / WP_NO when numeric, else the
  PID) -- never "WP **" -- the user from WP_BNAME, and the long-running limit.
* Cancelled jobs from Z_GET_OBSERVABILITY_DATA (count only) get their names
  from TBTCO, read only when the count is above zero.
* Wall: long source names wrap, one-line limits, readable durations and
  sweep age.
"""
from core.models import MetricResult, Status
from collectors import rfc_live


class _WP:
    ok = True
    system = "PS4"

    def __init__(self, rows):
        self.rows = rows

    def call(self, name, **kw):
        return {"WPLIST": self.rows} if name == "TH_WPINFO" else None


def _row(**kw):
    base = {"WP_TYP": "DIA", "WP_STATUS": "Running", "WP_ELTIME": "575",
            "WP_REPORT": "SAPLMALU", "WP_BNAME": "58128", "WP_PID": "40211"}
    base.update(kw)
    return base


def test_longest_running_uses_a_real_number():
    lr = rfc_live._work_processes(_WP([_row(WP_NO="**", WP_INDEX="112")]))["longest_running"]
    assert lr["wp"] == "112" and lr["user"] == "58128"
    assert lr["limit_seconds"] == 600


def test_longest_running_falls_back_to_the_pid():
    lr = rfc_live._work_processes(_WP([_row(WP_NO="**")]))["longest_running"]
    assert lr["wp"] == "" and lr["pid"] == "40211"


class _Tbtco:
    ok = True

    def __init__(self):
        self.reads = 0

    def read_table(self, table, fields, where="", rows=500):
        self.reads += 1
        return [["ZFI_POSTING"], ["ZFI_POSTING"], ["RSBTCDEL2"]]


def _cancelled(value, detail=""):
    return MetricResult(name="sap.sm37.cancelled_jobs", value=float(value),
                        display_value=f"{value} count", status=Status.NORMAL,
                        tcode="SM37", detail=detail, extra_data={"collector": "Z_FM"})


def test_cancelled_jobs_get_names_when_the_fm_gives_none():
    m, s = _cancelled(3), _Tbtco()
    rfc_live._name_cancelled_jobs(s, [m])
    assert m.detail == "ZFI_POSTING x2, RSBTCDEL2"


def test_no_read_for_zero_or_already_named():
    s = _Tbtco()
    rfc_live._name_cancelled_jobs(s, [_cancelled(0)])
    rfc_live._name_cancelled_jobs(s, [_cancelled(2, "JOB_A, JOB_B")])
    assert s.reads == 0


def test_wall_formatting_helpers_present():
    wall = open("dashboard/static/wall.html", encoding="utf-8").read()
    assert "function fmtDuration(" in wall and "function fmtAgeMinutes(" in wall
    assert "fmtAgeMinutes(s.snapshot_age_minutes)" in wall
    assert "overflow-wrap:anywhere" in wall
    assert "long-running from" in wall
    assert "WP ${IB.esc(wp.longest_running.wp)}" not in wall
