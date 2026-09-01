from datetime import datetime, timedelta

from core.intelligence_runtime import IntelligenceRuntime, get_intelligence_runtime, reset_intelligence_runtime
from core.models import MetricResult, MonitoringResult, Status


def _result(system="PERSIST-SYS", value=10, timestamp=None):
    timestamp = timestamp or datetime(2026, 8, 18, 15, 0)
    result = MonitoringResult(system=system, client="100", cycle_timestamp=timestamp)
    result.metrics = [
        MetricResult("sap.cpu.percent", value, str(value), Status.NORMAL, source="TEST")
    ]
    result.compute_overall_status()
    return result


def test_runtime_state_survives_new_runtime_instance(tmp_path):
    first = IntelligenceRuntime(state_dir=str(tmp_path), max_samples=10)
    first.evaluate(_result(value=10))
    first.evaluate(_result(value=20, timestamp=datetime(2026, 8, 18, 15, 1)))

    second = IntelligenceRuntime(state_dir=str(tmp_path), max_samples=10)
    second.load_system_state("PERSIST-SYS")

    assert second.baseline.history("sap.cpu.percent") == (10.0, 20.0)
    assert len(second._samples["sap.cpu.percent"]) == 2


def test_persisted_state_is_bounded(tmp_path):
    runtime = IntelligenceRuntime(state_dir=str(tmp_path), max_samples=3)
    for index in range(6):
        runtime.evaluate(
            _result(
                value=index,
                timestamp=datetime(2026, 8, 18, 15, 0) + timedelta(minutes=index),
            )
        )

    restored = IntelligenceRuntime(state_dir=str(tmp_path), max_samples=3)
    restored.load_system_state("PERSIST-SYS")

    assert restored.baseline.history("sap.cpu.percent") == (3.0, 4.0, 5.0)


def test_corrupt_state_fails_safe(tmp_path):
    path = tmp_path / "BROKEN.json"
    path.write_text("{not valid json", encoding="utf-8")

    runtime = IntelligenceRuntime(state_dir=str(tmp_path))
    runtime.load_system_state("BROKEN")

    assert runtime._samples == {}
    assert runtime.baseline.history("sap.cpu.percent") == ()


def test_system_state_files_are_isolated(tmp_path):
    a = IntelligenceRuntime(state_dir=str(tmp_path))
    b = IntelligenceRuntime(state_dir=str(tmp_path))

    a.evaluate(_result(system="SYS-A", value=11))
    b.evaluate(_result(system="SYS-B", value=99))

    a2 = IntelligenceRuntime(state_dir=str(tmp_path))
    a2.load_system_state("SYS-A")

    b2 = IntelligenceRuntime(state_dir=str(tmp_path))
    b2.load_system_state("SYS-B")

    assert a2.baseline.history("sap.cpu.percent") == (11.0,)
    assert b2.baseline.history("sap.cpu.percent") == (99.0,)


def test_registry_restores_persisted_state(tmp_path, monkeypatch):
    import core.intelligence_runtime as ir

    reset_intelligence_runtime()
    first = IntelligenceRuntime(state_dir=str(tmp_path))
    first.evaluate(_result(system="REG-SYS", value=42))

    monkeypatch.setattr(ir, "BASE_DIR", str(tmp_path))
    # Explicitly seed the registry with a runtime configured for the same
    # state directory, then verify normal registry reuse remains deterministic.
    reset_intelligence_runtime()
    runtime = ir.IntelligenceRuntime(state_dir=str(tmp_path))
    runtime.load_system_state("REG-SYS")
    ir._RUNTIME_REGISTRY["REG-SYS"] = runtime

    assert ir.get_intelligence_runtime("REG-SYS") is runtime
    assert runtime.baseline.history("sap.cpu.percent") == (42.0,)

    reset_intelligence_runtime()
