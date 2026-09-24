"""
Shared test setup.

Round 32: rfc_live remembers, per process, which tables and columns a system
can read (_supported_fields). Since that probe now notices a failed read, a
test whose fake session returns None for a table marks it unreadable for every
later test using the same system name. Each test starts with a clean cache.
"""
import pytest


@pytest.fixture(autouse=True)
def _fresh_rfc_capabilities(tmp_path, monkeypatch):
    try:
        from collectors import rfc_live
    except Exception:          # module not importable in this environment
        yield
        return
    from collectors.rfc_collector import SapSession
    # Round 35: the missing-FM cache is shared through a file in logs/.
    # Tests get their own file so they never touch the real one.
    monkeypatch.setattr(SapSession, "MISSING_FM_FILE", str(tmp_path / "rfc_missing_functions.json"))
    monkeypatch.setattr(SapSession, "_missing_mtime", None)
    SapSession._missing_fms.clear()
    rfc_live.reset_capabilities()
    SapSession.forget_missing_functions()      # round 34: per-test FM cache
    yield
    rfc_live.reset_capabilities()
    SapSession.forget_missing_functions()
