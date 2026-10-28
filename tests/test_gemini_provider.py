import os
from unittest.mock import Mock, patch

import requests

from evaluation.providers.gemini import GeminiProvider


def test_gemini_key_is_not_put_in_url():
    provider = GeminiProvider(model="test-model", api_version="v1")
    response = Mock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "{}"}]}}]
    }

    with patch.dict(os.environ, {"GEMINI_API_KEY": "SECRET-DO-NOT-LOG"}, clear=False):
        with patch("evaluation.providers.gemini.requests.post", return_value=response) as post:
            provider.generate("hello")

    url = post.call_args.args[0]
    headers = post.call_args.kwargs["headers"]
    assert "SECRET-DO-NOT-LOG" not in url
    assert headers["x-goog-api-key"] == "SECRET-DO-NOT-LOG"


def test_transient_503_is_retryable_without_exposing_secret():
    provider = GeminiProvider(model="test-model", retries=1)
    failed = Mock()
    failed.status_code = 503
    failed.raise_for_status.side_effect = requests.HTTPError("503")
    failed.json.return_value = {}

    success = Mock()
    success.status_code = 200
    success.raise_for_status.return_value = None
    success.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "{}"}]}}]
    }

    with patch.dict(os.environ, {"GEMINI_API_KEY": "SECRET"}, clear=False):
        with patch(
            "evaluation.providers.gemini.requests.post",
            side_effect=[failed, success],
        ) as post:
            assert provider.generate("hello") == "{}"
            assert post.call_count == 2


def test_404_has_actionable_model_message():
    provider = GeminiProvider(model="missing-model")
    response = Mock()
    response.status_code = 404
    response.raise_for_status.side_effect = requests.HTTPError("404")

    with patch.dict(os.environ, {"GEMINI_API_KEY": "SECRET"}, clear=False):
        with patch("evaluation.providers.gemini.requests.post", return_value=response):
            try:
                provider.generate("hello")
            except RuntimeError as exc:
                message = str(exc)
                assert "GEMINI_MODEL" in message
                assert "GEMINI_API_VERSION" in message
            else:
                raise AssertionError("Expected a RuntimeError")
