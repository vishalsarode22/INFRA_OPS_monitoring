"""
Finding 4: SAP number parsing with an explicit decimal notation hint.
"""
import pytest

from utils.sap_numbers import parse_sap_decimal, normalize_notation


@pytest.mark.parametrize("raw,notation,expected", [
    ("1.234.567,89", "", 1234567.89),
    ("1,234,567.89", "X", 1234567.89),
    ("1 234 567,89", "Y", 1234567.89),
    ("1.234", "X", 1.234),          # English: a decimal, not 1234
    ("1.234", "", 1234.0),          # German: grouping
    ("1,234", "X", 1234.0),
    ("1,234", "", 1.234),
    ("0,09", "", 0.09),
    ("15.987", "", 15987.0),
    ("-3,5", "", -3.5),
])
def test_notation_hint(raw, notation, expected):
    assert parse_sap_decimal(raw, notation) == pytest.approx(expected)


@pytest.mark.parametrize("raw,expected", [
    ("0.125", 0.125),               # was 125
    ("0,125", 0.125),
    ("-0.250", -0.25),
    ("1.149", 1149.0),              # unchanged default (SAP GUI German)
    ("795,52", 795.52),
    ("1.234,56", 1234.56),
])
def test_heuristic_default(raw, expected):
    assert parse_sap_decimal(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "abc", None, "n/a", "1.2.3,4,5"])
def test_unparseable_is_none_not_zero(raw):
    assert parse_sap_decimal(raw) is None


def test_hint_mismatch_falls_back_instead_of_inventing_grouping():
    # "0,09" cannot be English grouping; read as the decimal it is.
    assert parse_sap_decimal("0,09", "X") == pytest.approx(0.09)


def test_normalize_notation():
    assert normalize_notation(" ") == ""
    assert normalize_notation("x") == "X"
    assert normalize_notation("Y") == "Y"
    assert normalize_notation(None) is None
    assert normalize_notation("Q") is None


def test_rfc_perf_num_handles_grouped_and_unreadable():
    from collectors import rfc_perf as rp
    assert rp._num("1.234,5", "") == pytest.approx(1234.5)     # was None -> 0
    assert rp._num(12) == 12.0
    assert rp._num("garbage") is None


def test_smon_number_uses_notation():
    from collectors import rfc_live
    assert rfc_live._smon_number("15.987", "") == 15987.0      # was 15.987 MB
    assert rfc_live._smon_number("0,09") == pytest.approx(0.09)
    assert rfc_live._smon_number("") is None


def test_tcode_metrics_number_conversion():
    from core.tcode_metrics import _to_number
    assert _to_number("1,5") == pytest.approx(1.5)             # was 15
    assert _to_number("1,234") == 1234.0
    assert _to_number("10.5") == 10.5
    assert _to_number("abc") is None


def test_rfc_user_notation_read_once_from_usr01():
    from collectors import rfc_table

    class S:
        system = "NOTN"
        cfg = {"rfc": {"ashost": "h", "username": "mon", "password": "p"}}
        calls = 0

        def read_table(self, table, fields, where="", rows=500):
            S.calls += 1
            assert table == "USR01" and "BNAME = 'MON'" in where
            return [["X"]]

    rfc_table._notation_cache.pop("NOTN", None)
    assert rfc_table.rfc_user_notation(S()) == "X"
    assert rfc_table.rfc_user_notation(S()) == "X"
    assert S.calls == 1
