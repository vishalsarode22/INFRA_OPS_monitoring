"""
Is there a usable desktop for SAP GUI right now?

    "ok"            the session is connected and unlocked
    "locked"        Win+L / lock screen: SAP GUI may run, but screenshots can be black
    "disconnected"  the Remote Desktop window was closed without the
                    keep-desktop task (packaging/windows/install_keep_awake.ps1)
    "unknown"       not Windows, or the question could not be answered

Read-only: asks Windows, changes nothing.
"""
from __future__ import annotations

import os

WTS_CONNECT_STATE = 8          # WTS_INFO_CLASS.WTSConnectState
WTS_DISCONNECTED = 4           # WTS_CONNECTSTATE_CLASS.WTSDisconnected
DESKTOP_SWITCHDESKTOP = 0x0100


def state() -> str:
    if os.name != "nt":
        return "unknown"
    try:
        import ctypes
        from ctypes import wintypes
        wts = ctypes.WinDLL("wtsapi32")
        wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        buf = ctypes.c_void_p()
        size = wintypes.DWORD()
        if wts.WTSQuerySessionInformationW(None, wintypes.DWORD(0xFFFFFFFF), WTS_CONNECT_STATE,
                                           ctypes.byref(buf), ctypes.byref(size)):
            try:
                connect = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
            finally:
                wts.WTSFreeMemory(buf)
            if connect == WTS_DISCONNECTED:
                return "disconnected"
        user32 = ctypes.WinDLL("user32")
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        handle = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not handle:
            return "locked"
        user32.CloseDesktop(handle)
        return "ok"
    except Exception:  # noqa: BLE001
        return "unknown"


EXPLAIN = {
    "locked": ("the Windows screen of the monitoring VM was locked (Win+L) -- screenshots may come out "
               "black. Leave the VM by closing the Remote Desktop window instead of locking it."),
    "disconnected": ("the monitoring VM's Windows session was disconnected (Remote Desktop closed) and has no "
                     "screen -- run packaging\\windows\\install_keep_awake.ps1 once so it is kept alive "
                     "automatically."),
}
