"""Per-system thresholds: override > thresholds.yaml > code default, everywhere that grades."""
import os

import pytest

from core import thresholds as T


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from core import config_loader, timeline
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(T, "_known_system", lambda n: n in ("PS4", "CEAT BW_PRD"))
    monkeypatch.setattr(timeline, "add", lambda *a, **k: None)
    (tmp_path / "thresholds.yaml").write_text(
        "sap.st22.dumps:\n  warning: 5\n  critical: 10\n  inclusive: true\n"
        "sap.smq1.entries:\n  warning: 10\n  critical: 50\n"
        "sap.smlg.response_time:\n  warning: 1500\n  critical: 2000\n  emergency: 2500\n", encoding="utf-8")
    T._cache.clear()
    return tmp_path


def test_order_override_then_standard_then_default(cfg):
    T.save_override("PS4", "sap.st22.dumps", 20, 40)
    assert T.limits("sap.st22.dumps", (1, 2), system="PS4") == (20, 40)
    assert T.limits("sap.st22.dumps", (1, 2), system="CEAT BW_PRD") == (5, 10)
    assert T.limits("sap.st22.dumps", (1, 2)) == (5, 10)                    # no current system
    assert T.limits("sap.sm58.stuck_entries", (1, 10), system="PS4") == (1, 10)
    assert T.inclusive("sap.st22.dumps", system="PS4") is True           # not given: the standard's tick
    T.save_override("PS4", "sap.st22.dumps", 20, 40, False)
    assert T.inclusive("sap.st22.dumps", system="PS4") is False          # page sends the box as it is


def test_current_system_is_scoped(cfg):
    T.save_override("PS4", "sap.smq1.entries", 1, 5)
    with T.use_system("PS4"):
        assert T.limits("sap.smq1.entries") == (1, 5)
    assert T.limits("sap.smq1.entries") == (10, 50)


def test_override_only_one_field(cfg):
    T.save_override("PS4", "sap.smq1.entries", None, 99)
    assert T.limits("sap.smq1.entries", system="PS4") == (10, 99)


def test_reset_and_unknown_system(cfg):
    T.save_override("PS4", "sap.smq1.entries", 1, 5)
    assert T.remove_override("PS4", "sap.smq1.entries") is True
    assert T.limits("sap.smq1.entries", system="PS4") == (10, 50)
    assert not os.path.exists(cfg / "nothing")
    with pytest.raises(ValueError):
        T.save_override("NOPE", "sap.smq1.entries", 1, 5)
    with pytest.raises(ValueError):
        T.save_override("PS4", "sap.smq1.entries", 9, 5)                   # warning above critical


def test_rfc_collector_grades_with_the_session_system(cfg):
    from collectors import rfc_collector as C
    T.save_override("PS4", "sap.st22.dumps", 20, 40)
    calls = []

    @C._for_session_system
    def grade(session, v):
        calls.append(C._limits("sap.st22.dumps"))
        return C._grade("sap.st22.dumps", v)

    class S:
        system = "PS4"
    assert grade(S(), 12).value == "NORMAL" and calls[-1] == (20, 40)
    S.system = "CEAT BW_PRD"
    assert grade(S(), 12).value == "CRITICAL"


def test_threshold_engine_applies_overrides(cfg):
    from evaluation.threshold_engine import evaluate_all
    from core.models import MetricResult, Status
    T.save_override("PS4", "sap.smq1.entries", 1, 5)
    table = T.global_table()
    m = lambda: [MetricResult(name="sap.smq1.entries", value=7.0, display_value="7", status=Status.UNKNOWN,  # noqa: E731
                              source="sap_gui_collector", tcode="SMQ1")]
    assert evaluate_all(m(), table, system="PS4")[0].status.value == "CRITICAL"
    assert evaluate_all(m(), table, system="CEAT BW_PRD")[0].status.value == "NORMAL"


def test_live_view_uses_yaml_and_override(cfg):
    from core import live_metrics as L
    T.save_override("PS4", "sap.st22.dumps", 20, 40)
    assert L.effective_thresholds("PS4")["sap.st22.dumps"] == (20, 40)
    assert L.effective_thresholds("CEAT BW_PRD")["sap.st22.dumps"] == (5, 10)


def test_smlg_bands_follow_the_override(cfg):
    from sap_gui.smlg_analyzer import classify_response_time
    T.save_override("PS4", "sap.smlg.response_time", 3000, 4000)
    with T.use_system("PS4"):
        g = classify_response_time(2200)
        assert g["status"].value == "NORMAL" and g["threshold"] == "<= 3000 ms"
    assert classify_response_time(2200)["status"].value == "CRITICAL"


def test_catalogue_lists_dashboard_metrics(cfg):
    cat = T.catalogue()
    for k in ("sap.sm58.stuck_entries", "sap.smq1.entries", "sap.st03.dialog_resp_ms", "sap.sm12.lock_count"):
        assert k in cat
    assert cat["sap.sm58.stuck_entries"]["warning"] == 1


def test_st22_override_applies_to_both_names(cfg):
    T.save_override("PS4", "sap.st22.dumps", 20, 40)
    assert T.limits("sap.st22.dump_count", system="PS4") == (20, 40)


def test_override_validated_against_the_standard(cfg):
    with pytest.raises(ValueError):
        T.save_override("PS4", "sap.smq1.entries", None, 5)      # standard warning 10 > 5


def test_smlg_emergency_never_below_critical(cfg):
    from sap_gui.smlg_analyzer import classify_response_time
    T.save_override("PS4", "sap.smlg.response_time", 2000, 3000)
    with T.use_system("PS4"):
        g = classify_response_time(2700)
        assert g["status"].value == "WARNING"


def test_unticked_limit_is_critical_on_the_wall_too(cfg):
    from collectors import rfc_collector as C
    T.save_override("PS4", "sap.st22.dumps", 5, 10, False)
    with T.use_system("PS4"):
        assert C._grade("sap.st22.dumps", 10).value == "WARNING"
    assert C._grade("sap.st22.dumps", 10).value == "CRITICAL"
