"""
Fully automates launching SAP Logon and selecting a connection, so the
whole pipeline can run unattended from a cold start.
Kills any existing SAP processes first, since SAP GUI Scripting has
been observed to only reliably attach to sessions started fresh
(see project notes) -- reusing an already-open SAP Logon Pad risks
the same "0 sessions" scripting issue encountered earlier.
"""

import re
import subprocess
import time
import os as _os
from pywinauto import Desktop

# Post-visibility settle for the SAP Logon pad. The pad is usable the moment
# .wait("visible") returns; the extra second was defensive padding for old
# machines. Override with IBO_SAPLOGON_SETTLE in .env.
_PAD_SETTLE = float(_os.environ.get("IBO_SAPLOGON_SETTLE", "0.3") or 0.3)
from pywinauto.application import Application

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


def kill_sap_processes():
    for proc in ["saplogon.exe", "sapgui.exe"]:
        try:
            subprocess.run(["taskkill", "/F", "/IM", proc], capture_output=True)
        except Exception as e:
            log.debug(f"taskkill {proc} skipped/failed (likely not running): {e}")
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


def launch_saplogon(exe_path: str, timeout: int = 30):  # was 15
    log.info(f"Launching SAP Logon: {exe_path}")
    subprocess.Popen([exe_path])

    end_time = time.time() + timeout
    while time.time() < end_time:
        title = find_pad_title("win32")
        if title:
            try:
                # Build the spec on the EXACT discovered title so the caller
                # still gets a WindowSpecification (with .wait()), and so the
                # match cannot be ambiguous.
                pad = Desktop(backend="win32").window(title=title)
                pad.wait("visible", timeout=2)
                log.info(f"SAP Logon pad is visible: '{title}'.")
                time.sleep(_PAD_SETTLE)
                return pad
            except Exception as exc:
                log.debug(f"Pad '{title}' seen but not ready yet: {exc}")
        time.sleep(1)

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


def _activate(item, label: str) -> bool:
    """
    Opens a Logon Pad entry.

    Tries keyboard activation first (select the row, press Enter). That is
    driven through the accessibility API and does not depend on which window
    happens to be on top, so it cannot be stolen by another application the
    way a physical mouse click can.

    Falls back to double_click_input() only if selection is unavailable.
    """
    try:
        item.select()
        time.sleep(0.3)
        item.type_keys("{ENTER}")
        log.info(f"Opened connection '{label}' via keyboard activation.")
        return True
    except Exception as exc:
        log.debug(f"Keyboard activation failed for '{label}': {exc}")

    try:
        item.set_focus()
    except Exception:
        pass
    try:
        item.double_click_input()
        log.info(f"Double-clicked connection '{label}'.")
        return True
    except Exception as exc:
        log.warning(f"Could not activate connection '{label}': {exc}")
        return False


def select_connection(connection_name: str, timeout: int = 10):
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
                if _activate(item, connection_name):
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
                    if not _activate(item, title):
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
    raise TimeoutError(
        f"Could not find connection '{connection_name}' in {where}. "
        f"Entries present: {available}. "
        f"Set the matching *_SAP_CONNECTION_NAME in .env "
        f"(names are per-workstation and case-sensitive)."
    )