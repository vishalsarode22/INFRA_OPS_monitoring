"""
Round 32: ABAP dumps on the live wall.

* An unreadable SNAP shows ST22 as "Not measured" with SAP's reason, instead
  of the check vanishing from the card (CARFOUR QAS and PS4, 23.09.2026).
* Dumps are counted once each (SEQNO 000), not once per SNAP row.
* SapSession.call() keeps and logs the reason a call failed.
* The field probe treats read_table() -> None as a failure.
* PRIV mode reaches the payload again for the WORK PROCESSES block.
"""
import logging

from core.models import MetricResult, Status
from collectors import rfc_collector, rfc_live
from collectors.rfc_collector import SapSession
from collectors.rfc_live import _checks_from_metrics


class _Snap:
    """A session whose SNAP answers are scripted; everything else is empty."""
    ok = True

    def __init__(self, seq0=None, any_rows=None, wide=None, fail=False):
        self.seq0, self.any_rows, self.wide, self.fail = seq0, any_rows, wide, fail
        self.calls = []
        self.last_error = ""
        self.cfg = {"client": "500"}

    def read_table(self, table, fields, where="", rows=500):
        self.calls.append((table, tuple(fields), where, rows))
        if table != "SNAP":
            return None
        if self.fail:
            self.last_error = "RFC_READ_TABLE(SNAP): ABAPApplicationError: Key: NOT_AUTHORIZED"
            return None
        if "SEQNO = '000'" in where:
            return [["x"]] * self.seq0
        if rows == 1:
            return [["x"]] if self.any_rows else []
        return self.wide

    def call(self, *a, **k):
        return None


def _st22(session):
    return next(m for m in rfc_collector._from_standard_modules(session)
                if m.name == "sap.st22.dumps")


def test_unreadable_snap_is_unknown_with_the_reason():
    m = _st22(_Snap(fail=True))
    assert m.status == Status.UNKNOWN
    assert m.value is None and m.display_value == "Not measured"
    assert "NOT_AUTHORIZED" in m.detail
    assert m.extra_data.get("read_failed") is True


def test_dumps_are_counted_once_each():
    s = _Snap(seq0=3)
    m = _st22(s)
    assert m.value == 3 and m.display_value == "3 count"
    assert any(t == "SNAP" and "SEQNO = '000'" in w for t, _f, w, _r in s.calls)


def test_quiet_day_is_zero_without_a_wide_read():
    s = _Snap(seq0=0, any_rows=False)
    assert _st22(s).value == 0
    assert not any(r > 1 and "SEQNO" not in w for t, _f, w, r in s.calls if t == "SNAP")


def test_other_seqno_scheme_falls_back_to_deduplication():
    wide = [["101500", "vhrrnps4ci", "07"], ["101500", "vhrrnps4ci", "07"],
            ["101500", "vhrrnps4ci", "07"], ["112233", "vhrrnps4ai01", "03"]]
    assert _st22(_Snap(seq0=0, any_rows=True, wide=wide)).value == 2


class _Boom:
    def call(self, name, **kw):
        raise RuntimeError("Key: FIELD_NOT_VALID")


def test_call_keeps_and_logs_the_reason(caplog):
    s = SapSession("PS4", {})
    s.ok, s.conn = True, _Boom()
    with caplog.at_level(logging.WARNING):
        assert s.read_table("SNAP_TEST_R32", ["DATUM"]) is None
        assert s.read_table("SNAP_TEST_R32", ["DATUM"]) is None
    assert "RFC_READ_TABLE(SNAP_TEST_R32)" in s.last_error
    assert "FIELD_NOT_VALID" in s.last_error
    logged = [r for r in caplog.records if "SNAP_TEST_R32" in r.getMessage()]
    assert len(logged) == 1, "the same failure is logged once, not every poll"


def test_field_probe_drops_optional_columns_the_release_lacks():
    class S:
        ok = True
        last_error = ""

        def read_table(self, table, fields, where="", rows=500):
            if "PROGX" in fields:
                self.last_error = "RFC_READ_TABLE(ZR32): ABAPApplicationError: Key: FIELD_NOT_VALID"
                return None
            return [["x"]]
    rfc_live.reset_capabilities("R32SYS")
    got = rfc_live._supported_fields(S(), "R32SYS", "ZR32", ["DATUM"], ["PROGX"])
    assert got == ["DATUM"]


def test_field_probe_does_not_remember_a_connection_failure():
    class S:
        ok = True
        last_error = ""
        n = 0

        def read_table(self, table, fields, where="", rows=500):
            S.n += 1
            if S.n == 1:
                self.last_error = "RFC_READ_TABLE(ZR32B): RFCError: RFC_COMMUNICATION_FAILURE"
                return None
            return [["x"]]
    rfc_live.reset_capabilities("R32SYS")
    assert rfc_live._supported_fields(S(), "R32SYS", "ZR32B", ["DATUM"], []) is None
    assert rfc_live._supported_fields(S(), "R32SYS", "ZR32B", ["DATUM"], []) == ["DATUM"]


def test_wall_shows_unknown_st22_with_reason_and_carries_priv():
    rows = _checks_from_metrics([
        MetricResult(name="sap.st22.dumps", value=None, display_value="Not measured",
                     status=Status.UNKNOWN, tcode="ST22",
                     detail="SNAP not readable: Key: NOT_AUTHORIZED",
                     extra_data={"collector": "RFC_TABLE", "read_failed": True}),
        MetricResult(name="sap.sm50.priv_mode_wp", value=1, display_value="1 count",
                     status=Status.WARNING, tcode="SM50"),
    ])
    by = {r["metric"]: r for r in rows}
    assert by["sap.st22.dumps"]["value"] == "Not measured"
    assert "NOT_AUTHORIZED" in by["sap.st22.dumps"]["sub"]
    assert "sap.sm50.priv_mode_wp" in by


def test_wall_counts_only_the_checks_it_draws():
    wall = open("dashboard/static/wall.html", encoding="utf-8").read()
    assert "${shown.length} checks" in wall
    assert 'const bad = shown.filter(' in wall
    assert 'new Set(["sap.sm50.priv_mode_wp"])' in wall
