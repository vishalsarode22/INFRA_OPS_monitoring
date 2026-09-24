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
import os as _os

# Field navigation waits. SAP's login screen answers keystrokes in tens of
# milliseconds; the waits were sized for RDP over a slow WAN and were never
# tuned. Override with IBO_LOGIN_KEY_WAIT / IBO_LOGIN_TAB_WAIT / IBO_LOGIN_SUBMIT_WAIT
# in .env for a specific desktop.
_LOGIN_KEY_WAIT    = float(_os.environ.get("IBO_LOGIN_KEY_WAIT",    "0.15") or 0.15)
_LOGIN_TAB_WAIT    = float(_os.environ.get("IBO_LOGIN_TAB_WAIT",    "0.10") or 0.10)
_LOGIN_SUBMIT_WAIT = float(_os.environ.get("IBO_LOGIN_SUBMIT_WAIT", "1.0")  or 1.0)

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

# How long to hold out for the title WITH the connection string before
# settling for the bare "SAP GUI for Windows <release>" frame. The release
# number varies by machine (770, 800, ...) -- never match on it.
_CONNECTED_TITLE_GRACE_S = 8.0

# Windows that are NOT the logon screen and must never be matched.
# "SAP Logon( \d+)?$" covers "SAP Logon", "SAP Logon 770", "SAP Logon 800".
_LOGIN_TITLE_EXCLUDE = re.compile(
    r"SAP Logon( \d+)?$|SAP Logon Pad|Sapgui Splash|SAP Graphics Multiplexer|InfraBeat", re.IGNORECASE
)

# pywinauto's type_keys() treats these as control characters. A client,
# username or password containing any of them was silently mistyped -- a
# password of "Pa%%w0rd+" sends a literal percent-escape and a SHIFT hold,
# and the login just fails with bad credentials. Wrapping each in braces
# types it literally.
_TYPE_KEYS_SPECIALS = "^+%~(){}[]"


