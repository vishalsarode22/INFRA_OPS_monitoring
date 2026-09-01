"""Google Gemini provider with safe key handling and configurable model/version."""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv

load_dotenv()

import requests

from evaluation.providers.base import AIProvider
from utils.logger import get_logger


log = get_logger(__name__, "ai_provider")


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(
        self,
        model: str | None = None,
        timeout: int = 60,
        retries: int = 1,
        api_version: str | None = None,
    ):
        self.model = model or os.getenv(
            "GEMINI_MODEL",
            "gemini-3.6-flash",
        )

        self.api_version = api_version or os.getenv(
            "GEMINI_API_VERSION",
            "v1beta",
        )

        self.timeout = int(timeout)
        self.retries = max(0, int(retries))

        # Gemini 3.x uses thinking_level rather than thinking_budget.
        # LOW is appropriate for structured monitoring/RCA responses:
        # enough reasoning, lower latency, and less chance of spending
        # the output budget on internal reasoning.
        self.thinking_level = os.getenv(
            "GEMINI_THINKING_LEVEL",
            "low",
        ).strip().lower()

        if self.thinking_level not in {
            "minimal",
            "low",
            "medium",
            "high",
        }:
            self.thinking_level = "low"

        self.max_output_tokens = int(
            os.getenv(
                "GEMINI_MAX_OUTPUT_TOKENS",
                "4096",
            )
        )

    def _url(self) -> str:
        return (
            "https://generativelanguage.googleapis.com/"
            f"{self.api_version}/models/{self.model}:generateContent"
        )

    def _payload(self, prompt: str) -> dict:
        return {
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt,
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": self.max_output_tokens,
                "responseMimeType": "application/json",
                "thinkingConfig": {
                    "thinkingLevel": self.thinking_level,
                },
            },
        }

    def generate(self, prompt: str) -> str:
        api_key = os.getenv("GEMINI_API_KEY")

        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured."
            )

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }

        payload = self._payload(prompt)

        last_error = None

        for attempt in range(self.retries + 1):

            try:
                response = requests.post(
                    self._url(),
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )

                # ------------------------------------------------------
                # Transient provider failures
                # ------------------------------------------------------

                if response.status_code in {
                    429,
                    500,
                    502,
                    503,
                    504,
                }:

                    last_error = RuntimeError(
                        f"Gemini transient HTTP "
                        f"{response.status_code}"
                    )

                    if attempt < self.retries:

                        time.sleep(
                            1.5 * (attempt + 1)
                        )

                        log.warning(
                            "Gemini transient HTTP %s; retrying.",
                            response.status_code,
                        )

                        continue

                # ------------------------------------------------------
                # Authentication / permission
                # ------------------------------------------------------

                if response.status_code in {
                    401,
                    403,
                }:

                    raise RuntimeError(
                        "Gemini authentication/permission failed "
                        f"(HTTP {response.status_code}). "
                        "Check the API key and project."
                    )

                # ------------------------------------------------------
                # Model / endpoint problems
                # ------------------------------------------------------

                if response.status_code >= 400:
                    try:
                        error_data = response.json()
                        error_message = (
                            error_data.get("error", {}).get("message")
                            or response.text[:1000]
                        )
                    except (ValueError, TypeError):
                        error_message = response.text[:1000]

                    raise RuntimeError(
                        f"Gemini HTTP {response.status_code}: {error_message}"
                    )

                response.raise_for_status()

                data = response.json()

                candidates = data.get(
                    "candidates",
                    [],
                )

                if not candidates:
                    raise RuntimeError(
                        "Gemini returned no candidates."
                    )

                candidate = candidates[0]

                finish_reason = candidate.get(
                    "finishReason",
                    "",
                )

                content = candidate.get(
                    "content",
                    {},
                )

                parts = content.get(
                    "parts",
                    [],
                )

                text_parts = []

                for part in parts:

                    if not isinstance(part, dict):
                        continue

                    # Gemini 3 can return thought parts/signatures.
                    # Only collect normal model text.
                    if part.get("thought", False):
                        continue

                    text = part.get(
                        "text",
                        "",
                    )

                    if text:
                        text_parts.append(text)

                text = "".join(
                    text_parts
                ).strip()

                if not text:

                    raise RuntimeError(
                        "Gemini returned no usable text."
                    )

                # ------------------------------------------------------
                # IMPORTANT:
                #
                # If Gemini stopped because it reached the output
                # token limit, the JSON can be incomplete. Returning
                # that text causes the schema layer to report:
                #
                #   Unterminated string
                #
                # Treat it as a provider failure instead.
                # ------------------------------------------------------

                if finish_reason == "MAX_TOKENS":

                    log.warning(
                        "Gemini response reached MAX_TOKENS "
                        "(model=%s, max_output_tokens=%s).",
                        self.model,
                        self.max_output_tokens,
                    )

                    raise RuntimeError(
                        "Gemini response was truncated "
                        "because MAX_OUTPUT_TOKENS was reached."
                    )

                return text

            except requests.exceptions.RequestException as exc:

                last_error = exc

                status = getattr(
                    exc.response,
                    "status_code",
                    None,
                )

                if (
                    attempt < self.retries
                    and status in {
                        429,
                        500,
                        502,
                        503,
                        504,
                    }
                ):

                    time.sleep(
                        1.5 * (attempt + 1)
                    )

                    log.warning(
                        "Gemini request failed with HTTP %s; retrying.",
                        status,
                    )

                    continue

                raise RuntimeError(
                    f"Gemini request failed: "
                    f"{type(exc).__name__}"
                ) from exc

            except (
                KeyError,
                IndexError,
                TypeError,
                ValueError,
            ) as exc:

                raise RuntimeError(
                    "Unexpected Gemini response shape."
                ) from exc

        raise RuntimeError(
            str(
                last_error
                or "Gemini request failed."
            )
        )