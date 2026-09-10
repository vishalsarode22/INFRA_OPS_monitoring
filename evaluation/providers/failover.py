"""
Failover across several API keys and vendors.

Tries each provider in order and moves to the next when a key is exhausted.
Order is configured, not guessed, so a cheap key is used before an expensive
one and Gemini before Grok (or the reverse) is a config change rather than a
code change.

TWO KINDS OF FAILURE, HANDLED DIFFERENTLY -- this is the point of the module:

  Quota exhausted (429, 402, 401, 403)
      The key is finished for this run. It is retired: no later cycle in the
      same process tries it again. Retrying an exhausted key on every
      monitoring cycle wastes the timeout budget and, on some providers,
      counts as further failed requests.

  Transient (5xx, timeout, connection reset)
      The provider is still viable. The provider retries internally first;
      only if that fails does the chain move on, and the provider stays in
      rotation for the next cycle.

If every provider is exhausted, generate() raises. It does NOT return an
empty string or a placeholder: the RCA layer must be able to tell "the model
said nothing" from "we never asked", and a silent empty narrative in an alert
would read as "nothing to report".

Configuration, in .env:

    AI_PROVIDER_CHAIN=gemini:GEMINI_API_KEY,gemini:GEMINI_API_KEY_2,\\
                      grok:GROK_API_KEY,grok:GROK_API_KEY_2

Each entry is vendor:ENV_VAR_HOLDING_THE_KEY. Entries whose variable is
missing or blank are skipped at construction with a log line, so a chain can
be written once and used on machines that hold only some of the keys.
"""

from __future__ import annotations

import os

from evaluation.providers.base import AIProvider
from utils.logger import get_logger

log = get_logger(__name__, "ai_provider")

_DEFAULT_CHAIN = (
    "gemini:GEMINI_API_KEY,"
    "gemini:GEMINI_API_KEY_2,"
    "grok:GROK_API_KEY,"
    "grok:GROK_API_KEY_2"
)


def _quota_exhausted_types():
    from evaluation.providers.grok import QuotaExhausted
    return QuotaExhausted


def _looks_like_quota(exc: Exception) -> bool:
    """
    Gemini raises plain RuntimeError rather than a typed quota error, so the
    message is inspected. Deliberately narrow: anything not clearly a quota
    or auth problem is treated as transient, which keeps the key in rotation.
    Wrongly retiring a good key is worse than one wasted retry.
    """
    from evaluation.providers.grok import QuotaExhausted
    if isinstance(exc, QuotaExhausted):
        return True
    text = str(exc).lower()
    markers = ("429", "quota", "rate limit", "rate_limit", "exhausted",
               "resource_exhausted", "insufficient", "billing",
               "401", "403", "permission_denied", "api key not valid")
    return any(m in text for m in markers)


def build_chain(spec: str | None = None) -> list[AIProvider]:
    """Constructs the provider list from the chain spec. Never raises."""
    from evaluation.providers.gemini import GeminiProvider
    from evaluation.providers.grok import GrokProvider

    spec = spec or os.getenv("AI_PROVIDER_CHAIN", _DEFAULT_CHAIN)
    providers: list[AIProvider] = []

    for index, raw in enumerate(spec.split(","), start=1):
        entry = raw.strip()
        if not entry:
            continue

        if ":" in entry:
            vendor, env_var = (part.strip() for part in entry.split(":", 1))
        else:
            vendor, env_var = entry, ""

        vendor = vendor.lower()
        key = os.getenv(env_var, "").strip() if env_var else ""

        if env_var and not key:
            log.info(f"AI chain: skipping {vendor} slot {index} "
                     f"-- {env_var} not set")
            continue

        label = f"{vendor}#{index}"

        try:
            if vendor == "gemini":
                providers.append(GeminiProvider(api_key=key or None, label=label))
            elif vendor == "grok":
                providers.append(GrokProvider(api_key=key or None, label=label))
            else:
                log.warning(f"AI chain: unknown vendor '{vendor}' -- skipped")
                continue
        except Exception as exc:
            log.warning(f"AI chain: {label} could not be constructed: {exc}")
            continue

        log.info(f"AI chain: {label} ready ({env_var or 'env default'})")

    return providers


class FailoverProvider(AIProvider):
    name = "failover"

    def __init__(self, providers: list[AIProvider] | None = None,
                 spec: str | None = None):
        self.providers = providers if providers is not None else build_chain(spec)
        # Retired for the lifetime of this process, by label.
        self._exhausted: set[str] = set()

        if not self.providers:
            log.warning("AI chain: no providers configured -- "
                        "check AI_PROVIDER_CHAIN and the key variables in .env")

    def _label(self, provider: AIProvider) -> str:
        return getattr(provider, "label", None) or getattr(provider, "name", "?")

    @property
    def available(self) -> list[AIProvider]:
        return [p for p in self.providers
                if self._label(p) not in self._exhausted]

    def status(self) -> dict:
        """For logging and the dashboard: which keys are still usable."""
        return {
            "configured": [self._label(p) for p in self.providers],
            "exhausted": sorted(self._exhausted),
            "available": [self._label(p) for p in self.available],
        }

    def generate(self, prompt: str) -> str:
        candidates = self.available

        if not candidates:
            raise RuntimeError(
                "AI chain: every configured key is exhausted or unusable "
                f"({len(self._exhausted)} retired). "
                "Deterministic findings are unaffected; only the narrative "
                "paragraph is missing."
            )

        errors: list[str] = []

        for provider in candidates:
            label = self._label(provider)
            try:
                text = provider.generate(prompt)
                if text:
                    if errors:
                        log.info(f"AI chain: {label} answered after "
                                 f"{len(errors)} failure(s)")
                    return text
                errors.append(f"{label}: empty response")

            except Exception as exc:
                if _looks_like_quota(exc):
                    self._exhausted.add(label)
                    log.warning(f"AI chain: {label} exhausted, retiring it "
                                f"for this run -- {exc}")
                else:
                    log.warning(f"AI chain: {label} failed (transient, "
                                f"kept in rotation) -- {exc}")
                errors.append(f"{label}: {exc}")
                continue

        raise RuntimeError("AI chain: all providers failed -- "
                           + " | ".join(errors))
