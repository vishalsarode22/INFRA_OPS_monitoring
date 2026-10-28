"""
Round 35.

* Log rotation on Windows: a failed rename (WinError 32, file open in another
  process) used to DROP the record. It is now written anyway and the rotation
  retried later; one handler per file per process.
* The FU_NOT_FOUND cache is shared between processes through a JSON file.
* NO_DATA_FOUND / NOT_FOUND are empty answers, logged at DEBUG, not WARNING.
"""
import json
import logging

from collectors.rfc_collector import SapSession
from utils import logger as L


# ------------------------------------------------------------------ logging
def test_failed_rotation_still_writes_the_record(tmp_path, monkeypatch):
    path = tmp_path / "app.log"
    path.write_text("x" * 200, encoding="utf-8")
    h = L._SafeRotatingFileHandler(str(path), maxBytes=100, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))

    def locked():
        raise PermissionError(32, "The process cannot access the file")
    monkeypatch.setattr(h, "doRollover", locked)
    errors = []
    monkeypatch.setattr(h, "handleError", lambda rec: errors.append(rec))

    rec = logging.LogRecord("t", logging.WARNING, __file__, 1, "kept-%s", ("one",), None)
    h.handle(rec)
    assert "kept-one" in path.read_text(encoding="utf-8")
    assert errors == [], "a failed rotation is not a logging error"
    assert h._retry_after > 0, "rotation is retried later, not on every record"
    assert h.stream is None, "file is closed between writes so others can rename it"


def test_rotation_works_when_the_file_is_free(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("x" * 200, encoding="utf-8")
    h = L._SafeRotatingFileHandler(str(path), maxBytes=100, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    h.handle(logging.LogRecord("t", logging.INFO, __file__, 1, "fresh", None, None))
    assert (tmp_path / "app.log.1").exists()
    assert path.read_text(encoding="utf-8").strip() == "fresh"


def test_modules_share_one_handler_per_file():
    a = L.get_logger("r35.module_a")
    b = L.get_logger("r35.module_b")
    fa = [h for h in a.handlers if isinstance(h, L._SafeRotatingFileHandler)]
    fb = [h for h in b.handlers if isinstance(h, L._SafeRotatingFileHandler)]
    assert fa and fb
    assert {id(h) for h in fa} == {id(h) for h in fb}


# ------------------------------------------------------------------ RFC cache
class _FuNotFound(Exception):
    key = "FU_NOT_FOUND"


class _NoData(Exception):
    key = "NO_DATA_FOUND"


class _Conn:
    def __init__(self, exc=None):
        self.exc, self.calls = exc, []

    def call(self, name, **kw):
        self.calls.append(name)
        if self.exc:
            raise self.exc
        return {}


def _session(system, conn):
    s = SapSession(system, {})
    s.ok, s.conn = True, conn
    return s


def test_missing_fm_is_shared_through_the_file():
    _session("R35A", _Conn(_FuNotFound("key=FU_NOT_FOUND"))).call("Z_GET_LOGON_LOAD")
    data = json.load(open(SapSession.MISSING_FM_FILE, encoding="utf-8"))
    assert "R35A|Z_GET_LOGON_LOAD" in data

    # Another process: empty memory, same file.
    SapSession._missing_fms.clear()
    SapSession._missing_mtime = None
    conn = _Conn(_FuNotFound("key=FU_NOT_FOUND"))
    assert _session("R35A", conn).call("Z_GET_LOGON_LOAD") is None
    assert conn.calls == [], "learned from the file, SAP not asked"


def test_forget_clears_the_file_too():
    _session("R35B", _Conn(_FuNotFound("key=FU_NOT_FOUND"))).call("TH_LOAD_DISTRIBUTION")
    SapSession.forget_missing_functions("R35B")
    data = json.load(open(SapSession.MISSING_FM_FILE, encoding="utf-8"))
    assert "R35B|TH_LOAD_DISTRIBUTION" not in data


def test_no_data_found_is_not_a_warning(caplog):
    s = _session("R35C", _Conn(_NoData("key=NO_DATA_FOUND, message=ID:FL 046 OTHER_FM")))
    with caplog.at_level(logging.DEBUG):
        assert s.call("/SDF/GET_DUMP_LOG") is None
    assert "NO_DATA_FOUND" in s.last_error
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING
                and "R35C" in r.getMessage()]


def test_warning_dedupes_on_sap_key_not_message_text(caplog):
    class _Other(Exception):
        key = "FIELD_NOT_VALID"
    with caplog.at_level(logging.WARNING):
        for leaked in ("V1", "V2", "V3"):
            _session("R35D", _Conn(_Other(f"key=FIELD_NOT_VALID, message={leaked}"))).call("RFC_READ_TABLE")
    assert len([r for r in caplog.records if "R35D" in r.getMessage()]) == 1