def _literal(text: str) -> str:
    """Escape a string so type_keys() sends it character for character."""
    out = []
    for ch in str(text):
        out.append("{" + ch + "}" if ch in _TYPE_KEYS_SPECIALS else ch)
    return "".join(out)


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

    started = time.time()
    deadline = started + timeout
    seen: set[str] = set()
    rejected: dict[str, str] = {}   # SAP-looking windows and why each was not used

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

        # desktop.window(title_re=...) raises ElementAmbiguousError when MORE
        # THAN ONE window matches -- and right after launch there are always
        # several: the Logon pad, the new GUI window, the splash screen. That
        # error was swallowed by the except below, every second, for the whole
        # timeout: the login screen was on screen and never "appeared". So
        # enumerate all matches with windows() and pick the right one.
        # Collect every candidate from every pattern FIRST, then rank them all
        # together. Pattern order alone picked the bare "SAP" helper window over
        # "SAP GUI for Windows 800 [/H/host/S/3200 3]" when both were up.
        matches = {}
        for pattern in _LOGIN_TITLE_PATTERNS:
            try:
                for w in desktop.windows(title_re=pattern, visible_only=True):
                    try:
                        key = getattr(w, "handle", None) or id(w)
                    except Exception:
                        key = id(w)
                    matches.setdefault(key, w)
            except Exception as exc:
                rejected[f"pattern {pattern!r}"] = f"enumeration failed: {type(exc).__name__}"

        def _rank(w):
            try:
                t = (w.window_text() or "").lower()
            except Exception:
                t = ""
            return (0 if "[/" in t else 1,                 # carries the connection string
                    0 if "sap gui for windows" in t else 1,
                    0 if t.strip() == "sap" else 1,        # bare "SAP" is next best
                    t)

        for window in sorted(matches.values(), key=_rank):
            try:
                title = (window.window_text() or "").strip()
            except Exception as exc:
                rejected["<unreadable title>"] = type(exc).__name__
                continue
            if not title:
                continue
            if _LOGIN_TITLE_EXCLUDE.search(title):
                rejected[title] = "excluded (pad / splash / helper)"
                continue
            # windows() returns HwndWrapper objects: they have is_visible(),
            # set_focus() and type_keys() (which login() needs) but NOT the
            # .wait() a WindowSpecification has -- calling it raised
            # AttributeError and silently rejected the logon screen.
            try:
                if not window.is_visible():
                    rejected[title] = "not visible"
                    continue
            except Exception as exc:
                rejected[title] = f"is_visible failed: {type(exc).__name__}"
                continue
            # The logon screen first appears as "SAP GUI for Windows <release>"
            # and gets "[/H/host/S/3200 n]" appended once the connection is up.
            # Typing into the pre-connection frame is what produced
            # ElementNotVisible: the frame is replaced under our keystrokes.
            # So a title without the connection string is accepted only after
            # a few seconds of nothing better appearing.
            if "[/" not in title and (time.time() - started) < _CONNECTED_TITLE_GRACE_S:
                rejected[title] = f"waiting up to {_CONNECTED_TITLE_GRACE_S:.0f}s for the connected title"
                break            # re-scan on the next loop iteration
            log.info(f"SAP login window found: '{title}' ({len(matches)} SAP window(s) on screen).")
            time.sleep(1)
            try:
                window.set_focus()
            except Exception as exc:
                # A window that cannot be focused is usually a locked or
                # disconnected desktop -- GUI scripting cannot work there.
                log.warning(f"Could not focus the SAP window: {exc}")
            return window
        time.sleep(1)

    candidates = ", ".join(f"'{t}'" for t in sorted(seen)) or "none detected"
    why = "; ".join(f"'{t}': {r}" for t, r in rejected.items()) or "no window starting with SAP was seen"
    raise TimeoutError(
        f"SAP login window did not appear within {timeout}s. SAP windows considered -- {why}. "
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
    time.sleep(_LOGIN_KEY_WAIT)

    log.info("Clearing existing Client value...")

    login_window.type_keys("{DELETE}")
    login_window.type_keys("{DELETE}")
    login_window.type_keys("{DELETE}")

    time.sleep(_LOGIN_KEY_WAIT)

    log.info(f"Entering Client: {client}")

    login_window.type_keys(
        _literal(client),
        with_spaces=True,
    )

    time.sleep(_LOGIN_KEY_WAIT)

    login_window.type_keys("{TAB}")

    time.sleep(_LOGIN_TAB_WAIT)

    log.info(f"Entering Username: {username}")

    login_window.type_keys(
        _literal(username),
        with_spaces=True,
    )

    time.sleep(_LOGIN_KEY_WAIT)

    login_window.type_keys("{TAB}")

    time.sleep(_LOGIN_TAB_WAIT)

    log.info("Entering Password... (masked)")

    login_window.type_keys(
        _literal(password),
        with_spaces=True,
    )

    time.sleep(_LOGIN_KEY_WAIT)

    log.info("Client, username and password populated.")


def submit_login(login_window):
    """
    Submit login with ENTER.

    The language field is intentionally not modified.
    """

    log.info("Submitting login (ENTER)...")

    login_window.type_keys("{ENTER}")

    time.sleep(_LOGIN_SUBMIT_WAIT)

    log.info("Login submitted.")


# The logon screen's own fields. Addressing them directly beats typing:
# keyboard input goes to whichever window has focus, and three PS4 sweeps
# failed with "Logon window changed under us" because Notepad, File Explorer
# or Teams took focus while the credentials were being typed.
_LOGIN_FIELDS = (("client", "wnd[0]/usr/txtRSYST-MANDT"),
                 ("user", "wnd[0]/usr/txtRSYST-BNAME"),
                 ("password", "wnd[0]/usr/pwdRSYST-BCODE"),
                 ("language", "wnd[0]/usr/txtRSYST-LANGU"))


def login_via_scripting(client: str, username: str, password: str,
                        language: str = "EN", timeout: int = 20, submit: bool = True) -> bool:
    """
    Fill and submit the SAP logon screen through the scripting API.

    Returns True when the screen was filled (and submitted, if asked).
    False means "not possible here" -- the caller then types, as before.
    """
    from sap_gui.scripting_connection import get_scripting_session

    deadline = time.time() + timeout
    session = None
    while time.time() < deadline:
        try:
            candidate = get_scripting_session()
            candidate.findById("wnd[0]/usr/txtRSYST-BNAME")     # is this the logon screen?
            session = candidate
            break
        except Exception:
            time.sleep(1)
    if session is None:
        log.info("Scripting logon not available (no session or not on the logon screen); typing instead.")
        return False

    values = {"client": str(client or ""), "user": str(username or ""),
              "password": str(password or ""), "language": str(language or "")}
    for name, control_id in _LOGIN_FIELDS:
        value = values[name]
        if not value:
            continue
        try:
            session.findById(control_id).Text = value
        except Exception as exc:
            if name in ("user", "password"):
                log.warning(f"Scripting logon: {name} field not settable ({exc}); typing instead.")
                return False
            log.debug(f"Scripting logon: optional field {name} skipped ({exc})")
    log.info(f"Scripting logon: client {values['client']}, user {values['user']} (no keystrokes).")

    if not submit:
        return True
    session.findById("wnd[0]").sendVKey(0)
    time.sleep(_LOGIN_SUBMIT_WAIT)
    _handle_multiple_logon(session)
    return True


def _handle_multiple_logon(session) -> None:
    """
    The "multiple logon" dialog. Its default option ends the user's OTHER
    sessions, which on a production system could be a person's work, so this
    only ever picks "continue without ending any other logons". If that
    option cannot be selected the dialog is cancelled and the logon fails
    loudly rather than quietly signing someone else out.
    """
    try:
        popup = session.findById("wnd[1]")
    except Exception:
        return
    for control_id in ("wnd[1]/usr/radMULTI_LOGON_OPT2",
                       "wnd[1]/usr/radRESTART_OPT2"):
        try:
            session.findById(control_id).Select()
            session.findById("wnd[1]/tbar[0]/btn[0]").press()
            log.info("Multiple logon dialog: continued without ending other logons.")
            return
        except Exception:
            continue
    try:
        popup.sendVKey(12)
        log.warning("Multiple logon dialog appeared but its options were not recognised; "
                    "cancelled rather than ending another session.")
    except Exception:
        pass


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

    # Scripting first: it does not depend on window focus. Set
    # IBO_GUI_LOGIN_MODE=keyboard to force the old path.
    if _os.environ.get("IBO_GUI_LOGIN_MODE", "scripting").strip().lower() != "keyboard":
        try:
            if login_via_scripting(client, username, password, language, submit=submit):
                success = verify_login_success() if (submit and verify) else None
                return {"window": None, "success": success, "method": "scripting"}
        except Exception as exc:   # noqa: BLE001 -- fall back to typing
            log.warning(f"Scripting logon failed ({type(exc).__name__}: {exc}); typing instead.")

    login_window = find_login_window()

    # If the logon frame is replaced while we type (connection completing,
    # or a network blip closing and reopening it), the wrapper we hold points
    # at a window that no longer exists: ElementNotVisible. Find it again
    # once and retype from the start rather than fail the whole cycle.
    for attempt in (1, 2):
        try:
            fill_login_fields(login_window, client, username, password, language)
            break
        except Exception as exc:
            name = type(exc).__name__
            if attempt == 2 or name not in ("ElementNotVisible", "ElementNotEnabled", "InvalidWindowHandle", "RuntimeError"):
                raise
            log.warning(f"Logon window changed under us ({name}); finding it again and retyping.")
            time.sleep(2)
            login_window = find_login_window()

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

        # Release-agnostic: matches "SAP GUI for Windows 770",
        # "SAP GUI for Windows 800", and anything later.
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