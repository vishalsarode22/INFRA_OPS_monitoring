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

# --- Windows-only GUI bindings -------------------------------------------
# pywinauto / win32com exist only on Windows. Importing them at module load
# made the whole sap_gui package unimportable on a Linux collector host, which
# in turn made main.py and the dashboard runner unimportable there. The names
# below resolve normally on Windows; elsewhere they become guards that raise a
# clear error the moment GUI automation is actually attempted, so a missing
# GUI stack is never mistaken for a healthy one.
class _GuiUnavailable:
    def __init__(self, name, exc):
        self._name, self._exc = name, exc

    def _fail(self, *a, **kw):
        raise RuntimeError(
            f"SAP GUI Scripting is unavailable on this host: {self._name} "
            f"could not be imported ({self._exc}). GUI collection requires "
            "Windows with SAP GUI installed."
        )

    __call__ = _fail
    __getattr__ = lambda self, item: self._fail()

try:
    from pywinauto import Desktop
    _GUI_IMPORT_ERROR = None
except ImportError as _exc:
    _GUI_IMPORT_ERROR = _exc
    Desktop = _GuiUnavailable('Desktop', _exc)


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

# moved into guarded import: from pywinauto import Desktop

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


def _own_logon_handle():
    """
    HWND of wnd[0] of the MONITOR's pinned session, or None.

    This is how the keystroke fallback knows a window is ours: SAP GUI
    scripting reports the window handle of each session, and the pinned
    session is the connection the monitor itself opened.
    """
    try:
        from sap_gui.scripting_connection import get_scripting_session, pinned_connection_id
        if not pinned_connection_id():
            return None
        session = get_scripting_session(quiet=True)
        session.findById("wnd[0]/usr/txtRSYST-BNAME")        # must be the logon screen
        handle = int(session.findById("wnd[0]").Handle)
        return handle or None
    except Exception:  # noqa: BLE001
        return None


def _foreground_handle():
    try:
        import win32gui
        return int(win32gui.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        return None


def _ensure_foreground(window) -> None:
    """
    Refuse to send a keystroke unless `window` is the foreground window.
    type_keys() goes to whatever has focus: a Notepad ('*IBOPS_command.txt')
    or a consultant's SAP window that took focus would receive the
    monitor's client, user and password.
    """
    try:
        expected = int(window.handle)
    except Exception:  # noqa: BLE001
        expected = None
    actual = _foreground_handle()
    if not expected or actual != expected:
        raise RuntimeError(
            f"Refusing to type: the foreground window ({actual}) is not the monitor's own "
            f"SAP logon window ({expected}).")


def find_login_window(timeout: int = 30, expected_handle=None):
    """
    Return the monitor's OWN logon window, focused, for the keystroke
    fallback.

    It used to take any top-level window whose title started with "SAP" --
    which can be a consultant's session. Now the window must be the one
    whose handle SAP GUI scripting reports for the monitor's pinned
    connection (expected_handle, see _own_logon_handle()). Without that
    handle nothing is returned and nothing will be typed.
    """
    if not expected_handle:
        raise RuntimeError(
            "Refusing to type SAP credentials: the logon window cannot be verified as the "
            "monitor's own (no pinned SAP connection on its logon screen). Enable SAP GUI "
            "scripting so the logon is done through the scripting API.")
    log.info(f"Finding the monitor's own SAP logon window (hwnd {expected_handle})...")
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            window = Desktop(backend="win32").window(handle=int(expected_handle)).wrapper_object()
            if window.is_visible():
                try:
                    window.set_focus()
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"Could not focus the monitor's SAP logon window: {exc}")
                return window
            last_error = "not visible"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    raise TimeoutError(
        f"The monitor's SAP logon window (hwnd {expected_handle}) was not usable within "
        f"{timeout}s ({last_error}). If the desktop is locked or the RDP session is "
        f"disconnected, GUI automation cannot run.")


