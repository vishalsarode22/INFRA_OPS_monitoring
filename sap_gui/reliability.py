"""SAP GUI reliability policy and exception classification."""

from contextlib import contextmanager
from dataclasses import dataclass
import functools
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


# COM / RPC results that mean the SAP GUI session (or SAP Logon itself) is
# gone, not that one screen misbehaved. Seen in production: -2147417848
# "The object invoked has disconnected from its clients" after a session was
# closed under the sweep; -2147417851 "The server threw an exception" when
# saplogon.exe crashed mid-call.
SESSION_LOST_HRESULTS = frozenset({
    -2147417848,   # 0x80010108 RPC_E_DISCONNECTED
    -2147417851,   # 0x80010105 RPC_E_SERVERFAULT
    -2147418105,   # 0x80010007 RPC_E_SERVER_DIED
    -2147418094,   # 0x80010012 RPC_E_SERVER_DIED_DNE
    -2147023174,   # 0x800706BA RPC_S_SERVER_UNAVAILABLE
    -2147023170,   # 0x800706BE RPC_S_CALL_FAILED
    -2147220995,   # 0x800401FD CO_E_OBJNOTCONNECTED
})

_SESSION_LOST_TEXT = (
    "object invoked has disconnected", "rpc server is unavailable",
    "remote procedure call failed", "server threw an exception",
    "object is not connected to the server",
)


def _hresult(exc: BaseException):
    """The COM hresult of a pywintypes.com_error (or None)."""
    value = getattr(exc, "hresult", None)
    if value is None and getattr(exc, "args", None):
        value = exc.args[0]
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value > 0x7FFFFFFF:          # unsigned spelling of the same code
        value -= 1 << 32
    return value


def is_session_lost(exc: BaseException) -> bool:
    """
    True only when the SAP GUI session itself is gone (disconnected COM
    object, dead SAP Logon). Narrower than is_session_failure(): a missing
    control id is NOT a lost session. Used where an error must never be
    swallowed, because the caller's recovery depends on seeing it.
    """
    if isinstance(exc, (SapGuiSessionLost, SapGuiApplicationLost)):
        return True
    if _hresult(exc) in SESSION_LOST_HRESULTS:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _SESSION_LOST_TEXT)


@contextmanager
def com_thread():
    """
    CoInitialize for the current thread, CoUninitialize on exit.

    Every thread that touches SAP GUI scripting (win32com GetObject, session
    objects) must initialise COM itself; only the main thread gets it for
    free when pythoncom is imported. Sweep workers and the RCA thread did
    not, so GetObject("SAPGUI") failed there with "CoInitialize has not been
    called". Nested use is fine (COM counts the calls). No-op where
    pythoncom is unavailable (non-Windows / tests).
    """
    initialized = False
    pythoncom = None
    try:
        import pythoncom  # type: ignore
        pythoncom.CoInitialize()
        initialized = True
    except Exception:  # noqa: BLE001 -- no COM here: nothing to initialise
        pass
    try:
        yield
    finally:
        if initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


def com_thread_entry(fn):
    """Decorator form of com_thread() for thread target functions."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with com_thread():
            return fn(*args, **kwargs)
    return wrapper


def classify_gui_exception(exc: Exception) -> str:
    """Classify an automation failure without exposing credentials."""
    if is_session_lost(exc):
        return "session"
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
