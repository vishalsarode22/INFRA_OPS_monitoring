"""
SAP GUI login automation.

Exact required sequence:

    UP
    -> Ctrl+A
    -> Client
    -> TAB
    -> User
    -> TAB
    -> Password
    -> ENTER
"""

import re
import time

from pywinauto import Desktop

from utils.logger import get_logger


log = get_logger(__name__, "application")


# The SAP GUI logon screen's window title is not stable across releases,
# patch levels and languages. Seen in the wild: "SAP", "SAP R/3",
# "SAP Easy Access", and titles carrying the system description.
# Matching only the exact string "SAP" is why login timed out on a machine
# where the connection had opened perfectly well.
_LOGIN_TITLE_PATTERNS = [
    r"^SAP$",
    r"^SAP\s*R/3",
    r"^SAP\s+Easy\s+Access",
    r"^SAP\b.*",           # last resort: any top-level window starting "SAP"
]

# Windows that are NOT the logon screen and must never be matched.
_LOGIN_TITLE_EXCLUDE = re.compile(
    r"SAP Logon( \d+)?$|SAP Logon Pad|InfraBeat", re.IGNORECASE
)


def _visible_window_titles() -> list[str]:
    """Top-level window titles, for diagnosing a failed match."""
    titles = []
    try:
        for win in Desktop(backend="win32").windows():
            try:
                text = (win.window_text() or "").strip()
            except Exception:
                continue
            if text:
                titles.append(text)
    except Exception:
        pass
    return titles


# A window that means we are ALREADY logged on -- no logon screen will
# appear. Some connections auto-authenticate via saved credentials or SSO.
_ALREADY_LOGGED_IN = re.compile(
    r"Easy\s*Access|SAP Menu|^SAP.*\(\d{3}\)", re.IGNORECASE
)


def is_logged_in_window(window) -> bool:
    """True when the window is a live SAP session rather than a logon screen."""
    try:
        title = (window.window_text() or "").strip()
    except Exception:
        return False
    return bool(_ALREADY_LOGGED_IN.search(title))


def find_login_window(timeout: int = 30):
    """
    Find and focus the SAP login window.

    Tries progressively looser title patterns rather than one exact string,
    and on failure reports the window titles that WERE present -- turning
    "TimeoutError: timed out" into something actionable.
    """
    log.info("Finding SAP login window...")

    deadline = time.time() + timeout
    seen: set[str] = set()

    while time.time() < deadline:
        desktop = Desktop(backend="win32")

        # Record what is on screen, for the error message if we give up.
        try:
            for win in desktop.windows():
                try:
                    title = (win.window_text() or "").strip()
                except Exception:
                    continue
                if title:
                    seen.add(title)
        except Exception:
            pass

        for pattern in _LOGIN_TITLE_PATTERNS:
            try:
                window = desktop.window(title_re=pattern, visible_only=True)
                title = (window.window_text() or "").strip()
                if _LOGIN_TITLE_EXCLUDE.search(title):
                    continue          # that is the Logon Pad, not the logon screen
                window.wait("visible", timeout=2)
                log.info(f"SAP login window found: '{title}' (pattern {pattern!r}).")
                time.sleep(1)
                try:
                    window.set_focus()
                except Exception as exc:
                    # A window that cannot be focused is usually a locked or
                    # disconnected desktop -- GUI scripting cannot work there.
                    log.warning(f"Could not focus the SAP window: {exc}")
                return window
            except Exception:
                continue

        time.sleep(1)

    candidates = ", ".join(f"'{t}'" for t in sorted(seen)) or "none detected"
    raise TimeoutError(
        f"SAP login window did not appear within {timeout}s. "
        f"Visible windows were: {candidates}. "
        f"If a SAP window IS listed, add its title pattern to "
        f"_LOGIN_TITLE_PATTERNS in sap_gui/connection.py. If none is listed, "
        f"the desktop is probably locked or the RDP session is disconnected -- "
        f"GUI scripting drives a real desktop and cannot run without one."
    )

def fill_login_fields(
    login_window,
    client: str,
    username: str,
    password: str,
    language: str = "EN",
):
    """
    Populate the SAP login fields.

    Login submission is intentionally handled separately by
    submit_login() so ENTER is pressed exactly once.
    Language is intentionally not touched.
    """

    # ---------------------------------------------------------------
    # Move to CLIENT field
    # ---------------------------------------------------------------

    log.info("Moving to Client field...")

    login_window.type_keys("{UP}")
    time.sleep(0.5)

    log.info("Clearing existing Client value...")

    login_window.type_keys("{DELETE}")
    login_window.type_keys("{DELETE}")
    login_window.type_keys("{DELETE}")

    time.sleep(0.3)

    log.info(f"Entering Client: {client}")

    login_window.type_keys(
        str(client),
        with_spaces=True,
    )

    time.sleep(0.5)

    login_window.type_keys("{TAB}")

    time.sleep(0.3)

    log.info(f"Entering Username: {username}")

    login_window.type_keys(
        str(username),
        with_spaces=True,
    )

    time.sleep(0.5)

    login_window.type_keys("{TAB}")

    time.sleep(0.3)

    log.info("Entering Password... (masked)")

    login_window.type_keys(
        str(password),
        with_spaces=True,
    )

    time.sleep(0.5)

    log.info("Client, username and password populated.")

def submit_login(login_window):
    """
    Submit login with ENTER.

    The language field is intentionally not modified.
    """

    log.info("Submitting login (ENTER)...")

    login_window.type_keys("{ENTER}")

    time.sleep(2)

    log.info("Login submitted.")


def login(
    client: str,
    username: str,
    password: str,
    language: str = "EN",
    submit: bool = False,
    verify: bool = False,
) -> dict:
    """
    Full SAP GUI login flow.

    Exact sequence:

        Client -> Ctrl+A -> 000
        TAB -> User
        TAB -> Password
        ENTER
    """

    login_window = find_login_window()

    fill_login_fields(
        login_window,
        client,
        username,
        password,
        language,
    )

    success = None

    if submit:
        submit_login(login_window)

        if verify:
            success = verify_login_success()

    return {
        "window": login_window,
        "success": success,
    }


def verify_login_success(timeout: int = 10) -> bool:
    """
    Verify that SAP login completed successfully.
    """

    from pywinauto import Desktop

    deadline = time.time() + timeout

    while time.time() < deadline:

        time.sleep(0.5)

        windows = Desktop(
            backend="win32"
        ).windows()

        titles = []

        for window in windows:
            try:
                title = window.window_text()

                if title.strip():
                    titles.append(title)

            except Exception:
                continue

        login_screen_present = any(
            title == "SAP"
            for title in titles
        )

        session_present = any(
            "SAP GUI for Windows" in title
            for title in titles
        )

        if session_present and not login_screen_present:

            log.info(
                "Login verified: SAP session window detected, "
                "login screen closed."
            )

            return True

        if login_screen_present:
            continue

    log.warning(
        "Could not verify SAP login successfully."
    )

    return False