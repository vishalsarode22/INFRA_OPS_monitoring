import dashboard.app as dashboard_app
import core.orchestrator as orchestrator


def test_scheduler_processes_all_systems_and_isolates_failure(monkeypatch):
    systems = [
        {"name": "TST", "client": "000"},
        {"name": "QAS", "client": "100"},
        {"name": "DEV", "client": "200"},
    ]
    calls = []

    def fake_get_systems():
        return systems

    def fake_run(system_config):
        calls.append(system_config["name"])
        return system_config["name"] != "QAS"

    monkeypatch.setattr(dashboard_app, "get_systems", fake_get_systems)
    monkeypatch.setattr(dashboard_app, "_run_single_system", fake_run)
    dashboard_app._run_state.update({"running": False, "error": None, "current_system": None})

    dashboard_app._run_all_systems_background()

    assert calls == ["TST", "QAS", "DEV"]
    assert dashboard_app._run_state["running"] is False
    assert dashboard_app._run_state["error"] == "1 system(s) failed during the sweep."


def test_gui_only_system_skips_os_collectors(monkeypatch):
    called = {"linux": 0, "sap": 0}

    def fail_linux(*args, **kwargs):
        called["linux"] += 1
        raise AssertionError("Linux collector must not run for GUI-only system")

    def fail_sap(*args, **kwargs):
        called["sap"] += 1
        raise AssertionError("SAP process collector must not run for GUI-only system")

    monkeypatch.setattr(orchestrator, "collect_linux_metrics", fail_linux)
    monkeypatch.setattr(orchestrator, "collect_sap_process_list", fail_sap)
    monkeypatch.setattr(orchestrator, "get_thresholds", lambda: {})
    monkeypatch.setattr(orchestrator, "run_ai_analysis", lambda result: None)

    result = orchestrator.run_monitoring_cycle(
        system="QAS",
        client="100",
        ssh_creds={"host": ""},
        instance_nr=None,
        run_ai=False,
        send_alert=False,
    )

    assert called == {"linux": 0, "sap": 0}
    assert result.system == "QAS"
    assert result.client == "100"


def test_explicit_system_ssh_credentials_are_used(monkeypatch):
    observed = {}

    def fake_linux(**kwargs):
        observed.update(kwargs)
        return {}

    monkeypatch.setattr(orchestrator, "collect_linux_metrics", fake_linux)
    monkeypatch.setattr(orchestrator, "parse_linux_metrics", lambda raw: [])
    monkeypatch.setattr(orchestrator, "collect_sap_process_list", lambda **kwargs: {})
    monkeypatch.setattr(orchestrator, "parse_sap_process_list", lambda raw: [])
    monkeypatch.setattr(orchestrator, "get_thresholds", lambda: {})
    monkeypatch.setattr(orchestrator, "run_ai_analysis", lambda result: None)

    creds = {
        "host": "qas-host",
        "port": 22,
        "username": "qas-ssh",
        "password": "test-secret",
    }
    orchestrator.run_monitoring_cycle(
        system="QAS",
        client="100",
        ssh_creds=creds,
        instance_nr="03",
        run_ai=False,
        send_alert=False,
    )

    assert observed == creds


def test_system_instance_number_is_not_taken_from_global_env(monkeypatch):
    observed = {}

    monkeypatch.setattr(orchestrator, "collect_linux_metrics", lambda **kwargs: {})
    monkeypatch.setattr(orchestrator, "parse_linux_metrics", lambda raw: [])

    def fake_sap(**kwargs):
        observed.update(kwargs)
        return {}

    monkeypatch.setattr(orchestrator, "collect_sap_process_list", fake_sap)
    monkeypatch.setattr(orchestrator, "parse_sap_process_list", lambda raw: [])
    monkeypatch.setattr(orchestrator, "get_thresholds", lambda: {})
    monkeypatch.setattr(orchestrator, "run_ai_analysis", lambda result: None)

    orchestrator.run_monitoring_cycle(
        system="QAS",
        client="100",
        ssh_creds={"host": "qas-host", "port": 22, "username": "u", "password": "p"},
        instance_nr="03",
        run_ai=False,
        send_alert=False,
    )

    assert observed["instance_nr"] == "03"


def test_run_now_does_not_start_second_sweep(monkeypatch):
    dashboard_app._run_state.update({"running": True, "error": None, "current_system": "TST"})
    try:
        from fastapi.testclient import TestClient
        client = TestClient(dashboard_app.app)
        response = client.post("/api/run-now")
        assert response.status_code == 409
    finally:
        dashboard_app._run_state.update({"running": False, "error": None, "current_system": None})
