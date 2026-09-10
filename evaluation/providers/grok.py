"""
Grok (xAI) provider.

Same contract as GeminiProvider so the failover chain can hold both. The xAI
API is OpenAI-compatible, so this is a chat/completions call rather than
Gemini's generateContent shape.

Quota exhaustion is raised as QuotaExhausted so the chain can retire this key
for the rest of the run instead of retrying it on every cycle. Transient
failures (5xx, timeouts) are retried on the same key first -- moving to the
next provider on a blip would burn the fallback keys for nothing.
"""

from __future__ import annotations

import json
import os
import time

import requests
from dotenv import load_dotenv

from evaluation.providers.base import AIProvider
from utils.logger import get_logger

load_dotenv()

log = get_logger(__name__, "ai_provider")


class QuotaExhausted(RuntimeError):
    """Key is out of quota or rate-limited beyond retrying. Move to the next."""


class ProviderUnavailable(RuntimeError):
    """Transient failure. The chain may try the next provider."""


class GrokProvider(AIProvider):
    name = "grok"

    def __init__(
        self,
        model: str | None = None,
        timeout: int = 60,
        retries: int = 1,
        api_key: str | None = None,
        base_url: str | None = None,
        label: str | None = None,
    ):
        self.api_key = api_key or os.getenv("GROK_API_KEY")
        self.model = model or os.getenv("GROK_MODEL", "grok-4.3")
        self.base_url = (base_url or os.getenv("GROK_BASE_URL",
                                               "https://api.x.ai/v1")).rstrip("/")
        self.timeout = int(timeout)
        self.retries = max(0, int(retries))
        self.label = label or "grok"

        self.max_output_tokens = int(os.getenv("GROK_MAX_OUTPUT_TOKENS", "4096"))

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _payload(self, prompt: str) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": self.max_output_tokens,
        }
        # The RCA engine parses JSON. Asking for it explicitly avoids a prose
        # preamble that would fail json.loads downstream -- but some model
        # slugs reject the parameter outright with a 400, so it is dropped on
        # the retry rather than losing an otherwise-working key.
        if not getattr(self, "_no_json_mode", False):
            body["response_format"] = {"type": "json_object"}
        return body

    def generate(self, prompt: str) -> str:
        if not self.api_key:
            raise ProviderUnavailable(f"{self.label}: no API key configured")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                response = requests.post(
                    self._url(),
                    headers=headers,
                    json=self._payload(prompt),
                    timeout=self.timeout,
                )

                # Quota / rate limit. 429 can mean either "slow down" or "out
                # of credit"; the body distinguishes them where the provider
                # bothers to say. Treated as exhausted either way, because a
                # monitoring cycle cannot wait out a rate limit.
                if response.status_code in (429, 402):
                    raise QuotaExhausted(
                        f"{self.label}: HTTP {response.status_code} "
                        f"{response.text[:200]}")

                # Bad or revoked key -- no point retrying it this run.
                if response.status_code in (401, 403):
                    raise QuotaExhausted(
                        f"{self.label}: HTTP {response.status_code} "
                        f"(key rejected)")

                if response.status_code >= 500:
                    last_error = ProviderUnavailable(
                        f"{self.label}: HTTP {response.status_code}")
                    if attempt < self.retries:
                        time.sleep(2 * (attempt + 1))
                        continue
                    raise last_error

                # 400 is the one status where the BODY is the whole diagnosis:
                # xAI returns "model does not exist" (retired slug) and
                # "response_format not supported" through the same status
                # code. raise_for_status() discards it, which turned every
                # cause into the same useless "400 Bad Request for url".
                if response.status_code == 400:
                    body = response.text[:400]
                    # Some models reject the JSON-object response format.
                    # Retry once without it rather than losing the key.
                    if "response_format" in body and not getattr(self, "_no_json_mode", False):
                        log.info(f"{self.label}: model rejected response_format; "
                                 "retrying once in plain-text mode")
                        self._no_json_mode = True
                        continue
                    raise ProviderUnavailable(
                        f"{self.label}: HTTP 400 from xAI -- {body} "
                        f"(model='{self.model}'; set GROK_MODEL to a current "
                        f"slug if this says the model does not exist)")

                response.raise_for_status()
                data = response.json()

                choices = data.get("choices") or []
                if not choices:
                    raise ProviderUnavailable(f"{self.label}: empty response")

                content = (choices[0].get("message") or {}).get("content", "")
                if not content:
                    raise ProviderUnavailable(f"{self.label}: no content")

                return content

            except (QuotaExhausted, ProviderUnavailable):
                raise
            except requests.RequestException as exc:
                last_error = ProviderUnavailable(f"{self.label}: {exc}")
                if attempt < self.retries:
                    time.sleep(2 * (attempt + 1))
                    continue
            except json.JSONDecodeError as exc:
                raise ProviderUnavailable(f"{self.label}: bad JSON: {exc}")

        raise last_error or ProviderUnavailable(f"{self.label}: failed")
