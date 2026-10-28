
import logging

from core.config_loader import validate_configuration, configuration_summary
from utils.logger import SecretRedactionFilter
import core.production_health as health


def test_configuration_summary_never_contains_secret_value(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SUPER-SECRET")
    monkeypatch.setenv("SAP_PASSWORD", "SAP-PASSWORD-SECRET")
    result = configuration_summary()
    assert "SUPER-SECRET" not in str(result)
    assert "SAP-PASSWORD-SECRET" not in str(result)


def test_invalid_ai_provider_is_reported_without_raising(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "unknown-provider")
    result = validate_configuration()
    assert result["valid"] is False
    assert result["invalid"]["AI_PROVIDER"] == "unsupported"


def test_invalid_port_is_reported(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "not-a-port")
    result = validate_configuration()
    assert result["invalid"]["SMTP_PORT"] == "not_integer"


def test_ai_missing_key_is_optional_configuration_state(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = validate_configuration()
    assert result["ai_provider"]["provider"] == "gemini"
    assert result["ai_provider"]["configured"] is False


def test_secret_redaction_filter_removes_password():
    record = logging.LogRecord(
        "test", logging.ERROR, __file__, 1,
        "password=super-secret GEMINI_API_KEY=another-secret", (), None
    )
    assert SecretRedactionFilter().filter(record)
    assert "super-secret" not in record.msg
    assert "another-secret" not in record.msg
    assert "[REDACTED]" in record.msg


def test_secret_redaction_does_not_break_non_string_arguments():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "value=%s", (123,), None
    )
    assert SecretRedactionFilter().filter(record)


def test_readiness_contains_safe_configuration_summary(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "DO-NOT-RETURN")
    result = health.readiness()
    assert "configuration" in result["checks"]
    assert "DO-NOT-RETURN" not in str(result)
