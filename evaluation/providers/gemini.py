"""Google Gemini provider with safe key handling and configurable model/version.

QUOTA IS NOT A TRANSIENT FAILURE
--------------------------------
This module used to lump HTTP 429 in with 500/502/503/504 and sleep-and-retry
it. A 429 caused by a *daily quota* will still be a 429 in 4.5 seconds, so each
key burned its full retry budget before the failover chain even saw the error --
and then the next key did the same. Measured over four days of this project's
logs: 185 chains failed completely, median 17.4s, max 293.4s, 94.7 minutes total
spent on calls that returned nothing.

Two changes fix it:
  1. A quota 429 raises QuotaExhausted immediately, with no retry. The failover
     chain already knows to retire a provider on that type (see
     failover._looks_like_quota) -- it simply never got the chance.
  2. An exhausted key is remembered process-wide until the next UTC midnight
     (when Google's daily quotas reset), so the second, third and fourth system
     in a sweep skip it instead of rediscovering it at full cost.

A 429 that is a genuine short-term rate limit (Retry-After present, or no quota
wording in the body) is still retried, once, honouring Retry-After.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

import requests

from evaluation.providers.base import AIProvider
from evaluation.providers.grok import QuotaExhausted
from utils.logger import get_logger


log = get_logger(__name__, "ai_provider")


# ---------------------------------------------------------------------------
# Quota bookkeeping
#
# Google's free-tier quotas reset at UTC midnight, so an exhausted key is dead
# for a known, bounded period rather than indefinitely. Keys are held by hash,
# never in plaintext, so a log line or a crash dump cannot leak one.
# ---------------------------------------------------------------------------

_quota_blocks: dict[str, datetime] = {}
_quota_lock = threading.Lock()

_QUOTA_MARKERS = (
    "quota", "exceeded", "resource_exhausted", "resource has been exhausted",
    "billing", "free tier", "daily limit",
)


def _key_id(api_key: str) -> str:
    """A stable, non-reversible handle for a key, for use as a dict key."""
    import hashlib
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


def _next_utc_midnight() -> datetime:
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)


def _quota_block(api_key: str) -> None:
    with _quota_lock:
        _quota_blocks[_key_id(api_key)] = _next_utc_midnight()


def _quota_block_remaining(api_key: str) -> float:
    """Seconds until this key is worth trying again; 0.0 if it is usable now."""
    with _quota_lock:
        until = _quota_blocks.get(_key_id(api_key))
    if not until:
        return 0.0
    left = (until - datetime.now(timezone.utc)).total_seconds()
    if left <= 0:
        with _quota_lock:
            _quota_blocks.pop(_key_id(api_key), None)
        return 0.0
    return left


def reset_quota_blocks() -> None:
    """Clear all quota blocks -- call after rotating keys, and from tests."""
    with _quota_lock:
        _quota_blocks.clear()


def _is_quota_429(body: str, retry_after: str | None) -> bool:
    """
    Distinguish 'you are out of quota for today' from 'you are going too fast'.

    Deliberately biased toward treating an ambiguous 429 as a quota error: the
    cost of wrongly retiring a key for a few hours is one degraded AI summary,
    while the cost of wrongly retrying is the 95 minutes of dead waiting this
    change exists to remove. A server that sends Retry-After is telling us it
    expects to recover, so that is the signal that flips it back.
    """
    if any(marker in body for marker in _QUOTA_MARKERS):
        return True
    return retry_after is None


def _retry_after_seconds(retry_after: str | None, default: float) -> float:
    try:
        return max(0.0, min(30.0, float(retry_after)))
    except (TypeError, ValueError):
        return default


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(
        self,
        model: str | None = None,
        timeout: int = 60,
        retries: int = 1,
        api_version: str | None = None,
        api_key: str | None = None,
        label: str | None = None,
    ):
        # The key is held on the instance so several GeminiProvider objects,
        # each with a different key, can sit in a failover chain. Only an
        # EXPLICIT key is stored here; the single-key environment fallback is
        # resolved in generate(), at call time. Resolving it at construction
        # froze whatever .env held when the object was built, which meant a
        # real key from the developer's .env silently overrode the value a
        # test had patched into the environment -- and then appeared in the
        # assertion output. Call-time resolution keeps the fallback and makes
        # the environment authoritative when no explicit key was given.
        self.api_key = api_key
        self.label = label or "gemini"

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
        api_key = self.api_key or os.getenv("GEMINI_API_KEY")

        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured."
            )

        # Skip a key already known to be out of quota for the rest of the day.
        # Without this, every system in the sweep pays the full discovery cost
        # again -- four systems x four keys x the retry budget.
        _blocked = _quota_block_remaining(api_key)
        if _blocked:
            raise QuotaExhausted(
                f"Gemini key quota exhausted; skipping for another "
                f"{int(_blocked // 60)} min (resets at UTC midnight)."
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

                if response.status_code == 429:

                    body = (response.text or "")[:400].lower()
                    retry_after = response.headers.get("Retry-After")

                    # A daily/project quota. Retrying cannot help -- retire the
                    # key for the rest of the day and let the chain move on.
                    if _is_quota_429(body, retry_after):
                        _quota_block(api_key)
                        raise QuotaExhausted(
                            "Gemini quota exhausted (HTTP 429). Key retired "
                            "until UTC midnight."
                        )

                    # A genuine per-minute rate limit. Honour Retry-After if the
                    # server sent one rather than guessing, and only once.
                    last_error = RuntimeError("Gemini rate-limited (HTTP 429)")
                    if attempt < self.retries:
                        wait = _retry_after_seconds(retry_after, default=1.5 * (attempt + 1))
                        log.warning(
                            "Gemini rate-limited (HTTP 429); retrying in %.1fs.",
                            wait,
                        )
                        time.sleep(wait)
                        continue

                if response.status_code in {
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

                if response.status_code == 404:
                    # Almost always a model name / API version pairing that
                    # this endpoint does not serve (models are retired, and
                    # v1 vs v1beta expose different sets). Name the two
                    # settings that fix it rather than echoing the raw body.
                    raise RuntimeError(
                        f"Gemini HTTP 404: model '{self.model}' was not found "
                        f"under API version '{self.api_version}'. Check "
                        f"GEMINI_MODEL and GEMINI_API_VERSION in .env -- the "
                        f"model may be retired or only available under a "
                        f"different API version."
                    )

                if response.status_code >= 400:
                    try:
                        error_data = response.json()
                        error_message = (
                            error_data.get("error", {}).get("message")
                            or response.text[:1000]
                        )
                    except (ValueError, TypeError):
                        error_message = response.text[:1000]
                    if not isinstance(error_message, str):
                        error_message = response.text[:1000] if isinstance(
                            response.text, str) else f"HTTP {response.status_code}"

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

                if status == 429:
                    _quota_block(api_key)
                    raise QuotaExhausted(
                        "Gemini quota/rate limit on a failed request "
                        "(HTTP 429). Key retired until UTC midnight."
                    ) from exc

                if (
                    attempt < self.retries
                    and status in {
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