def _type(window, keys, **kwargs) -> None:
    """type_keys(), but only while `window` is the foreground window."""
    _ensure_foreground(window)
    window.type_keys(keys, **kwargs)


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

    # The cursor starts in the User field. User and password are typed FIRST
    # and the client last: with SAP GUI's "automatic TAB at field end" on,
    # typing the 3-digit client jumps to User by itself, so the old order
    # (client, TAB, user, TAB, password) put the user name into Password and
    # the password into Language -- the empty-User logon screen of 16 Oct.
    log.info(f"Entering Username: {username}")
    _type(login_window, "{HOME}+{END}{DELETE}")
    _type(login_window, _literal(username), with_spaces=True)
    time.sleep(_LOGIN_KEY_WAIT)

    _type(login_window, "{TAB}")
    time.sleep(_LOGIN_TAB_WAIT)

    log.info("Entering Password... (masked)")
    _type(login_window, "{HOME}+{END}{DELETE}")
    _type(login_window, _literal(password), with_spaces=True)
    time.sleep(_LOGIN_KEY_WAIT)

    # Up twice: Password -> User -> Client.
    log.info(f"Entering Client: {client}")
    _type(login_window, "{UP}")
    time.sleep(_LOGIN_TAB_WAIT)
    _type(login_window, "{UP}")
    time.sleep(_LOGIN_TAB_WAIT)
    _type(login_window, "{HOME}+{END}{DELETE}")
    _type(login_window, _literal(client), with_spaces=True)
    time.sleep(_LOGIN_KEY_WAIT)

    log.info("Client, username and password populated.")


def submit_login(login_window):
    """
    Submit login with ENTER.

    The language field is intentionally not modified.
    """

    log.info("Submitting login (ENTER)...")

    _type(login_window, "{ENTER}")

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
    LAST_LOGON.update(message="", refused=False)
    while True:
        candidate = None
        try:
            candidate = get_scripting_session(quiet=True)
            candidate.findById("wnd[0]/usr/txtRSYST-BNAME")     # is this the logon screen?
            session = candidate
            break
        except Exception:
            # Already logged on (SSO / saved logon): our session has a user
            # and no logon screen. Nothing to fill; verification checks it
            # is the expected user.
            try:
                already = str(candidate.Info.User or "").strip() if candidate is not None else ""
            except Exception:  # noqa: BLE001
                already = ""
            if already:
                log.info(f"Scripting logon: the monitor's session is already logged on as {already}.")
                return True
            if time.time() >= deadline:
                break
            time.sleep(0.25)
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
    # The User field must hold the user before Enter: a logon sent without it
    # sat on the logon screen (CEAT S4P, 16 Oct).
    try:
        user_ctl = session.findById("wnd[0]/usr/txtRSYST-BNAME")
        if not str(user_ctl.Text or "").strip():
            user_ctl.Text = values["user"]
            if not str(user_ctl.Text or "").strip():
                log.warning("Scripting logon: the User field stayed empty after filling; typing instead.")
                try:   # the typed path must not append to a password already set here
                    session.findById("wnd[0]/usr/pwdRSYST-BCODE").Text = ""
                except Exception:  # noqa: BLE001
                    pass
                return False
    except Exception as exc:  # noqa: BLE001
        log.debug(f"Scripting logon: user field re-check skipped ({exc})")
    log.info(f"Scripting logon: client {values['client']}, user {values['user']} (no keystrokes).")

    if not submit:
        return True
    session.findById("wnd[0]").sendVKey(0)
    _wait_idle(session, _LOGIN_SUBMIT_WAIT)
    _handle_multiple_logon(session)
    # SAP says why a logon failed in the status bar ("Name or password is
    # incorrect", "User is locked"...). Keep it for the report and the mail.
    try:
        sbar = session.findById("wnd[0]/sbar")
        if str(getattr(sbar, "MessageType", "") or "") in ("E", "A"):
            LAST_LOGON.update(message=str(getattr(sbar, "Text", "") or "").strip(), refused=True)
            log.warning(f"SAP refused the logon: {LAST_LOGON['message']}")
    except Exception:  # noqa: BLE001
        pass
    if not LAST_LOGON["refused"]:
        # A popup still open after the logon (e.g. "change your password")
        # stops it without a status-bar message: say what it shows.
        try:
            from sap_gui.tcode_actions import _popup_text
            text = _popup_text(session)
            if text:
                LAST_LOGON["message"] = f"popup after logon: {text}"
        except Exception:  # noqa: BLE001
            pass
    return True


