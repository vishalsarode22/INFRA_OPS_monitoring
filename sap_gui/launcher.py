"""
Fully automates launching SAP Logon and selecting a connection, so the
whole pipeline can run unattended from a cold start.
Kills any existing SAP processes first, since SAP GUI Scripting has
been observed to only reliably attach to sessions started fresh
(see project notes) -- reusing an already-open SAP Logon Pad risks
the same "0 sessions" scripting issue encountered earlier.
"""

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


def kill_sap_processes():
    for proc in ["saplogon.exe", "sapgui.exe"]:
        try:
            subprocess.run(["taskkill", "/F", "/IM", proc], capture_output=True)
        except Exception as e:
            log.debug(f"taskkill {proc} skipped/failed (likely not running): {e}")
    time.sleep(1)


def launch_saplogon(exe_path: str, timeout: int = 30):  # was 15
    log.info(f"Launching SAP Logon: {exe_path}")
    subprocess.Popen([exe_path])

    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            pad = Desktop(backend="win32").window(title="SAP Logon 800")
            pad.wait("visible", timeout=2)
            log.info("SAP Logon 800 pad is visible.")
            time.sleep(_PAD_SETTLE)
            return pad
        except Exception:
            time.sleep(1)

    raise TimeoutError("SAP Logon 800 pad did not appear in time.")


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

    while time.time() < end_time:
        try:
            app = Application(backend="uia").connect(title="SAP Logon 800")
            window = app.window(title="SAP Logon 800")

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
    raise TimeoutError(
        f"Could not find connection '{connection_name}' in SAP Logon Pad. "
        f"Entries present: {available}. "
        f"Set the matching *_SAP_CONNECTION_NAME in .env "
        f"(names are per-workstation and case-sensitive)."
    )
