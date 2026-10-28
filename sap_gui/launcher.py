"""
Fully automates launching SAP Logon and selecting a connection, so the
whole pipeline can run unattended from a cold start.

Session safety (consultants may be logged on manually on the same VM):
the monitor opens its OWN connection (scripting OpenConnection), drives
only that, closes only that, and terminates only a SAP Logon process it
started itself and that holds nobody else's connection. See
kill_sap_processes().
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
    from pywinauto.application import Application
    _GUI_IMPORT_ERROR = None
except ImportError as _exc:
    _GUI_IMPORT_ERROR = _exc
    Desktop = _GuiUnavailable('Desktop', _exc)
    Application = _GuiUnavailable('Application', _exc)


import re
import subprocess
import time
import os as _os
# moved into guarded import: from pywinauto import Desktop

# Post-visibility settle for the SAP Logon pad. The pad is usable the moment
# .wait("visible") returns; the extra second was defensive padding for old
# machines. Override with IBO_SAPLOGON_SETTLE in .env.
_PAD_SETTLE = float(_os.environ.get("IBO_SAPLOGON_SETTLE", "0.3") or 0.3)
# moved into guarded import: from pywinauto.application import Application

from utils.logger import get_logger

log = get_logger(__name__, "application")


# The Logon Pad's window title carries the SAP GUI RELEASE, which differs
# per workstation: "SAP Logon 800" on one machine, "SAP Logon 770" on the
# next, plain "SAP Logon" or "SAP Logon Pad 750" on others. Matching one
# literal release meant the pad was launched, sat visible on screen, and
# was never found -- the whole sweep then reported "SAP GUI unavailable"
# for 30s on a machine where nothing was actually wrong.
#
# Anchored at both ends on purpose: an unanchored "SAP Logon" also matches
# the logon SCREEN and helper windows, which is the ambiguity that broke
# the login finder in sap_gui/connection.py.
_PAD_TITLE_RE = re.compile(r"^SAP Logon( Pad)?(\s+\d+)?$", re.IGNORECASE)

# Override if a site has a pad title this does not cover.
_PAD_TITLE_OVERRIDE = (_os.environ.get("IBO_SAPLOGON_PAD_TITLE") or "").strip()


_SAP_IMAGES = ("saplogon.exe", "sapgui.exe")


class GuiHostBusy(RuntimeError):
    """Kept for compatibility; no longer raised. The monitor now works alongside
    someone else's SAP Logon instead of refusing (see kill_sap_processes)."""