# What SAP said when the last scripting logon was refused ("" = nothing).
LAST_LOGON: dict = {"message": "", "refused": False}


def _wait_idle(session, limit: float) -> None:
    """Wait until SAP has answered (session not busy), at most `limit` seconds."""
    end = time.time() + max(0.0, limit)
    time.sleep(0.15)
    while time.time() < end:
        try:
            if not session.Busy:
                return
        except Exception:
            return
        time.sleep(0.1)


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


def _other_connections_open() -> bool:
    """True when SAP connections other than the monitor's are open here."""
    try:
        from sap_gui.scripting_connection import connection_ids, pinned_connection_id
        ids = connection_ids() or set()
        mine = pinned_connection_id()
        return bool(ids - ({mine} if mine else set()))
    except Exception:
        return False


def _pinned_session_logged_in(timeout: float = 15.0, expected_user=None) -> bool:
    """Logged in = our session has the expected user and is past the logon screen."""
    from sap_gui.scripting_connection import get_scripting_session
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = get_scripting_session(quiet=True)
            user = str(s.Info.User or "").strip()
            tx = str(s.Info.Transaction or "").strip().upper()
            if user and tx not in ("", "S000"):
                if expected_user and user.lower() != str(expected_user).strip().lower():
                    log.warning(f"The monitor's session is logged on as {user}, "
                                f"not the configured {expected_user}.")
                    return False
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _register_identity(client, username) -> None:
    """After a verified logon: remember who the monitor is (scripting_connection)."""
    try:
        from sap_gui.scripting_connection import get_scripting_session, register_identity
        system = None
        try:
            system = str(get_scripting_session(quiet=True).Info.SystemName or "") or None
        except Exception:  # noqa: BLE001
            pass
        register_identity(client=client, user=username, system=system)
    except Exception:  # noqa: BLE001
        pass


_said: dict = {}


