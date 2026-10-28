"""
Finding 1: a refused password must block EVERY further logon, in every
process, until the password changes or an operator clears the block.
"""
import os
import subprocess
import sys

import pytest

from collectors import rfc_collector as rc
from collectors import rfc_logon_guard as guard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LogonError(Exception):
    key = "RFC_LOGON_FAILURE"


class CommunicationError(Exception):
    key = "RFC_COMMUNICATION_FAILURE"


def _cfg(password="wrong"):
    return {"rfc": {"ashost": "sap-prd.example", "sysnr": "00", "client": "100",
                    "username": "MONITOR", "password": password}}


@pytest.fixture
def fake_conn(monkeypatch):
    attempts = []

    class Conn:
        behaviour = {"wrong": LogonError("Name or password is incorrect (repeat logon)")}

        def __init__(self, config=None, **params):
            attempts.append(dict(params, config=config))
            exc = Conn.behaviour.get(params.get("passwd"))
            if exc is not None:
                raise exc

        def call(self, name, **kw):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(rc, "Connection", Conn)
    monkeypatch.setattr(rc, "PYRFC_AVAILABLE", True)
    monkeypatch.setattr(rc, "_tcp_reachable", lambda *a, **k: (True, ""))
    rc.clear_cooldown()
    Conn.attempts = attempts
    return Conn


def _open(system="PRD", password="wrong"):
    with rc.SapSession(system, _cfg(password)) as s:
        return s.ok, s.error, s.logon_blocked


def test_refused_password_blocks_all_further_logons(fake_conn):
    ok, err, blocked = _open()
    assert not ok and blocked
    assert "Logon blocked after failed password" in err and "fix credentials in .env" in err
    assert len(fake_conn.attempts) == 1
    # Ten more tries (any path, any time later): no further logon attempt.
    for _ in range(10):
        ok, err, blocked = _open()
        assert not ok and blocked
    assert len(fake_conn.attempts) == 1


def test_block_does_not_expire_like_the_old_cooldown(fake_conn, monkeypatch):
    _open()
    rc.clear_cooldown()                      # the in-memory 900 s cooldown is gone
    monkeypatch.setattr(rc, "_COOLDOWN_AUTH", 0)
    _open()
    assert len(fake_conn.attempts) == 1


def test_new_password_is_a_new_key(fake_conn):
    _open(password="wrong")
    ok, err, blocked = _open(password="right")
    assert ok and not blocked and err is None
    assert len(fake_conn.attempts) == 2
    # A successful logon drops the stale block for the old password.
    assert guard.list_blocks("PRD") == []


def test_operator_clear_allows_one_new_attempt(fake_conn):
    _open()
    assert guard.logon_block_status("PRD")["blocked"] is True
    assert rc.clear_logon_block("PRD") == 1
    assert guard.logon_block_status("PRD")["blocked"] is False
    _open()
    assert len(fake_conn.attempts) == 2           # tried once more, blocked again
    assert guard.logon_block_status("PRD")["blocked"] is True


def test_block_is_per_system_target(fake_conn):
    _open("PRD")
    cfg = _cfg()
    cfg["rfc"]["ashost"] = "sap-qas.example"
    with rc.SapSession("QAS", cfg) as s:
        assert s.logon_blocked                    # refused too, separately
    assert len(fake_conn.attempts) == 2
    assert {b["system"] for b in guard.list_blocks()} == {"PRD", "QAS"}


def test_network_errors_do_not_block(fake_conn):
    fake_conn.behaviour["netfail"] = CommunicationError("partner not reached (host sap-prd)")
    ok, err, blocked = _open(password="netfail")
    assert not ok and not blocked
    assert guard.list_blocks() == []


@pytest.mark.parametrize("text", [
    "Password logon no longer possible - too many failed attempts",
    "RFC_LOGON_FAILURE: Name or password is incorrect (repeat logon)",
    "User is locked. Please notify the person responsible",
])
def test_auth_failure_texts(text):
    assert guard.is_auth_failure(RuntimeError(text))