def _current_session_id():
    """Windows session id of this process (RDP/console), or None."""
    if _os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        sid = wintypes.DWORD()
        if ctypes.windll.kernel32.ProcessIdToSessionId(_os.getpid(), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:  # noqa: BLE001
        pass
    return None


def _process_create_time(pid: int):
    """
    Creation time (FILETIME ticks) of a process of OURS, or None.

    Stored with every PID the monitor launches, so a PID that Windows has
    since reused for someone else's SAP Logon is never mistaken for ours.
    """
    if _os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return None
            return (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _sap_processes() -> list[dict]:
    """[{pid, image, user, session}] for SAP GUI processes in THIS Windows session.

    Other Windows sessions (another RDP user) have their own SAP Logon, which
    can never hold the monitor's connection and is never touched; filtering
    by session keeps them out of every decision here.
    """
    # Without /V: /V asks every window for its title and took 1-5 s per call
    # (three calls before each logon). The user column it added was never used.
    out: list[dict] = []
    try:
        res = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=15)
        import csv, io
        my_session = _current_session_id()
        for row in csv.reader(io.StringIO(res.stdout or "")):
            if len(row) >= 2 and row[0].lower() in _SAP_IMAGES:
                try:
                    pid = int(row[1])
                except ValueError:
                    continue
                try:
                    session = int(row[3]) if len(row) > 3 else None
                except ValueError:
                    session = None
                if my_session is not None and session is not None and session != my_session:
                    continue
                out.append({"pid": pid, "image": row[0].lower(), "user": "", "session": session})
    except Exception as e:
        log.debug(f"tasklist failed: {e}")
    return out


def _foreign_sap_processes() -> list[dict]:
    """SAP GUI processes that the monitor did not launch (an operator's session)."""
    mine = {int(p) for p in _launched_pids}
    return [p for p in _sap_processes() if p["pid"] not in mine]


# PIDs of saplogon.exe started by launch_saplogon() in this process, so the
# cleanup can be limited to what the sweep itself opened.
_launched_pids: set[int] = set()
# pid -> process creation time recorded at launch (None when unreadable).
_launched_ctimes: dict[int, int | None] = {}
try:
    from utils.paths import BASE_DIR as _BASE_DIR      # never __file__ (frozen exe)
except Exception:  # noqa: BLE001
    _BASE_DIR = _os.getcwd()
_PIDS_PATH = _os.path.join(_BASE_DIR, "dashboard", "sap_gui_pids.json")


def _load_pids() -> None:
    """A restart must still recognise the SAP Logon the previous run opened.

    File format: [{"pid": 123, "ctime": 1339...}] (older files: [123, ...]).
    """
    try:
        from core.atomic_json import read_json
        for entry in read_json(_PIDS_PATH, default=[]) or []:
            if isinstance(entry, dict):
                pid = int(entry.get("pid"))
                ctime = entry.get("ctime")
                _launched_ctimes[pid] = int(ctime) if ctime is not None else None
            else:
                pid = int(entry)
            _launched_pids.add(pid)
    except Exception:
        pass


def _save_pids() -> None:
    try:
        from core.atomic_json import write_json
        write_json(_PIDS_PATH, [{"pid": pid, "ctime": _launched_ctimes.get(pid)}
                                for pid in sorted(_launched_pids)])
    except Exception:
        pass


def _still_ours(pid: int) -> bool:
    """Same process we launched? (PID not reused since.) Unknown -> trust the PID."""
    recorded = _launched_ctimes.get(pid)
    if recorded is None:
        return True
    current = _process_create_time(pid)
    return current is None or current == recorded


_load_pids()


def _dedicated_host() -> bool:
    """IBO_DEDICATED_GUI_HOST=1 says nobody but the monitor uses SAP GUI here."""
    return (_os.environ.get("IBO_DEDICATED_GUI_HOST", "0") or "0").strip().lower() in ("1", "true", "yes")


def kill_sap_processes(force_all: bool | None = None):
    """
    End the monitor's SAP GUI work -- and nothing that belongs to anyone else.

    The original ran `taskkill /F /IM saplogon.exe` and `sapgui.exe`, which
    killed EVERY SAP session on the machine, including a consultant's, with
    no warning. Now, before a sweep, after a sweep, after an RCA capture and
    when the watchdog finds a hung sweep:

      1. The monitor's own connection (pinned in scripting_connection) is
         closed through the scripting API. Other connections in the same
         SAP Logon process are untouched.
      2. The SAP Logon process the monitor started is killed only when no
         connection is left in it -- i.e. nobody else is using it. If a
         person's sessions are open (in their own SAP Logon, or in one the
         monitor started that they then joined), SAP Logon keeps running.
      3. IBO_DEDICATED_GUI_HOST=1 (or force_all=True) declares the VM
         reserved for monitoring and restores the old kill-everything.

    Never raises: a cleanup problem must not fail a sweep.
    """
    force_all = _dedicated_host() if force_all is None else force_all
    pinned = None
    try:
        from sap_gui.scripting_connection import (close_pinned_connection, connection_ids_guarded,
                                                  pinned_connection_id, unpin)
        pinned = pinned_connection_id()
        closed = close_pinned_connection()
        # Own COM apartment + timeout: this runs on the watchdog thread too,
        # where COM may not be initialised and SAP Logon may be hung.
        remaining = connection_ids_guarded()
        if closed:
            unpin()
    except Exception as exc:  # noqa: BLE001
        log.debug(f"monitor connection close skipped: {exc}")
        closed, remaining = False, None

    # Connections that are NOT the monitor's. If our own close hung, our
    # connection is still listed; it does not count as "someone else".
    others = None if remaining is None else {c for c in remaining if c != pinned}
    foreign = _foreign_sap_processes()
    if not force_all:
        others_present = bool(others) or (others is None and (bool(foreign) or _shared_at_launch))
        if others_present or (_shared_at_launch and not closed):
            if not closed and pinned:
                log.error(f"The monitor's connection {pinned} could not be closed and SAP Logon "
                          f"also holds other people's sessions, so it was NOT killed. Close "
                          f"connection {pinned} by hand when convenient.")
            n = len(others) if others else "an unknown number of"
            log.info(f"SAP Logon left running: {n} other connection(s) open on this machine "
                     f"(someone else's sessions). Only the monitor's connection was closed.")
            _launched_pids.clear()
            _launched_ctimes.clear()
            _save_pids()
            return
    if force_all:
        log.warning("IBO_DEDICATED_GUI_HOST=1: ending EVERY SAP GUI process on this machine "
                    "(the VM is declared reserved for monitoring).")
        for proc in _SAP_IMAGES:
            try:
                subprocess.run(["taskkill", "/F", "/IM", proc], capture_output=True)
            except Exception as e:
                log.debug(f"taskkill {proc} skipped/failed (likely not running): {e}")
    else:
        # Only PIDs that are STILL a SAP GUI process: when SAP Logon was
        # already running, the process we started handed over and exited, and
        # its PID may since belong to something unrelated.
        live_sap = {p["pid"] for p in _sap_processes()} if _launched_pids else set()
        killed = False
        for pid in list(_launched_pids):
            if pid not in live_sap:
                continue
            if not _still_ours(pid):
                log.warning(f"PID {pid} is a SAP GUI process but not the one the monitor started "
                            f"(PID reused); left running.")
                continue
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
                killed = True
                log.info(f"SAP Logon started by the monitor (pid {pid}) closed; no other sessions in it.")
            except Exception as e:
                log.debug(f"taskkill pid {pid} failed: {e}")
        # sapgui.exe is spawned by saplogon.exe; /T above takes the tree.
        if not killed:
            log.info("No SAP Logon process was terminated (none started by the monitor is "
                     "still running); other SAP GUI sessions were not touched.")
        _launched_pids.clear()
        _launched_ctimes.clear()
        _save_pids()
        if killed:
            time.sleep(1)         # let Windows release the process before a relaunch
        return
    _launched_pids.clear()
    _launched_ctimes.clear()
    _save_pids()
    time.sleep(1)


def _sap_window_titles() -> list[str]:
    """Every visible top-level title, for a useful failure message."""
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


def find_pad_title(backend: str = "win32") -> str | None:
    """
    Return the Logon Pad's ACTUAL window title, or None if it is not up yet.

    Enumerating with windows() rather than asking for window(title_re=...)
    is deliberate: the latter RAISES when more than one window matches, and
    right after launch the splash and the pad can both be present. The raise
    was swallowed by the retry loop and looked exactly like "not there yet".
    """
    if _PAD_TITLE_OVERRIDE:
        return _PAD_TITLE_OVERRIDE
    try:
        candidates = Desktop(backend=backend).windows(visible_only=True)
    except Exception as exc:
        log.debug(f"Window enumeration failed: {exc}")
        return None

    for win in candidates:
        try:
            title = (win.window_text() or "").strip()
        except Exception:
            continue
        if title and _PAD_TITLE_RE.match(title):
            return title
    return None


_PAD_GRACE_S = 5.0


def _scripting_ready() -> bool:
    try:
        from sap_gui.scripting_connection import connection_ids
        return connection_ids() is not None
    except Exception:  # noqa: BLE001
        return False


def launch_saplogon(exe_path: str, timeout: int = 30):  # was 15
    """
    Make sure SAP Logon is running in this Windows session.

    If it is already running (an operator's, or one the monitor kept), it is
    REUSED, not started again: SAP Logon is single-instance, a second start
    only hands over to the running one and exits, and the monitor must never
    claim -- and later kill -- a process it did not start. Only a SAP Logon
    the monitor really started is remembered (PID + creation time, in
    dashboard/sap_gui_pids.json) as the one cleanup may end.

    Returns the Logon Pad window spec when the pad is visible, or None when
    SAP GUI scripting is reachable without it (pad minimised to the tray):
    the connection is opened through scripting either way.
    """
    global _shared_at_launch
    running = _sap_processes()
    _shared_at_launch = bool(running)
    proc = None
    if running:
        log.info("SAP Logon is already running in this Windows session (pid(s) "
                 f"{', '.join(str(p['pid']) for p in running)}); the monitor will open its own "
                 "connection in it and close only that. That SAP Logon is never killed.")
    else:
        log.info(f"Launching SAP Logon: {exe_path}")
        proc = subprocess.Popen([exe_path])

    end_time = time.time() + timeout
    pad = None
    while time.time() < end_time:
        title = find_pad_title("win32")
        if title:
            try:
                # Build the spec on the EXACT discovered title so the caller
                # still gets a WindowSpecification (with .wait()), and so the
                # match cannot be ambiguous.
                spec = Desktop(backend="win32").window(title=title)
                spec.wait("visible", timeout=2)
                log.info(f"SAP Logon pad is visible: '{title}'.")
                time.sleep(_PAD_SETTLE)
                pad = spec
                break
            except Exception as exc:
                log.debug(f"Pad '{title}' seen but not ready yet: {exc}")
        # A pad minimised to the tray is never "visible"; scripting is what
        # the monitor needs. A fresh start gets a few seconds to show its pad.
        if (running or time.time() > end_time - timeout + _PAD_GRACE_S) and _scripting_ready():
            log.info("SAP Logon is reachable through scripting (Logon Pad not visible).")
            break
        time.sleep(1)
    else:
        if not _scripting_ready():
            present = ", ".join(f"'{t}'" for t in sorted(set(_sap_window_titles()))) or "none detected"
            raise TimeoutError(
                f"No SAP Logon pad appeared within {timeout}s. "
                f"Looked for a window titled like 'SAP Logon', 'SAP Logon 770', "
                f"'SAP Logon 800' or 'SAP Logon Pad 750'. "
                f"Visible windows were: {present}. "
                f"If the pad IS listed above under another name, set "
                f"IBO_SAPLOGON_PAD_TITLE in .env to that exact title. If nothing SAP "
                f"is listed, the desktop is locked or the RDP session is disconnected "
                f"-- GUI scripting drives a real desktop and cannot run without one."
            )

    if proc is not None:
        if proc.poll() is None:
            _launched_pids.add(int(proc.pid))
            _launched_ctimes[int(proc.pid)] = _process_create_time(proc.pid)
            _save_pids()
            log.info(f"SAP Logon started by the monitor: pid {proc.pid} (the only SAP Logon "
                     "cleanup may end, and only when no one else's connection is in it).")
        else:
            # It handed over to a SAP Logon that appeared meanwhile: not ours.
            _shared_at_launch = True
            log.info("The SAP Logon process the monitor started exited at once (another SAP "
                     "Logon took over); nothing recorded as the monitor's.")
    return pad


def _foreground_handle():
    try:
        import win32gui
        return int(win32gui.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        return None


def _bring_to_front(hwnd) -> bool:
    """
    Make the Logon Pad the foreground window WITHOUT sending any key or click.

    Windows refuses SetForegroundWindow from a process that is not in front
    (the window only flashes in the taskbar) -- the reason "CARFOUR QAS" was
    listed as present yet never opened. Attaching our input queue to the
    foreground window's thread for the call lifts that restriction; nothing
    is typed into the other window.
    """
    if not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(hwnd)
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return True
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        me = kernel32.GetCurrentThreadId()
        attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_thread, False)
        time.sleep(0.3)
        return user32.GetForegroundWindow() == hwnd
    except Exception as exc:  # noqa: BLE001 -- not Windows, or no permission
        log.debug(f"Could not bring the Logon Pad to the front: {exc}")
        return False


_last_activate_refusal = {"label": None}


def _activate(item, label: str, pad_handle=None) -> bool:
    """
    Opens a Logon Pad entry.

    Tries keyboard activation first (select the row, press Enter). That is
    driven through the accessibility API and does not depend on which window
    happens to be on top, so it cannot be stolen by another application the
    way a physical mouse click can.

    Falls back to double_click_input() only if selection is unavailable.

    No input is sent unless the Logon Pad (pad_handle) is the foreground
    window: an ENTER or a click must never land in a Notepad, a browser or a
    consultant's SAP window that happens to have focus.
    """
    def _pad_in_front() -> bool:
        actual = _foreground_handle()
        if pad_handle and actual == int(pad_handle):
            return True
        if pad_handle and _bring_to_front(pad_handle):
            return True
        log.warning(f"Not activating '{label}': the SAP Logon Pad is not the foreground window "
                    f"(foreground hwnd {_foreground_handle()}, pad hwnd {pad_handle}).")
        _last_activate_refusal["label"] = label
        return False

    try:
        item.select()
        time.sleep(0.3)
        if not _pad_in_front():
            return False
        item.type_keys("{ENTER}")
        log.info(f"Opened connection '{label}' via keyboard activation.")
        return True
    except Exception as exc:
        log.debug(f"Keyboard activation failed for '{label}': {exc}")

    try:
        item.set_focus()
    except Exception:
        pass
    if not _pad_in_front():
        return False
    try:
        item.double_click_input()
        log.info(f"Double-clicked connection '{label}'.")
        return True
    except Exception as exc:
        log.warning(f"Could not activate connection '{label}': {exc}")
        return False


_shared_at_launch = False


def select_connection(connection_name: str, timeout: int = 30):
    """
    Open `connection_name` and pin it as the monitor's connection (see
    scripting_connection), so the sweep drives only its own session even
    when other people's connections are open on this machine.

    1. Scripting: application.OpenConnection(entry, False) -- asynchronous,
       so SAP GUI's "a script is opening a connection" prompt cannot block
       the sweep -- then the new connection id (before/after comparison,
       matched on the entry's Description) is pinned once it has a session
       (IBO_SAP_OPEN_CONNECTION_TIMEOUT, default 20 s).
    2. Only if that is not possible: the Logon Pad, with input sent only
       while the pad is the foreground window, and the connection that
       appears pinned only if it can be attributed to this entry.
    Raises when the monitor's own connection cannot be identified: the
    sweep must not continue on a connection it cannot prove is its own.
    The timeout matches launch_saplogon's (30 s).
    """
    try:
        from sap_gui.scripting_connection import (connection_ids, open_connection,
                                                  pin_new_connection, unpin)
    except Exception as exc:                # pragma: no cover - non-Windows
        raise RuntimeError(f"SAP GUI scripting is not available: {exc}") from exc
    unpin()
    try:
        open_connection(connection_name)
        return True
    except Exception as exc:  # noqa: BLE001
        log.info(f"Opening '{connection_name}' through scripting failed ({type(exc).__name__}: "
                 f"{exc}); using the Logon Pad.")
    before = connection_ids()
    if before is None:
        raise RuntimeError(
            "SAP GUI scripting is not reachable, so the monitor could not identify its own "
            "connection; not driving the Logon Pad blind. Check that scripting is enabled "
            "in SAP GUI options and on the server (sapgui/user_scripting).")
    result = _select_connection_on_pad(connection_name, timeout)
    if not pin_new_connection(before, timeout=timeout, description=connection_name):
        raise RuntimeError(
            f"'{connection_name}' was activated on the Logon Pad but the monitor could not "
            f"identify the connection it opened; stopping rather than risk driving someone "
            f"else's session.")
    return result


def _select_connection_on_pad(connection_name: str, timeout: int = 30):
    """
    Finds and double-clicks the named connection entry in SAP Logon Pad,
    using the UIA backend since the connection list is not exposed via SAP
    GUI Scripting (scripting only attaches to sessions, not the pad).

    Matching is exact first, then CASE-INSENSITIVE.

    pywinauto matches titles case-sensitively, and Logon Pad entry names are
    typed by whoever created them. Three systems failed a whole sweep because
    the configured names differed from the pad by one letter of case --
    "Test system" vs "test system", "Fuji QAS" vs "fuji QAS",
    "Centor QAS system" vs "Centor QAS System". That is a configuration typo,
    not a monitoring failure, so it should not cost a collection cycle.

    On failure the error now LISTS the entries that are actually present,
    which turns a guessing game into a one-line fix.
    """
    log.info(f"Selecting connection: {connection_name}")
    _last_activate_refusal["label"] = None
    end_time = time.time() + timeout
    target = (connection_name or "").strip().lower()
    seen: list[str] = []
    pad_title: str | None = None

    while time.time() < end_time:
        try:
            # Re-discover each pass: the pad may not be up on the first loop,
            # and the release number is not knowable in advance.
            pad_title = find_pad_title("uia") or find_pad_title("win32")
            if not pad_title:
                time.sleep(1)
                continue

            app = Application(backend="uia").connect(title=pad_title)
            window = app.window(title=pad_title)
            try:
                pad_handle = window.wrapper_object().handle
            except Exception:  # noqa: BLE001
                pad_handle = None

            # The Logon Pad MUST be the foreground window before any click.
            #
            # double_click_input() sends a PHYSICAL mouse click at screen
            # coordinates. If another window (Teams, a browser, an editor)
            # is on top, the click lands on that instead -- silently. The
            # symptom is a pad whose selected row never changes and a SAP
            # GUI window that never opens, with no error anywhere.
            try:
                window.set_focus()
                time.sleep(0.4)
            except Exception as exc:
                log.debug(f"Could not focus SAP Logon Pad: {exc}")

            # 1. Exact match -- the fast path, and what SAP itself would do.
            try:
                item = window.child_window(title=connection_name, control_type="ListItem")
                item.wait("visible", timeout=2)
                if _activate(item, connection_name, pad_handle):
                    return True
            except Exception:
                pass

            # 2. Case-insensitive match over the visible entries.
            seen = []
            for item in window.descendants(control_type="ListItem"):
                try:
                    title = (item.window_text() or "").strip()
                except Exception:
                    continue
                if not title:
                    continue
                seen.append(title)
                if title.lower() == target:
                    if not _activate(item, title, pad_handle):
                        continue
                    log.warning(
                        f"Connection matched case-insensitively: configured "
                        f"'{connection_name}', actual '{title}'. Update the "
                        f"*_SAP_CONNECTION_NAME value in .env to '{title}'."
                    )
                    return True
        except Exception as e:
            log.debug(f"Connection item not found yet, retrying: {e}")
        time.sleep(1)

    available = ", ".join(f"'{t}'" for t in dict.fromkeys(seen)) or "none detected"
    where = f"pad '{pad_title}'" if pad_title else "the SAP Logon Pad (pad window never found)"
    if target in {t.lower() for t in seen} or _last_activate_refusal["label"]:
        # Found -- the honest message. The old one said "could not find" while
        # listing the very entry.
        raise TimeoutError(
            f"Connection '{connection_name}' is in {where} but was not opened: the Logon Pad could not "
            f"be brought to the front (another window kept the focus, or the screen of this machine is "
            f"locked or disconnected). Nothing was typed. Keep the VM's desktop session unlocked (sign "
            f"out of Remote Desktop with 'tscon' instead of closing the window), or enable SAP GUI "
            f"scripting so the connection is opened without the Logon Pad.")
    raise TimeoutError(
        f"Could not find connection '{connection_name}' in {where}. "
        f"Entries present: {available}. "
        f"Set the matching *_SAP_CONNECTION_NAME in .env "
        f"(names are per-workstation and case-sensitive)."
    )