def _first_in(key: str, seconds: float) -> bool:
    """True the first time `key` is seen in `seconds` (rate-limits a repeated error line)."""
    import time as _time
    now = _time.time()
    last = _said.get(key)
    if last is not None and now - last < seconds:
        return False
    _said[key] = now
    return True


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

    # An empty user or password (a ${VAR} missing from .env resolves to "")
    # left the sweep sitting on the logon screen until the watchdog killed
    # it. Refuse up front and say which setting is missing.
    missing = [n for n, v in (("user", username), ("password", password), ("client", client))
               if not str(v or "").strip()]
    if missing:
        raise ValueError(
            f"SAP GUI logon not attempted: the {', '.join(missing)} for this system is empty. "
            f"Check the system's username/password in config/systems.yaml and the ${{...}} "
            f"values it refers to in .env (e.g. SARLOHA_QAS_SAP_USERNAME).")

    # Scripting first: it does not depend on window focus. Set
    # IBO_GUI_LOGIN_MODE=keyboard to force the old path.
    shared = _other_connections_open()
    scripting_why = "scripting logon not tried"
    if shared or _os.environ.get("IBO_GUI_LOGIN_MODE", "scripting").strip().lower() != "keyboard":
        scripting_why = "scripting logon returned no session"
        try:
            if login_via_scripting(client, username, password, language, submit=submit):
                if submit and verify:
                    if LAST_LOGON.get("refused"):
                        success = False                 # SAP said no: do not wait to find out
                    else:
                        # Ask our own session first: it answers as soon as the
                        # logon is through. verify_login_success() asks the
                        # same session (Info.User, logon screen gone) a
                        # last time; neither looks at window titles.
                        success = _pinned_session_logged_in(timeout=15.0, expected_user=username)
                        if not success and not shared:
                            success = verify_login_success(timeout=3, expected_user=username)
                        if success:
                            _register_identity(client, username)
                else:
                    success = None
                return {"window": None, "success": success, "method": "scripting",
                        "message": LAST_LOGON.get("message", "")}
        except Exception as exc:   # noqa: BLE001 -- fall back to typing
            scripting_why = f"{type(exc).__name__}: {exc}"
            log.warning(f"Scripting logon failed ({scripting_why}); typing instead.")

    if shared:
        # Typing goes to whichever window has focus; with someone else's SAP
        # sessions open that could be THEIR window. Never type in that case.
        # The owner's rule working as designed: said once every 6 hours as an
        # error WITH the reason scripting failed (that is what to fix), a
        # warning in between -- it was 299 identical Errors-page entries.
        msg = ("Scripting logon was not possible and other SAP sessions are open on this machine; "
               f"not typing into the desktop. GUI evidence skipped this cycle. Why scripting failed: "
               f"{scripting_why}. Fix: SAP GUI Options > Accessibility & Scripting > Scripting: enable "
               f"scripting and switch off both 'Notify when...' boxes; on the SAP system, "
               f"sapgui/user_scripting = TRUE (RZ11).")
        if _first_in(f"shared:{scripting_why[:60]}", 6 * 3600):
            log.error(msg)
        else:
            log.warning(msg)
        return {"window": None, "success": False, "method": "skipped-shared-desktop",
                "message": f"scripting failed ({scripting_why}); other SAP sessions open, nothing typed"}

    # Last resort: keystrokes, but ONLY into the logon window of the
    # connection the monitor itself opened (its handle comes from scripting),
    # and only while that window has the focus. Otherwise refuse.
    own_handle = _own_logon_handle()
    if own_handle is None:
        log.error("Scripting logon was not possible and the monitor's own logon window cannot be "
                  "verified; refusing to type credentials into the desktop.")
    login_window = find_login_window(expected_handle=own_handle)

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
            own_handle = _own_logon_handle()
            login_window = find_login_window(expected_handle=own_handle)

    success = None

    if submit:
        submit_login(login_window)

        if verify:
            success = verify_login_success(expected_user=username)
            if success:
                _register_identity(client, username)

    return {
        "window": login_window,
        "success": success,
    }


def verify_login_success(timeout: int = 10, expected_user=None) -> bool:
    """
    Verify that the MONITOR's session is logged on.

    It used to look for any window titled "SAP GUI for Windows" -- a
    consultant's session satisfied that. Now it asks the monitor's own
    scripting session: Info.User must be set (and equal expected_user when
    given) and the logon screen must be gone.
    """
    from sap_gui.scripting_connection import get_scripting_session

    deadline = time.time() + timeout
    seen_user = ""
    while True:
        try:
            session = get_scripting_session(quiet=True)
            seen_user = str(session.Info.User or "").strip()
            on_logon_screen = True
            try:
                session.findById("wnd[0]/usr/txtRSYST-BNAME")
            except Exception:  # noqa: BLE001
                on_logon_screen = False
            if seen_user and not on_logon_screen:
                if expected_user and seen_user.lower() != str(expected_user).strip().lower():
                    log.warning(f"Login check: the monitor's session is logged on as {seen_user}, "
                                f"not {expected_user}.")
                    return False
                log.info(f"Login verified on the monitor's own session (user {seen_user}).")
                return True
        except Exception:  # noqa: BLE001
            pass
        if time.time() >= deadline:
            break
        time.sleep(0.5)

    log.warning("Could not verify SAP login on the monitor's own session"
                + (f" (user seen: {seen_user})." if seen_user else "."))
    return False
