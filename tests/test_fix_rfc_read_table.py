"""
Finding 5: RFC_READ_TABLE read by OFFSET/LENGTH, 512-byte chunks, a
quote-aware OPTIONS splitter and honest row caps.
"""
import pytest

from collectors import rfc_table
from collectors.rfc_collector import SapSession


class DataBufferExceeded(Exception):
    key = "DATA_BUFFER_EXCEEDED"


class FakeReadTable:
    """Behaves like RFC_READ_TABLE: fixed-width WA, 512-byte limit, paging."""

    def __init__(self, schema, rows):
        self.schema, self.rows, self.calls = schema, rows, []

    def call(self, name, **kw):
        assert name == "RFC_READ_TABLE"
        self.calls.append(kw)
        fields = [f["FIELDNAME"] for f in kw["FIELDS"]] or list(self.schema)
        assert kw.get("DELIMITER", "") == ""
        for o in kw.get("OPTIONS", []):
            assert len(o["TEXT"]) <= 72
        layout, pos = [], 0
        for f in fields:
            layout.append({"FIELDNAME": f, "OFFSET": f"{pos:06d}", "LENGTH": f"{self.schema[f]:06d}",
                           "TYPE": "C", "FIELDTEXT": ""})
            pos += self.schema[f]
        if pos > 512 and kw.get("NO_DATA") != "X":
            raise DataBufferExceeded("Key: DATA_BUFFER_EXCEEDED")
        if kw.get("NO_DATA") == "X":
            return {"FIELDS": layout, "DATA": []}
        skip, count = int(kw.get("ROWSKIPS", 0) or 0), int(kw.get("ROWCOUNT", 0) or 0)
        chosen = self.rows[skip:skip + count] if count else self.rows[skip:]
        data = []
        for r in chosen:
            data.append({"WA": "".join(str(r.get(f, "")).ljust(self.schema[f])[:self.schema[f]]
                                        for f in fields).rstrip()})
        return {"FIELDS": layout, "DATA": data}


def _session(conn, system="RT"):
    s = SapSession(system, {})
    s.ok, s.conn = True, conn
    return s


def test_pipe_inside_a_value_does_not_shift_columns():
    conn = FakeReadTable({"GNAME": 30, "GARG": 60, "GUNAME": 12},
                         [{"GNAME": "ZLOCK", "GARG": "100|A|B", "GUNAME": "MONI"}])
    rows = _session(conn).read_table("SEQG3", ["GNAME", "GARG", "GUNAME"])
    assert [c.strip() for c in rows[0]] == ["ZLOCK", "100|A|B", "MONI"]


def test_wide_field_list_is_chunked_and_joined_by_key():
    schema = {"KEY": 10, "A": 300, "B": 300, "C": 100}
    data = [{"KEY": f"K{i}", "A": f"a{i}", "B": f"b{i}", "C": f"c{i}"} for i in range(5)]
    conn = FakeReadTable(schema, data)
    res = rfc_table.read_table(conn, "ZWIDE", ["KEY", "A", "B", "C"], "", 100, key_fields=["KEY"])
    assert res["error"] is None and res["chunks"] >= 2 and res["joined_by"] == "key"
    assert [r["B"].strip() for r in res["rows"]] == [f"b{i}" for i in range(5)]
    assert all(r["C"].strip() == f"c{i}" for i, r in enumerate(res["rows"]))
    # after the first (refused) full-width attempt, no request exceeded 512 bytes
    assert not any(sum(schema[f["FIELDNAME"]] for f in c["FIELDS"]) > 512
                   for c in conn.calls[1:] if c.get("NO_DATA") != "X")


def test_wide_field_list_without_keys_joins_by_row_index():
    schema = {"A": 300, "B": 300}
    conn = FakeReadTable(schema, [{"A": "x", "B": "y"}])
    res = rfc_table.read_table(conn, "ZWIDE", ["A", "B"])
    assert res["joined_by"] == "row_index" and res["rows"][0]["B"].strip() == "y"


def test_row_cap_is_reported_as_partial():
    conn = FakeReadTable({"N": 5}, [{"N": str(i)} for i in range(10)])
    s = _session(conn)
    rows = s.read_table("ZN", ["N"], "", 4)
    assert len(rows) == 4 and s.last_read["partial"] is True and s.last_read["row_cap"] == 4
    rows = s.read_table("ZN", ["N"], "", 10)
    assert len(rows) == 10 and s.last_read["partial"] is False


def test_paging_reads_past_one_page(monkeypatch):
    conn = FakeReadTable({"N": 5}, [{"N": str(i)} for i in range(12)])
    s = _session(conn)
    monkeypatch.setattr(SapSession, "READ_PAGE_ROWS", 5)
    rows = s.read_table("ZN", ["N"], "", 100)
    assert [r[0].strip() for r in rows] == [str(i) for i in range(12)]
    assert [c.get("ROWSKIPS", 0) for c in conn.calls] == [0, 5, 10]


def test_failed_read_returns_none_with_reason():
    class Boom:
        def call(self, name, **kw):
            raise RuntimeError("Key: TABLE_NOT_AVAILABLE")
    s = _session(Boom())
    assert s.read_table("SNAP", ["DATUM"]) is None
    assert "TABLE_NOT_AVAILABLE" in s.last_error


# ---------------------------------------------------------------- OPTIONS --

def _joined(opts):
    return " ".join(o["TEXT"] for o in opts)


def test_spaces_inside_quotes_are_preserved():
    opts = rfc_table.split_options("TEXT = 'A  B   C'   AND  X = '1'")
    assert _joined(opts) == "TEXT = 'A  B   C' AND X = '1'"


def test_quoted_literal_never_split_across_lines():
    lit = "'" + "word " * 12 + "end'"            # 66 chars with spaces inside
    where = f"FIELD_ONE = 'abc' AND LONG_FIELD_NAME = {lit} AND Z = '1'"
    opts = rfc_table.split_options(where)
    assert all(len(o["TEXT"]) <= 72 for o in opts)
    assert any(lit in o["TEXT"] for o in opts)
    for o in opts:
        assert o["TEXT"].count("'") % 2 == 0


def test_escaped_quote_inside_literal():
    opts = rfc_table.split_options("NAME = 'O''BRIEN  X' AND A = 'b'")
    assert _joined(opts) == "NAME = 'O''BRIEN  X' AND A = 'b'"


def test_lines_do_not_end_on_an_operator():
    where = " AND ".join(f"BNAME = 'USER{i:04d}'" for i in range(12))
    opts = rfc_table.split_options(where)
    assert all(len(o["TEXT"]) <= 72 for o in opts)
    assert not any(o["TEXT"].split()[-1] in ("AND", "OR") for o in opts)
    assert _joined(opts) == where


def test_session_split_where_delegates():
    assert SapSession._split_where("A = 'x  y'") == [{"TEXT": "A = 'x  y'"}]
    assert SapSession._split_where("") == []