def test_block_is_visible_to_another_process(fake_conn, tmp_path):
    """The whole point: the sweep / pool workers are separate processes."""
    _open()
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from collectors import rfc_logon_guard as g\n"
        "t = g.LogonTarget('PRD', 'sap-prd.example', '00', '100', 'MONITOR', 'wrong')\n"
        "t2 = g.LogonTarget('PRD', 'sap-prd.example', '00', '100', 'MONITOR', 'new-pw')\n"
        "print(bool(g.block_info(t)), bool(g.block_info(t2)))\n" % ROOT)
    env = dict(os.environ)
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "True False", out.stderr


def test_store_holds_no_password(fake_conn):
    _open(password="wrong")
    raw = open(guard.db_path(), "rb").read()
    assert b"wrong" not in raw


def test_cli_lists_and_clears(fake_conn, capsys):
    _open()
    assert guard._main(["--list"]) == 0
    assert "PRD" in capsys.readouterr().out
    assert guard._main(["--clear", "PRD"]) == 0
    assert guard.list_blocks() == []


def test_collect_rfc_metrics_surfaces_block_without_retry_countdown(fake_conn):
    _open()
    metrics, error = rc.collect_rfc_metrics("PRD", _cfg())
    assert metrics == [] and "Logon blocked" in error and "retrying in" not in error


def test_live_read_reports_block(fake_conn):
    from collectors import rfc_live
    _open()
    rfc_live._cache.clear()
    p = rfc_live.read_live("PRD", _cfg(), use_cache=False)
    assert p["connected"] is False and p["logon_blocked"] is True
    assert "Logon blocked" in p["error"]
    assert len(fake_conn.attempts) == 1


def test_perf_collector_goes_through_guard(fake_conn):
    from collectors import rfc_perf
    _open()
    metrics, error = rfc_perf.collect_perf_metrics("PRD", _cfg())
    assert metrics == [] and "Logon blocked" in error
    assert len(fake_conn.attempts) == 1


def test_perf_background_refresh_goes_through_guard(fake_conn, monkeypatch):
    from collectors import rfc_live
    import core.config_loader as cl
    _open()
    monkeypatch.setattr(cl, "get_systems", lambda: [dict(_cfg(), name="PRD")])
    rfc_live._perf_refreshing.add("PRD")
    rfc_live._perf_refresh_background("PRD", "100")
    assert len(fake_conn.attempts) == 1
    assert "PRD" not in rfc_live._perf_refreshing


def test_perf_background_refresh_parks_on_network_failure(fake_conn, monkeypatch):
    from collectors import rfc_live
    import core.config_loader as cl
    fake_conn.behaviour["netfail"] = CommunicationError("partner not reached")
    monkeypatch.setattr(cl, "get_systems", lambda: [dict(_cfg("netfail"), name="PRD")])
    rfc_live._perf_refresh_background("PRD", "100")
    assert rc.cooldown_remaining("PRD")[0] > 0
    rc.clear_cooldown()


def test_ase_login_goes_through_guard(monkeypatch):
    from collectors import ase_collector as ase
    calls = []

    class FakePyodbc:
        @staticmethod
        def connect(dsn, timeout=None, autocommit=None):
            calls.append(dsn)
            raise RuntimeError("[28000] Login failed. (4002) (SQLDriverConnect)")

    monkeypatch.setitem(sys.modules, "pyodbc", FakePyodbc)
    cfg = {"db": {"type": "ase", "host": "h", "port": 4901, "username": "mon",
                  "password": "bad;pw", "database": "PRD"}}
    for _ in range(3):
        metrics, err = ase.collect_ase_metrics("PRD", cfg)
        assert metrics == [] and "Logon blocked" in err
    assert len(calls) == 1
    # the ';' in the password is quoted, not a DSN terminator
    assert "PWD={bad;pw};" in calls[0]
