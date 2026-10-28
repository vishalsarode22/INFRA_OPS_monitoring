"""
Shared test setup.

Round 32: rfc_live remembers, per process, which tables and columns a system
can read (_supported_fields). Since that probe now notices a failed read, a
test whose fake session returns None for a table marks it unreadable for every
later test using the same system name. Each test starts with a clean cache.
"""
import os
import tempfile

# Round 36: must run before any project module is imported, because
# utils.logger picks its folder at import time. Tests log into a throwaway
# folder, never into the real logs/.
os.environ.setdefault("IBO_LOG_DIR", tempfile.mkdtemp(prefix="ibops-test-logs-"))

# The existing suite was written against JSON files, no login, one RFC
# logon per read, and inline mail. Keep those semantics for it; the new
# behaviour (SQLite store, auth, pooling, job queue) is covered by
# tests/test_services_store_auth.py, which switches each on explicitly.
os.environ.setdefault("IBO_STATE_BACKEND", "files")
os.environ.setdefault("IBO_AUTH", "0")
os.environ.setdefault("IBO_RFC_POOL", "0")
os.environ.setdefault("IBO_MAIL_ASYNC", "0")
# New reads and features added in the operations round are off for the
# existing suite (their own tests switch them on): the CCMS file-system read
# would add RFC calls to every fake session, and learned baselines would
# re-grade metrics the old tests assert on.
os.environ.setdefault("IBO_CCMS_FILESYSTEMS", "0")
os.environ.setdefault("IBO_LEARNED_MODE", "off")
# Round 2: background RFC work (extra checks, synthetic logon, change
# snapshots) runs in the calling thread under test, so fakes apply.
os.environ.setdefault("IBO_CHECK_PROCESSES", "0")
# Settings page: never pull the machine's real .env into a test run.
os.environ.setdefault("IBO_SETTINGS_SYNC", "0")
os.environ.setdefault("IBO_VPN_SERVICE_CHECK", "0")
os.environ.setdefault("IBO_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="ibops-test-db-"), "ibops.sqlite"))

# ISOLATION: the test suite never reads the machine's real .env. Before, many
# modules called load_dotenv() on import, so test results depended on the
# live settings (mail on/off, internal domains, recipients...) -- tests passed
# on the VM and failed elsewhere, and real credentials were loaded into the
# test process. Loading the project's .env is now a no-op under pytest; a test
# that needs a setting sets it itself. IBO_TEST_READ_ENV=1 restores the old
# behaviour for a deliberate check. Other .env files (temp files a test
# writes) still load normally.
import dotenv as _dotenv  # noqa: E402

_PROJECT_ENV = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
_real_load_dotenv, _real_dotenv_values = _dotenv.load_dotenv, _dotenv.dotenv_values


def _is_project_env(path, kwargs) -> bool:
    if os.environ.get("IBO_TEST_READ_ENV") == "1" or kwargs.get("stream") is not None:
        return False
    if path in (None, ""):
        return True                                  # default search finds the project .env
    try:
        return os.path.abspath(os.fspath(path)) == _PROJECT_ENV
    except TypeError:
        return False


def _test_load_dotenv(dotenv_path=None, *args, **kwargs):
    if _is_project_env(dotenv_path, kwargs):
        return False
    return _real_load_dotenv(dotenv_path, *args, **kwargs)


def _test_dotenv_values(dotenv_path=None, *args, **kwargs):
    if _is_project_env(dotenv_path, kwargs):
        return {}
    return _real_dotenv_values(dotenv_path, *args, **kwargs)


_dotenv.load_dotenv = _test_load_dotenv
_dotenv.dotenv_values = _test_dotenv_values
try:
    import dotenv.main as _dotenv_main  # noqa: E402
    _dotenv_main.load_dotenv = _test_load_dotenv
    _dotenv_main.dotenv_values = _test_dotenv_values
except Exception:  # noqa: BLE001
    pass

# SAFETY: a test run must never reach a real mail server, WhatsApp, Gemini or
# an SAP host. Project modules load the machine's real .env on import, so a
# test that forgets a mock would otherwise send a real mail with real
# credentials (to real clients). Any connection to a non-loopback address
# raises instead. IBO_TEST_ALLOW_NETWORK=1 lifts this for a deliberate check.
import socket as _socket  # noqa: E402

if os.environ.get("IBO_TEST_ALLOW_NETWORK", "0") != "1":
    _LOOPBACK = ("127.", "::1", "localhost", "0.0.0.0")

    def _is_local(address) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, (bytes, bytearray)):
            host = host.decode(errors="ignore")
        return not isinstance(host, str) or host == "" or host.startswith(_LOOPBACK)

    class TestNetworkBlocked(OSError):
        pass

    _real_connect = _socket.socket.connect
    _real_connect_ex = _socket.socket.connect_ex
    _real_create_connection = _socket.create_connection

    def _guard(address):
        if not _is_local(address):
            raise TestNetworkBlocked(f"test tried to reach {address!r}; network is blocked in tests")

    def _connect(self, address):
        if self.family in (_socket.AF_INET, _socket.AF_INET6):
            _guard(address)
        return _real_connect(self, address)

    def _connect_ex(self, address):
        if self.family in (_socket.AF_INET, _socket.AF_INET6):
            _guard(address)
        return _real_connect_ex(self, address)

    def _create_connection(address, *args, **kwargs):
        _guard(address)
        return _real_create_connection(address, *args, **kwargs)

    _socket.socket.connect = _connect
    _socket.socket.connect_ex = _connect_ex
    _socket.create_connection = _create_connection

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_rfc_capabilities(tmp_path, monkeypatch):
    # The RFC/DB logon guard persists blocks in a shared SQLite file; tests
    # must never write the real one under runtime/.
    monkeypatch.setenv("IBO_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("IBO_LOGON_GUARD_DB", str(tmp_path / "runtime" / "logon_guard.sqlite3"))
    try:
        from collectors import sap_clock
        sap_clock.forget()
    except Exception:
        pass
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
    # Round 37: per-system read caches must not leak between tests.
    from collectors import rfc_collector
    rfc_collector._sm58_cache.clear()
    rfc_live._ccms_read_cache.clear()
    rfc_live._ccms_total_cache.clear()
    rfc_live.reset_capabilities()
    SapSession.forget_missing_functions()      # round 34: per-test FM cache
    yield
    rfc_live.reset_capabilities()
    SapSession.forget_missing_functions()


# Test review 28.10.2026: some tests wrote the REAL dashboard state files
# (reach_state.json, parser_health.json, intelligence_state/) -- run on the
# monitoring VM they would overwrite live state. Point them at a temp folder.
import pytest as _pytest_state


@_pytest_state.fixture(autouse=True, scope="session")
def _state_files_in_tmp():
    tmp = tempfile.mkdtemp(prefix="ibops-test-state-")
    patched = []
    try:
        from core import reachability as _r
        patched.append((_r, "_STATE_PATH", _r._STATE_PATH))
        _r._STATE_PATH = os.path.join(tmp, "reach_state.json")
    except Exception:
        pass
    try:
        from core import parser_health as _ph
        patched.append((_ph, "STATE_PATH", _ph.STATE_PATH))
        _ph.STATE_PATH = os.path.join(tmp, "parser_health.json")
    except Exception:
        pass
    try:
        from core import intelligence_runtime as _ir
        patched.append((_ir, "BASE_DIR", _ir.BASE_DIR))
        _ir.BASE_DIR = tmp
    except Exception:
        pass
    yield
    for mod, name, val in patched:
        setattr(mod, name, val)
