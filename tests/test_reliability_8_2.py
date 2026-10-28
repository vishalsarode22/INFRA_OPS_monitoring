import json


def test_intelligence_persistence_failure_does_not_break_evaluation(tmp_path, monkeypatch):
    from core.intelligence_runtime import IntelligenceRuntime
    from core.models import MetricResult, MonitoringResult, Status
    runtime = IntelligenceRuntime(state_dir=str(tmp_path))
    result = MonitoringResult(system="RECOVERY", client="000")
    result.metrics = [MetricResult("cpu", 10, "10", Status.NORMAL, source="TEST")]
    result.compute_overall_status()
    def fail(_system):
        raise OSError("disk")
    monkeypatch.setattr(runtime, "persist_system_state", fail)
    intelligence = runtime.evaluate(result)
    assert intelligence is not None


def test_dashboard_system_failure_does_not_stop_next_system(monkeypatch):
    import dashboard.app as app
    calls = []
    monkeypatch.setattr(app, "get_systems", lambda: [{"name": "A"}, {"name": "B"}])
    monkeypatch.setattr(app, "_run_single_system", lambda cfg: calls.append(cfg["name"]) or cfg["name"] == "B")
    app._run_state["running"] = False
    app._run_all_systems_background()
    assert calls == ["A", "B"]
    assert app._run_state["running"] is False
    assert app._run_state["current_system"] is None
    assert app._run_state["error"] == "1 system(s) failed during the sweep."


def test_dashboard_single_system_runner_clears_current_system_on_exception(monkeypatch):
    import dashboard.app as app
    import main
    monkeypatch.setattr(app, "_append_run_history", lambda event: None)
    monkeypatch.setattr(main, "run_pipeline_for_system", lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    assert app._run_single_system({"name": "FAIL"}) is False
    assert app._run_state["current_system"] is None


def test_dashboard_failed_system_is_reported_without_secret(monkeypatch):
    import dashboard.app as app
    import main
    events = []
    monkeypatch.setattr(app, "_append_run_history", lambda event: events.append(event))
    monkeypatch.setattr(main, "run_pipeline_for_system", lambda cfg: False)
    assert app._run_single_system({"name": "SYS"}) is False
    assert events[-1]["status"] == "error"
    assert "password" not in str(events[-1]).lower()


def test_gemini_timeout_error_is_generic(monkeypatch):
    from evaluation.providers.gemini import GeminiProvider
    import requests
    provider = GeminiProvider(model="test", retries=0, timeout=1)
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET")
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.Timeout("SECRET")))
    try:
        provider.generate("x")
    except RuntimeError as exc:
        assert "SECRET" not in str(exc)
        assert "Timeout" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_run_history_remains_bounded(monkeypatch, tmp_path):
    import dashboard.app as app
    path = tmp_path / "history.json"
    monkeypatch.setattr(app, "RUN_HISTORY_PATH", str(path))
    for i in range(80):
        app._append_run_history({"system": "S", "i": i})
    data = json.loads(path.read_text())
    assert len(data) == 50
    assert data[-1]["i"] == 79
