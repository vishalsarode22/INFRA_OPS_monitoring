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

import json
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


def _next_daily_reset() -> datetime:
    """
    When a per-day Gemini quota is worth trying again. Google resets the
    free-tier daily quotas at midnight Pacific time, i.e. 08:00 UTC in winter
    (07:00 in summer). 08:00 UTC is always after the reset, without needing a
    time-zone database (not present on a plain Windows Python).
    IBO_GEMINI_DAILY_RESET_UTC_HOUR overrides the hour.
    """
    try:
        hour = int(os.getenv("IBO_GEMINI_DAILY_RESET_UTC_HOUR", "8") or 8) % 24
    except ValueError:
        hour = 8
    now = datetime.now(timezone.utc)
    reset = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return reset if reset > now else reset + timedelta(days=1)


def _quota_block(api_key: str, seconds: float | None = None) -> None:
    """Pause a key: until the daily reset (seconds None) or for `seconds` (per-minute limit)."""
    until = (_next_daily_reset() if seconds is None
             else datetime.now(timezone.utc) + timedelta(seconds=max(1.0, float(seconds))))
    with _quota_lock:
        _quota_blocks[_key_id(api_key)] = until


def _parse_delay(text: str | None) -> float | None:
    """'37s' / '1.5s' / '30' -> seconds."""
    if not text:
        return None
    import re
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*s?\s*$", str(text))
    return float(m.group(1)) if m else None


def _quota_window(body: str, retry_after: str | None) -> tuple[str, float]:
    """
    ("day", 0) for a daily quota, ("minute", seconds to wait) otherwise.

    Google's 429 body names the quota that ran out
    (error.details[].violations[].quotaId, e.g.
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier" or "...PerMinute...")
    and how long to wait (RetryInfo.retryDelay "37s"). Only a quota that says
    per DAY retires the key until the daily reset; a per-minute limit -- or a
    429 that says nothing -- pauses it for ~60 s. Treating every "quota" 429
    as the daily limit retired keys for the whole day after one busy minute.
    """
    names, delay = [], None
    try:
        data = json.loads(body or "{}")
        for d in ((data.get("error") or {}).get("details") or []):
            t = str(d.get("@type") or "")
            if t.endswith("QuotaFailure"):
                for v in d.get("violations") or []:
                    names.append(f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}")
            elif t.endswith("RetryInfo"):
                delay = _parse_delay(d.get("retryDelay"))
    except (ValueError, AttributeError, TypeError):
        pass
    text = (" ".join(names) + " " + (body or "")).lower()
    if "perday" in text.replace("_", "").replace(" ", "") or "per day" in text or "daily" in text:
        return "day", 0.0
    if delay is None:
        delay = _parse_delay(retry_after)
    try:
        floor = float(os.getenv("IBO_GEMINI_MINUTE_BACKOFF_S", "60") or 60)
    except ValueError:
        floor = 60.0
    return "minute", max(floor, delay or 0.0)


# Client-side rate limiter: never send more than IBO_GEMINI_RPM requests a
# minute from this process (default 8; the free tier allows about 10 for the
# Flash models). A request over the limit waits up to 2 s for a slot, else is
# refused locally (QuotaExhausted) and the caller falls back to the rules text.
_sent_at: list[float] = []
_rate_lock = threading.Lock()


def _rpm() -> int:
    try:
        return max(1, int(os.getenv("IBO_GEMINI_RPM", "8") or 8))
    except ValueError:
        return 8


def _rate_gate() -> None:
    while True:
        with _rate_lock:
            now = time.monotonic()
            del _sent_at[:len([t for t in _sent_at if now - t >= 60])]
            if len(_sent_at) < _rpm():
                return
            wait = 60 - (now - _sent_at[0])
        if wait > 2:
            raise QuotaExhausted(f"local Gemini limit of {_rpm()} requests/minute reached "
                                 f"(IBO_GEMINI_RPM); next slot in {wait:.0f}s")
        time.sleep(max(0.05, wait))


def _rate_note() -> None:
    """A request reached Google (it counts against the per-minute quota)."""
    with _rate_lock:
        _sent_at.append(time.monotonic())


def reset_rate_limiter() -> None:
    with _rate_lock:
        _sent_at.clear()


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
                f"Gemini key paused after a quota answer; skipping for another "
                f"{int(_blocked // 60)} min {int(_blocked % 60)} s."
            )
        _rate_gate()

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

                _rate_note()
                if response.status_code == 429:

                    full_body = (response.text or "")[:6000]
                    body = full_body[:400].lower()
                    retry_after = response.headers.get("Retry-After")

                    # A quota answer. Retrying now cannot help: pause the key
                    # (until the daily reset for a per-day quota, ~60 s for a
                    # per-minute one) and let the chain move on.
                    if _is_quota_429(body, retry_after):
                        window, secs = _quota_window(full_body, retry_after)
                        _quota_block(api_key, None if window == "day" else secs)
                        raise QuotaExhausted(
                            "Gemini daily quota exhausted (HTTP 429). Key paused "
                            "until the daily reset." if window == "day" else
                            f"Gemini per-minute limit (HTTP 429). Key paused {secs:.0f} s."
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
                    _resp = getattr(exc, "response", None)
                    window, secs = _quota_window(
                        (getattr(_resp, "text", "") or "")[:6000],
                        (getattr(_resp, "headers", None) or {}).get("Retry-After"))
                    _quota_block(api_key, None if window == "day" else secs)
                    raise QuotaExhausted(
                        "Gemini daily quota on a failed request (HTTP 429). Key paused "
                        "until the daily reset." if window == "day" else
                        f"Gemini per-minute limit on a failed request (HTTP 429). "
                        f"Key paused {secs:.0f} s."
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