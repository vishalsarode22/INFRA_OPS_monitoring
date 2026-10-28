"""Remote sources: systems collected by another dashboard show up here, and a
lost collector is never shown as healthy."""
import time

import pytest

from dashboard import remote_sources as rs

SRC = {"name": "ubuntu", "url": "http://127.0.0.1:8001", "label": "Ubuntu"}
_real_load_sources = rs.load_sources


def _payload(name="SARLOHA_QAS", **extra):
    base = {
        "system": name, "sid": "QA1", "client": "500", "connected": True,
        "error": None, "cooldown": 0, "cpu": None, "memory": None,
        "checks": [{"metric": "sap.st22.dumps", "value": "5 count", "status": "WARNING"}],
        "instances": [], "work_processes": {"total": 60},
        "dispatcher": {"label": "Running", "status": "NORMAL", "source": ""},
        "icm": {"label": "Running", "status": "NORMAL", "source": ""},
        "cache_age_seconds": 12,
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    rs._state.clear(); rs._shown.clear()
    monkeypatch.setattr(rs, "load_sources", lambda: [SRC])
    monkeypatch.setattr(rs, "ensure_poller", lambda: None)
    # The failure diagnosis probes TCP ports; never touch the real network
    # (a real tunnel on 8001 on the test machine would change the answer).
    from core import reachability
    monkeypatch.setattr(reachability, "tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(reachability, "report", lambda *a, **k: None)
    monkeypatch.setattr(reachability, "report_ok", lambda *a, **k: None)
    yield
    rs._state.clear()


def _poll_with(monkeypatch, result):
    def fake_fetch(_src):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(rs, "_fetch", fake_fetch)
    rs.poll_once()


def test_no_sources_configured_leaves_the_list_untouched(monkeypatch):
    monkeypatch.setattr(rs, "load_sources", lambda: [])
    local = [{"system": "PS4"}]
    assert rs.merge(local) is local


def test_remote_system_is_merged_and_tagged(monkeypatch):
    _poll_with(monkeypatch, [_payload()])
    out = rs.merge([{"system": "PS4", "connected": True}])
    assert [p["system"] for p in out] == ["PS4", "SARLOHA_QAS"]
    remote = out[1]
    assert remote["remote"] is True and remote["source"] == "ubuntu"
    assert remote["client"] == "500 · via Ubuntu"
    assert remote["checks"][0]["value"] == "5 count"
    assert remote["remote_age_seconds"] >= 12      # fetch age + the source's own cache age
    assert remote["connected"] is True


def test_a_source_that_goes_silent_is_not_shown_as_healthy(monkeypatch):
    _poll_with(monkeypatch, [_payload()])
    _poll_with(monkeypatch, OSError("timed out"))
    rs._state["ubuntu"]["at"] = time.time() - 600            # silent for ten minutes
    card = rs.merge([])[0]
    assert card["connected"] is False
    assert card["source_unreachable"] is True
    assert card["checks"] == [] and card["work_processes"] == {}   # old values dropped
    # Nothing listens on the tunnel port in this test: the card says the
    # tunnel is down, keeps the raw error, and is not shown as live.
    assert "[TUNNEL DOWN]" in card["error"] and "not shown as live" in card["error"]
    assert "timed out" in card["error"]
    assert card["dispatcher"]["status"] == "UNKNOWN"


def test_one_failed_poll_does_not_flicker_the_card(monkeypatch):
    _poll_with(monkeypatch, [_payload()])
    _poll_with(monkeypatch, OSError("timed out"))
    card = rs.merge([])[0]
    assert card["connected"] is True and not card.get("source_unreachable")


def test_a_source_that_never_answered_gets_a_visible_placeholder(monkeypatch):
    _poll_with(monkeypatch, OSError("connection refused"))
    out = rs.merge([])
    assert len(out) == 1
    assert out[0]["source_unreachable"] is True and out[0]["connected"] is False
    assert out[0]["error"].startswith("[TUNNEL DOWN]")
    assert "connection refused" in out[0]["error"]


def test_a_local_system_keeps_its_name_when_names_clash(monkeypatch):
    _poll_with(monkeypatch, [_payload("PS4")])
    out = rs.merge([{"system": "PS4"}])
    assert [p["system"] for p in out] == ["PS4", "PS4 (Ubuntu)"]


def test_find_returns_the_remote_payload_for_the_detail_page(monkeypatch):
    _poll_with(monkeypatch, [_payload()])
    assert rs.find("SARLOHA_QAS")["checks"][0]["metric"] == "sap.st22.dumps"
    assert rs.find("PS4") is None


def test_bad_answer_from_a_source_is_recorded_not_raised(monkeypatch):
    _poll_with(monkeypatch, ValueError("unexpected answer (no 'systems' list)"))
    assert rs._state["ubuntu"]["error"].startswith("ValueError")


def test_config_file_parsing_skips_bad_entries(tmp_path, monkeypatch):
    f = tmp_path / "remote_sources.yaml"
    f.write_text(
        "sources:\n"
        "  - name: ubuntu\n    label: Ubuntu collector\n    url: http://127.0.0.1:8001/\n"
        "  - name: nourl\n"
        "  - name: ftp\n    url: ftp://example\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("IBO_REMOTE_SOURCES_FILE", str(f))
    got = _real_load_sources()
    assert got == [{"name": "ubuntu", "url": "http://127.0.0.1:8001",
                    "label": "Ubuntu collector", "token": "", "customer": "",
                    "ssh_host": "", "ssh_port": 22, "ssh_user": "", "remote_port": 8000}]


def test_missing_config_file_means_the_feature_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_REMOTE_SOURCES_FILE", str(tmp_path / "nope.yaml"))
    assert _real_load_sources() == []


# ---------------------------------------------------------------------------
# Forwarding "Monitor this system" to the collector
# ---------------------------------------------------------------------------

SRC_TOK = dict(SRC, token="abc123")


def _calls(monkeypatch, answer):
    """Record forwarded calls; `answer` is (status, body) or an exception."""
    seen = []
    def fake_call(source, method, path, token=""):
        seen.append((source["name"], method, path, token))
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(rs, "_call", fake_call)
    return seen


def test_run_is_forwarded_with_the_collectors_token(monkeypatch):
    monkeypatch.setattr(rs, "load_sources", lambda: [SRC_TOK])
    _poll_with(monkeypatch, [_payload()])
    seen = _calls(monkeypatch, (200, {"started": True, "scope": "SARLOHA_QAS"}))
    status, body = rs.start_remote_run("SARLOHA_QAS")
    assert status == 200 and body["started"] is True
    assert body["forwarded_to"] == "Ubuntu" and body["remote_system"] == "SARLOHA_QAS"
    assert seen == [("ubuntu", "POST", "/api/run-now/SARLOHA_QAS", "abc123")]


def test_renamed_card_forwards_the_collectors_own_name(monkeypatch):
    monkeypatch.setattr(rs, "load_sources", lambda: [SRC_TOK])
    _poll_with(monkeypatch, [_payload("PS4")])
    rs.merge([{"system": "PS4"}])             # local PS4 exists -> remote shown as "PS4 (Ubuntu)"
    seen = _calls(monkeypatch, (200, {"started": True}))
    status, _ = rs.start_remote_run("PS4 (Ubuntu)")
    assert status == 200
    assert seen[0][2] == "/api/run-now/PS4"


def test_no_token_means_refused_locally_without_calling_the_collector(monkeypatch):
    _poll_with(monkeypatch, [_payload()])      # SRC has no token
    seen = _calls(monkeypatch, (200, {"started": True}))
    status, body = rs.start_remote_run("SARLOHA_QAS")
    assert status == 401 and "No token configured" in body["detail"]
    assert seen == []


def test_collector_refusals_are_passed_through(monkeypatch):
    monkeypatch.setattr(rs, "load_sources", lambda: [SRC_TOK])
    _poll_with(monkeypatch, [_payload()])
    _calls(monkeypatch, (409, {"detail": "A run is already in progress (SARLOHA PRD)."}))
    status, body = rs.start_remote_run("SARLOHA_QAS")
    assert status == 409 and "already in progress" in body["detail"]


def test_unknown_or_local_system_is_not_forwarded(monkeypatch):
    monkeypatch.setattr(rs, "load_sources", lambda: [SRC_TOK])
    _poll_with(monkeypatch, [_payload()])
    seen = _calls(monkeypatch, (200, {}))
    status, body = rs.start_remote_run("PS4")
    assert status == 404 and "not supplied by a remote collector" in body["detail"]
    assert seen == []


def test_remote_run_status_is_tagged_with_the_collector(monkeypatch):
    monkeypatch.setattr(rs, "load_sources", lambda: [SRC_TOK])
    _poll_with(monkeypatch, [_payload()])
    _calls(monkeypatch, (200, {"running": True, "current_system": "SARLOHA_QAS"}))
    st = rs.remote_run_status("SARLOHA_QAS")
    assert st["running"] is True and st["remote"] is True and st["collector"] == "Ubuntu"
    assert rs.remote_run_status("PS4") == {"running": False, "remote": False}


# ---------------------------------------------------------------------------
# "Read live" also makes the collector re-read (no stale collector data)
# ---------------------------------------------------------------------------

def test_read_live_asks_the_collector_to_reread_then_fetches(monkeypatch):
    calls = []
    src = dict(SRC, token="tok")
    monkeypatch.setattr(rs, "load_sources", lambda: [src])
    states = iter([{"in_progress": True}, {"in_progress": False}])

    def fake_call(source, method, path, token=""):
        calls.append((method, path, token))
        if path == "/api/live/refresh":
            return 200, {"started": True}
        return 200, next(states)

    monkeypatch.setattr(rs, "_call", fake_call)
    monkeypatch.setattr(rs.time, "sleep", lambda s: None)
    fetched = []
    monkeypatch.setattr(rs, "poll_once", lambda: fetched.append(True))
    notes = rs.refresh_remote(timeout=5)
    assert calls[0] == ("POST", "/api/live/refresh", "tok")
    assert all(c[2] == "tok" for c in calls)
    assert fetched == [True] and "re-read" in notes[0]


def test_read_live_without_token_does_not_call_the_collector(monkeypatch):
    monkeypatch.setattr(rs, "load_sources", lambda: [dict(SRC, token="")])
    monkeypatch.setattr(rs, "_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    monkeypatch.setattr(rs, "poll_once", lambda: None)
    assert "no token" in rs.refresh_remote(timeout=1)[0]
