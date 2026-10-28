"""SMQ1 / SMQ2 on the live wall for systems without the Z function module."""
from collectors import rfc_collector as C
from collectors import rfc_live as L


class FakeSession:
    """RFC_READ_TABLE stand-in: tables -> list of row dicts; unknown column -> None (like a failed read)."""

    def __init__(self, tables, system="T1"):
        self.tables, self.system, self.last_error, self.reads = tables, system, "", []

    def read_table(self, table, fields, where="", rows=500):
        self.reads.append((table, tuple(fields)))
        if table not in self.tables:
            self.last_error = "TABLE_NOT_AVAILABLE"
            return None
        data = self.tables[table]
        if any(f not in data["cols"] for f in fields):
            self.last_error = "FIELD_NOT_VALID"
            return None
        return [[str(r.get(f, "")) for f in fields] for r in data["rows"][:rows]]


def _reset(monkeypatch=None):
    C._qrfc_fields_cache.clear()
    C._qrfc_rows_cache.clear()


def _limits(monkeypatch, warn=10, crit=50):
    monkeypatch.setattr(C, "_limits", lambda key: (warn, crit))


def test_an_error_entry_is_at_least_warning_and_nosend_is_not_counted(monkeypatch):
    _reset(); _limits(monkeypatch)
    out_rows = ([{"QNAME": "CRM_SITE_01", "QSTATE": "SYSFAIL", "DEST": "CRMPRD"}] * 3
                + [{"QNAME": "BW_DELTA", "QSTATE": "NOSEND", "DEST": ""}] * 500
                + [{"QNAME": "Q_OK", "QSTATE": "READY", "DEST": "X"}])
    s = FakeSession({"TRFCQOUT": {"cols": {"QNAME", "QSTATE", "DEST"}, "rows": out_rows},
                     "TRFCQIN": {"cols": {"QNAME", "QSTATE"}, "rows": []}})
    m = {x.name: x for x in C._qrfc_metrics(s)}
    out, inn = m["sap.smq1.entries"], m["sap.smq2.entries"]
    assert out.value == 4 and out.status.value == "WARNING"
    assert "CRM_SITE_01 SYSFAIL -> CRMPRD (3)" in out.detail and "500 in NOSEND" in out.detail
    assert inn.value == 0 and inn.status.value == "NORMAL"


def test_many_waiting_entries_follow_the_thresholds(monkeypatch):
    _reset(); _limits(monkeypatch)
    s = FakeSession({"TRFCQOUT": {"cols": {"QNAME", "QSTATE"}, "rows": [{"QNAME": "Q", "QSTATE": "READY"}] * 60},
                     "TRFCQIN": {"cols": {"QNAME", "QSTATE"}, "rows": [{"QNAME": "I", "QSTATE": "READY"}] * 12}})
    m = {x.name: x for x in C._qrfc_metrics(s)}
    assert m["sap.smq1.entries"].status.value == "CRITICAL"
    assert m["sap.smq2.entries"].status.value == "WARNING"


def test_release_without_state_column_is_graded_on_the_count(monkeypatch):
    _reset(); _limits(monkeypatch)
    s = FakeSession({"TRFCQOUT": {"cols": {"QNAME"}, "rows": [{"QNAME": "Q"}] * 7},
                     "TRFCQIN": {"cols": {"QNAME"}, "rows": []}})
    out = {x.name: x for x in C._qrfc_metrics(s)}["sap.smq1.entries"]
    assert out.status.value == "NORMAL" and out.display_value == "7 count" and "not readable" in out.detail


def test_unreadable_table_is_not_measured_and_not_probed_again(monkeypatch):
    _reset(); _limits(monkeypatch)
    s = FakeSession({"TRFCQIN": {"cols": {"QNAME", "QSTATE"}, "rows": []}})
    out = {x.name: x for x in C._qrfc_metrics(s)}["sap.smq1.entries"]
    assert out.status.value == "UNKNOWN" and out.display_value == "Not measured"
    C._qrfc_rows_cache.clear()
    n = len([r for r in s.reads if r[0] == "TRFCQOUT"])
    C._qrfc_metrics(s)
    assert len([r for r in s.reads if r[0] == "TRFCQOUT"]) == n


def test_rows_are_cached_between_polls(monkeypatch):
    _reset(); _limits(monkeypatch)
    s = FakeSession({"TRFCQOUT": {"cols": {"QNAME", "QSTATE"}, "rows": []},
                     "TRFCQIN": {"cols": {"QNAME", "QSTATE"}, "rows": []}})
    C._qrfc_metrics(s)
    n = len(s.reads)
    C._qrfc_metrics(s)
    assert len(s.reads) == n


def test_wall_shows_smq1_and_smq2(monkeypatch):
    _reset(); _limits(monkeypatch)
    s = FakeSession({"TRFCQOUT": {"cols": {"QNAME", "QSTATE"}, "rows": []},
                     "TRFCQIN": {"cols": {"QNAME", "QSTATE"}, "rows": []}})
    rows = L._checks_from_metrics(C._qrfc_metrics(s))
    assert {r["tcode"] for r in rows} == {"SMQ1", "SMQ2"}
