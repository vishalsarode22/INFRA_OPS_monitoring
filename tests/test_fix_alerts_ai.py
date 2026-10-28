"""
Finding 7: AI budget on the free Gemini tier. No network: requests.post is
replaced; state files live in tmp_path.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ------------------------------------------------------------- fingerprint

def test_fingerprint_ignores_live_values():
    from evaluation.ai_analyzer import _incident_ai_fingerprint as fp
    a = SimpleNamespace(rule_id="SM12_LOCKS", severity="WARNING",
                        evidence=["SM12 locks = 641 at 09:14"], affected_metrics=["sap.sm12.lock_count"])
    b = SimpleNamespace(rule_id="SM12_LOCKS", severity="WARNING",
                        evidence=["SM12 locks = 702 at 09:19"], affected_metrics=["sap.sm12.lock_count"])
    assert fp(a) == fp(b)
    c = SimpleNamespace(**{**vars(b), "affected_metrics": ["sap.sm12.lock_count", "sap.sm50.wp_utilization"]})
    d = SimpleNamespace(**{**vars(b), "severity": "CRITICAL"})
    assert fp(c) != fp(a) and fp(d) != fp(a)


# ------------------------------------------------------------- ST22 bands

@pytest.fixture
def budget(tmp_path, monkeypatch):
    from evaluation import ai_budget
    monkeypatch.setattr(ai_budget, "PATH", str(tmp_path / "ai_budget.json"))
    monkeypatch.delenv("IBO_AI_ST22_BANDS", raising=False)
    return ai_budget


def test_st22_once_a_day_unless_a_new_band(budget):
    day = "2026-10-23"
    assert not budget.st22_due("PS4", 0, day)
    assert budget.st22_due("PS4", 3, day)
    budget.st22_mark("PS4", 3, day)
    assert not budget.st22_due("PS4", 7, day)        # same band (1-9)
    assert budget.st22_due("PS4", 12, day)           # 10+ is new
    budget.st22_mark("PS4", 12, day)
    assert not budget.st22_due("PS4", 40, day)
    assert budget.st22_due("PS4", 3, "2026-10-24")   # a new SAP day
    assert budget.st22_due("CEAT S4P", 3, day)       # per system


def test_analyze_incidents_skips_st22_in_the_same_band(budget, monkeypatch):
    from evaluation import ai_analyzer
    from core.models import MonitoringResult, AIAnalysis
    calls = []
    monkeypatch.setattr(ai_analyzer, "analyze",
                        lambda *a, **k: calls.append(1) or AIAnalysis(root_cause_category="X"))
    prov = SimpleNamespace(name="gemini")
    gui = [SimpleNamespace(tcode="ST22", extra_data={"dump_count": 4})]
    r = MonitoringResult(system="PS4", client="100")
    r.gui_results = gui
    ai_analyzer.analyze_incidents(r, provider=prov)
    ai_analyzer.analyze_incidents(r, provider=prov)
    assert len(calls) == 1
    gui[0].extra_data["dump_count"] = 55
    ai_analyzer.analyze_incidents(r, provider=prov)
    assert len(calls) == 2


def test_report_summary_cached_per_snapshot(budget, monkeypatch):
    from evaluation import health_report
    from core.models import MonitoringResult
    monkeypatch.setenv("USE_MOCK_AI", "false")
    monkeypatch.setattr(health_report, "_ai_on", lambda: True)
    calls = []
    monkeypatch.setattr(health_report, "_llm_summary", lambda ctx: calls.append(ctx) or "AI words.")
    r = MonitoringResult(system="PS4", client="100", cycle_timestamp=datetime(2026, 10, 23, 9, 0))
    for _ in range(5):
        rep = health_report.build_report(r)
    assert len(calls) == 1 and rep["summary"] == "AI words." and rep["summary_source"] == "llm"
    # a live report (new stamp every call) is rate-limited
    r2 = MonitoringResult(system="PS4", client="100", cycle_timestamp=datetime(2026, 10, 23, 9, 1))
    rep2 = health_report.build_report(r2)
    assert len(calls) == 1 and rep2["summary_source"] == "rules"


# ------------------------------------------------------------- Gemini quota

def _resp(status, body, headers=None):
    return SimpleNamespace(status_code=status, text=json.dumps(body), headers=headers or {},
                           json=lambda: body, raise_for_status=lambda: None)


PER_MINUTE = {"error": {"code": 429, "message": "You exceeded your current quota",
                        "status": "RESOURCE_EXHAUSTED", "details": [
                            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                                {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                                 "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
                            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}}
PER_DAY = {"error": {"code": 429, "message": "You exceeded your current quota",
                     "status": "RESOURCE_EXHAUSTED", "details": [
                         {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                             {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}


@pytest.fixture
def gem(monkeypatch):
    from evaluation.providers import gemini
    gemini.reset_quota_blocks()
    gemini.reset_rate_limiter()
    yield gemini
    gemini.reset_quota_blocks()
    gemini.reset_rate_limiter()


def test_quota_window_parsing(gem):
    assert gem._quota_window(json.dumps(PER_MINUTE), None) == ("minute", 60.0)
    assert gem._quota_window(json.dumps(PER_DAY), None)[0] == "day"
    assert gem._quota_window("too many requests", "90") == ("minute", 90.0)
    assert gem._quota_window("", None) == ("minute", 60.0)      # ambiguous: back off, do not retire


def test_per_minute_429_pauses_the_key_briefly(gem):
    from evaluation.providers.grok import QuotaExhausted
    p = gem.GeminiProvider(api_key="k-minute")
    with mock.patch.object(gem.requests, "post", return_value=_resp(429, PER_MINUTE)):
        with pytest.raises(QuotaExhausted):
            p.generate("x")
    left = gem._quota_block_remaining("k-minute")
    assert 0 < left <= 61


def test_per_day_429_retires_the_key_until_the_daily_reset(gem):
    from evaluation.providers.grok import QuotaExhausted
    p = gem.GeminiProvider(api_key="k-day")
    with mock.patch.object(gem.requests, "post", return_value=_resp(429, PER_DAY)):
        with pytest.raises(QuotaExhausted):
            p.generate("x")
    left = gem._quota_block_remaining("k-day")
    assert 60 < left <= 24 * 3600
    reset = gem._next_daily_reset()
    assert reset.hour == 8 and reset.minute == 0


def test_client_side_rpm_limiter(gem, monkeypatch):
    from evaluation.providers.grok import QuotaExhausted
    monkeypatch.setenv("IBO_GEMINI_RPM", "2")
    ok = {"candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}]}
    p = gem.GeminiProvider(api_key="k-rpm")
    with mock.patch.object(gem.requests, "post", return_value=_resp(200, ok)) as post:
        p.generate("a")
        p.generate("b")
        with pytest.raises(QuotaExhausted):
            p.generate("c")
    assert post.call_count == 2


# ------------------------------------------------------------- RCA budget

@pytest.fixture
def rca(tmp_path, monkeypatch):
    from core import rca_trigger
    from pathlib import Path
    monkeypatch.setattr(rca_trigger, "RCA_STATE_PATH", Path(tmp_path / "rca.json"))
    monkeypatch.delenv("IBO_RCA_DAILY_CAP", raising=False)
    return rca_trigger


def test_rca_cooldown_backs_off_and_is_capped_per_day(rca):
    t = datetime(2026, 10, 23, 8, 0)
    assert rca.rca_budget_block("PS4", 30, t) is None
    rca.mark_fired_on_disk("PS4", "spike", now=t, base_minutes=30)
    assert "cooldown" in rca.rca_budget_block("PS4", 30, t + timedelta(minutes=20))
    t2 = t + timedelta(minutes=31)
    assert rca.rca_budget_block("PS4", 30, t2) is None
    rca.mark_fired_on_disk("PS4", "spike", now=t2, base_minutes=30)
    assert rca.rca_cooldown_minutes("PS4", 30) == 60
    assert rca.rca_budget_block("PS4", 30, t2 + timedelta(minutes=45)) is not None
    t3 = t2 + timedelta(minutes=61)
    rca.mark_fired_on_disk("PS4", "spike", now=t3, base_minutes=30)
    assert rca.rca_cooldown_minutes("PS4", 30) == 120
    t4 = t3 + timedelta(minutes=121)
    rca.mark_fired_on_disk("PS4", "spike", now=t4, base_minutes=30)
    assert rca.rca_cooldown_minutes("PS4", 30) == 120               # capped at 4 x base
    # four runs today: the daily cap (4) blocks the fifth whatever the cooldown
    block = rca.rca_budget_block("PS4", 30, t4 + timedelta(hours=5))
    assert block and "daily cap" in block
    # persisted on disk: a new process sees the same
    assert json.loads(rca.RCA_STATE_PATH.read_text())["PS4"]["count"] == 4


def test_sweep_decision_obeys_the_budget(rca, monkeypatch):
    cfg = rca.TriggerConfig(mode="system", threshold_ms=1500, cooldown_minutes=30)
    t = datetime(2026, 10, 23, 8, 0)
    fire, _ = rca.sweep_decision("PS4", 3000, "app1", cfg, now=t)
    assert fire
    rca.mark_fired_on_disk("PS4", "x", now=t, base_minutes=30)
    fire, why = rca.sweep_decision("PS4", 3000, "app1", cfg, now=t + timedelta(minutes=5))
    assert not fire and "cooldown" in why
