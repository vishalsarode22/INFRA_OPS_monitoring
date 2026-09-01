"""SAP GUI reliability policy and exception classification."""

from dataclasses import dataclass
import time


class SapGuiReliabilityError(RuntimeError):
    """Base class for SAP GUI reliability errors."""


class SapGuiSessionLost(SapGuiReliabilityError):
    """The SAP GUI scripting session is no longer usable."""


class SapGuiApplicationLost(SapGuiReliabilityError):
    """SAP Logon/GUI application is no longer available."""


class SapGuiNonRetryableError(SapGuiReliabilityError):
    """The operation is not expected to succeed by retrying."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 15.0

    def delay_before_retry(self, failed_attempt: int) -> float:
        if failed_attempt < 1:
            return 0.0
        return min(
            self.max_delay_seconds,
            self.initial_delay_seconds * (self.backoff_multiplier ** (failed_attempt - 1)),
        )

    def sleep_before_retry(self, failed_attempt: int) -> None:
        delay = self.delay_before_retry(failed_attempt)
        if delay > 0:
            time.sleep(delay)


DEFAULT_TCODE_POLICY = RetryPolicy(max_attempts=3)
DEFAULT_SYSTEM_POLICY = RetryPolicy(
    max_attempts=2,
    initial_delay_seconds=5.0,
    backoff_multiplier=2.0,
    max_delay_seconds=30.0,
)


def classify_gui_exception(exc: Exception) -> str:
    """Classify an automation failure without exposing credentials."""
    text = str(exc).lower()

    non_retryable_markers = (
        "invalid credentials", "invalid password", "user locked",
        "user expired", "not authorized", "authorization",
        "does not exist", "unknown transaction", "t-code does not exist",
        "no action registered",
    )
    if any(marker in text for marker in non_retryable_markers):
        return "non_retryable"

    session_markers = (
        "session", "scripting engine", "sapgui", "com error",
        "rpc server", "disconnected", "object required", "invalid window",
        "element not found", "wnd[0]",
    )
    if any(marker in text for marker in session_markers):
        return "session"

    return "transient"


def is_non_retryable(exc: Exception) -> bool:
    return classify_gui_exception(exc) == "non_retryable"


def is_session_failure(exc: Exception) -> bool:
    return classify_gui_exception(exc) == "session"
