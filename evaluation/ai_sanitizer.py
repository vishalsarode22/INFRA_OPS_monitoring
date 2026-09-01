"""Sanitize monitoring context before it reaches an external LLM."""

from __future__ import annotations

import re
from typing import Any


SECRET_KEY_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_ -]?key|token|"
    r"access[_ -]?token|refresh[_ -]?token|authorization|private[_ -]?key)\b"
    r"\s*[:=]\s*([^\s,;]+)"
)

BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")

SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|secret|api[_ -]?key|token|authorization)"
    r"(\s*[:=]\s*)([^,\s;]+)"
)


def sanitize_text(text: str) -> str:
    """Remove common secret/token patterns while preserving useful context."""
    text = str(text or "")
    text = BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = SECRET_KEY_PATTERN.sub(r"\1=[REDACTED]", text)
    text = SENSITIVE_VALUE_PATTERN.sub(r"\1=[REDACTED]", text)
    return text


def sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {str(k): sanitize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_value(v) for v in value)
    return value
