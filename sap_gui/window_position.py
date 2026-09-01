"""
EXPERIMENTAL: moves SAP GUI windows to an off-screen position so the
automation doesn't visibly pop up on the user's desktop during a run.

Gated behind .env GUI_HIDDEN_MODE=true (default: false/off). Off by
default because it hasn't been verified against the real SAP Logon Pad
yet -- test it deliberately, watching the first run, before relying on
it. If anything misbehaves (connection not clicking, login fields not
filling), set GUI_HIDDEN_MODE=false to go straight back to the known-
working visible behavior with no other changes needed.

Why "off-screen" and not "minimized": pywinauto's type_keys() targets
whatever window has focus regardless of its on-screen position, so it
works fine either way. But minimizing a window and then calling
type_keys() -> set_focus() typically RESTORES it automatically (Windows
won't let a minimized window receive focus without un-minimizing) --
so minimizing gets silently undone the moment login starts typing.
Moving off-screen avoids that: the window stays in the "restored"
state (not minimized), just positioned somewhere the user's monitor
doesn't show, so focus/keyboard input works normally and it never
visibly appears.

Why this SPECIFIC offset and not an extreme value like -32000: mouse
click simulation (used once, for the SAP Logon Pad connection list)
uses absolute screen coordinates that Windows clamps to the current
virtual screen's bounding box. On a single-monitor machine, going far
outside that box means clicks silently miss. A modest offset (window
placed just above/left of the visible desktop, still technically
inside a generously-sized virtual coordinate region) is safer than a
large one -- but this has NOT been tested against your actual SAP
Logon Pad, hence the opt-in flag.
"""
import os
import win32gui
import win32con

from utils.logger import get_logger

log = get_logger(__name__, "application")

# Deliberately modest -- see module docstring. Adjust if you find a
# value that reliably works better on your specific machine/monitor setup.
OFFSCREEN_X = -2000
OFFSCREEN_Y = -2000


def is_hidden_mode_enabled() -> bool:
    return os.getenv("GUI_HIDDEN_MODE", "false").strip().lower() == "true"


def move_offscreen(window):
    """
    Moves a pywinauto window wrapper off-screen, preserving its current
    size. Safe to call even if hidden mode is disabled -- callers should
    check is_hidden_mode_enabled() themselves before calling this, but
    this function itself has no side effects beyond the move.
    """
    try:
        hwnd = window.handle
        rect = win32gui.GetWindowRect(hwnd)
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        win32gui.SetWindowPos(
            hwnd, win32con.HWND_TOP, OFFSCREEN_X, OFFSCREEN_Y, width, height,
            win32con.SWP_NOACTIVATE,
        )
        log.debug(f"Moved window '{window.window_text()}' off-screen to ({OFFSCREEN_X}, {OFFSCREEN_Y}).")
    except Exception as e:
        log.warning(f"Could not move window off-screen (continuing visibly): {e}")
