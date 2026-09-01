import os
from pathlib import Path

import core.production_health as health


def test_liveness_is_ok():
    result = health.liveness()
    assert result["status"] == "ok"
    assert result["service"] == "InfraBeatOps"


def test_readiness_contains_required_checks():
    result = health.readiness()
    assert "checks" in result
    assert "config_directory" in result["checks"]
    assert "snapshot_storage" in result["checks"]


def test_gemini_key_is_never_returned():
    old = os.environ.get("GEMINI_API_KEY")
    try:
        os.environ["GEMINI_API_KEY"] = "SUPER-SECRET-DO-NOT-RETURN"
        result = health.readiness()
        assert "SUPER-SECRET-DO-NOT-RETURN" not in str(result)
    finally:
        if old is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = old


def test_ai_provider_does_not_make_readiness_depend_on_network(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = health.readiness()
    assert result["checks"]["ai_provider"]["ok"] is True
    assert result["checks"]["ai_provider"]["configured"] is False


def test_readiness_has_no_secret_fields():
    result = health.readiness()
    text = str(result).lower()
    assert "password" not in text
    assert "api_key" not in text


def test_readiness_status_is_binary_contract():
    result = health.readiness()
    assert result["status"] in {"ready", "not_ready"}
