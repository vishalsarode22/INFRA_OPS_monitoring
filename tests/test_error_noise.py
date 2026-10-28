"""Errors page noise: logon polling is not an error. (Worker-crash noise:
tests/test_fix_rfc_workers_per_system.py.)"""
import logging

import pytest


def test_quiet_scripting_poll_logs_debug_not_error(monkeypatch):
    from sap_gui import scripting_connection as S
    seen = []
    monkeypatch.setattr(S, "_engine", lambda: (_ for _ in ()).throw(RuntimeError("no engine")))
    monkeypatch.setattr(S.log, "error", lambda m, *a, **k: seen.append(("error", m)))
    monkeypatch.setattr(S.log, "debug", lambda m, *a, **k: seen.append(("debug", m)))
    for quiet, level in ((True, "debug"), (False, "error")):
        seen.clear()
        with pytest.raises(RuntimeError):
            S.get_scripting_session(quiet=quiet)
        assert seen and seen[-1][0] == level
