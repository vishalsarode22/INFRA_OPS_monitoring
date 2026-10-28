"""
Security fixes: RFC pool timeouts/recovery (7), no exception text in
responses (9), report AI-summary cache (10), repository hygiene (11, 12),
loopback-by-default binding (6), escaping in the UI (5).
"""
import os
import re
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ------------------------------------------------------------- 7 RFC pool

def _slow(seconds):
    time.sleep(seconds)
    return "done"


# Covered by tests/test_fix_rfc_workers_per_system.py (one worker process per system).


# ------------------------------------------------------ 9 error leakage

def test_500_detail_has_error_id_not_exception_text(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import dashboard.app as A
    from core import delivery_settings

    def boom(*a, **k):
        raise RuntimeError("C:\\secret\\path\\.env line 7: password=hunter2")

    monkeypatch.setattr(delivery_settings, "save_settings", boom)
    c = TestClient(A.app)
    csrf = c.get("/api/me").json()["csrf_token"]
    r = c.post("/api/delivery-settings", json={"plain": {}}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 500
    assert "hunter2" not in r.text and "secret\\\\path" not in r.text
    assert "error id" in r.json()["detail"]


def test_no_detail_str_exc_for_server_errors_in_app():
    src = _read("dashboard/app.py")
    assert not re.search(r"status_code=500,\s*detail=str\(exc\)", src)
    assert 'detail=f"Could not save: {exc}"' not in src


# ---------------------------------------------------- 10 report cache

def test_health_report_ai_summary_is_cached_per_snapshot(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import dashboard.app as A
    from evaluation import health_report
    calls = []
    monkeypatch.setattr(health_report, "_llm_summary", lambda ctx: calls.append(ctx) or "AI says hi")
    # The AI path is only taken with a real provider; the per-snapshot budget
    # cache (evaluation/ai_budget.py) must start empty and stay out of logs/.
    monkeypatch.setenv("USE_MOCK_AI", "false")
    monkeypatch.setattr(health_report, "_ai_on", lambda: True)
    from evaluation import ai_budget
    import tempfile, os as _os
    monkeypatch.setattr(ai_budget, "PATH", _os.path.join(tempfile.mkdtemp(), "ai_budget.json"))
    monkeypatch.setattr(ai_budget, "summary_allowed", lambda system: True)
    snap = {"system": "PS4", "client": "100", "cycle_timestamp": "2026-10-23 10:00:00",
            "metrics": [{"name": "cpu", "status": "WARNING", "display_value": "91%"}]}
    import core.status_snapshot as ss
    monkeypatch.setattr(ss, "load_snapshot", lambda name: dict(snap))
    A._report_cache.clear()
    c = TestClient(A.app)
    r1 = c.get("/api/report/PS4")
    r2 = c.get("/api/report/PS4")
    assert r1.status_code == 200 and r2.json()["summary"] == r1.json()["summary"]
    assert len(calls) == 1
    snap["cycle_timestamp"] = "2026-10-23 11:00:00"            # a new sweep
    c.get("/api/report/PS4")
    assert len(calls) == 2


# --------------------------------------------------- 6 binding defaults

def test_no_start_script_binds_all_interfaces():
    for rel in ("start.ps1", "start_dashboard_hidden.ps1", "packaging/windows/start_infrabeatops.ps1",
                "packaging/windows/start_infrabeatops.cmd", "packaging/windows/start_ui.ps1",
                "packaging/windows/start_collector.ps1", "packaging/systemd/infrabeatops.service",
                "packaging/OPERATIONS.md", "run_dashboard.py", "services/web.py"):
        assert "0.0.0.0 --port" not in _read(rel), rel
        assert "--host 0.0.0.0" not in _read(rel), rel
    hidden = _read("start_dashboard_hidden.ps1")
    assert "D:\\SAP_BASIS_MONITOR" not in hidden and "$PSScriptRoot" in hidden and ".venv" in hidden


def test_serve_defaults_to_loopback(monkeypatch):
    from dashboard import serve
    monkeypatch.delenv("IBO_BIND_HOST", raising=False)
    assert serve.bind_host() == "127.0.0.1" and serve.is_loopback(serve.bind_host())
    monkeypatch.setenv("IBO_BIND_HOST", "0.0.0.0")
    assert serve.bind_host() == "0.0.0.0" and not serve.is_loopback("0.0.0.0")


def test_stop_script_matches_the_port_exactly():
    bat = "\n".join(l for l in _read("stop_dashboard.bat").splitlines()
                    if not l.strip().lower().startswith("rem"))
    assert "findstr :8000" not in bat
    assert "-LocalPort" in bat and "IBO_(BIND" in bat     # exact port, read from .env
    assert "python*" in bat                                   # only the dashboard's python is stopped


# --------------------------------------------------- 11/12 repo hygiene

def test_gitignore_has_no_bom_no_crlf_and_covers_secrets_and_state():
    raw = open(os.path.join(ROOT, ".gitignore"), "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    text = raw.decode("utf-8")
    for line in ("config/users.yaml", "dashboard/session_secret.key", "config/systems.yaml",
                 "!config/systems.yaml.example", "config/backups/", "dashboard/*.lock",
                 "*.sqlite", "config/schedules.json", "config/profile_state.json",
                 "dashboard/run_history.json", "dashboard/snapshots/"):
        assert line in text.splitlines(), line


def test_runtime_requirements_are_pinned_and_test_tools_split_out():
    req = [l.strip() for l in _read("requirements.txt").splitlines()
           if l.strip() and not l.startswith("#")]
    assert all("==" in l for l in req), req
    names = {l.split("==")[0].lower() for l in req}
    assert "pytest" not in names and "httpx" not in names
    dev = _read("requirements-dev.txt")
    assert "-r requirements.txt" in dev and "pytest==" in dev and "httpx==" in dev


# --------------------------------------------------------- 5 escaping

def test_escape_helpers_escape_quotes_and_handlers_use_jsarg():
    shell = _read("dashboard/static/shell.js")
    assert "\"'\": \"&#39;\"" in shell
    index = _read("dashboard/static/index.html")
    assert "d.textContent=value;return d.innerHTML" not in index
    assert "\"'\":\"&#39;\"" in index
    assert "onclick=\"incidentAction('${incidentId}'" not in index
    assert "${x.actor}" not in index and "${ai ? (ai.likely_root_cause" not in index
    for name in os.listdir(os.path.join(ROOT, "dashboard", "static")):
        if name.endswith((".html", ".js")):
            text = _read(os.path.join("dashboard", "static", name))
            # IB.esc output decoded by the attribute parser would break out of
            # a JS string: inline handlers must use IB.jsArg.
            assert not re.search(r"on\w+=\"[^\"]*'\$\{(IB|OPS)\.esc\(", text), name
