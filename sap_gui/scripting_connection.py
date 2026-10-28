"""
SAP GUI Scripting connection helper.
Provides a reliable, deterministic way to get the active SAP session
for navigation and field data extraction -- replaces keyboard-automation-based
navigation, which proved unreliable for multi-step T-code switching.
IMPORTANT: requires a freshly started SAP GUI session (scripting does not
reliably attach to sessions that were already open before scripting was
enabled -- see project notes). If this fails, the user must fully close
SAP Logon/SAP GUI and log in fresh.
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
    import win32com.client
    _GUI_IMPORT_ERROR = None
except ImportError as _exc:
    _GUI_IMPORT_ERROR = _exc
    win32com = _GuiUnavailable('win32com', _exc)


# moved into guarded import: import win32com.client

from utils.logger import get_logger

log = get_logger(__name__, "application")


# ---------------------------------------------------------------------------
# Which connection is the MONITOR's.
#
# SAP Logon is single-instance: when a consultant already has SAP Logon open
# on this machine, the sweep's connection opens INSIDE their process, next to
# their sessions. "The first connection" (Children(0)) is then very likely
# THEIR session, and the sweep would type T-codes into a person's work.
#
# So the sweep pins its own connection: sap_gui.launcher.select_connection()
# records the connection ids that exist before it double-clicks the pad
# entry, and pins the one that appears afterwards. Every caller of
# get_scripting_session() -- login, T-code capture, recovery, RCA -- then
# gets a session of the pinned connection and nothing else. If the pinned
# connection has gone, this RAISES; it never falls back to someone else's.
#
# close_pinned_connection() ends only that connection, so cleanup after a
# sweep, after a hang (watchdog) or after an RCA capture leaves every other
# session on the machine exactly as it was.
# ---------------------------------------------------------------------------

_pinned_id: str | None = None
_pinned_desc: str | None = None      # Logon Pad entry of the pinned connection

# Who the monitor logged on as (set by sap_gui.connection.login after a
# verified logon). Used only when no connection is pinned: a session is then
# acceptable only if its SystemName/Client/User match this exactly.
_identity: dict = {}


def _engine():
    sap_gui_auto = win32com.client.GetObject("SAPGUI")
    return sap_gui_auto.GetScriptingEngine


def _children(collection):
    try:
        return [collection.Children(i) for i in range(int(collection.Children.Count))]
    except Exception:
        return []


def connection_ids():
    """Ids of all open connections, or None when the engine is unreachable."""
    try:
        return {str(c.Id) for c in _children(_engine())}
    except Exception:
        return None


def connection_ids_guarded(timeout: float = 5.0):
    """
    connection_ids() on a helper thread with its own COM apartment and a
    timeout. For callers on threads that may not have COM initialised (the
    watchdog) or where SAP Logon may be hung. None = unknown.
    """
    import threading as _th
    from sap_gui.reliability import com_thread
    box = {"ids": None}

    def _run():
        with com_thread():
            box["ids"] = connection_ids()

    t = _th.Thread(target=_run, daemon=True, name="list-sap-connections")
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.warning(f"Listing SAP connections did not return in {timeout:.0f}s (SAP Logon hung?).")
        return None
    return box["ids"]


def _description(conn) -> str:
    try:
        value = conn.Description
    except Exception:
        return ""
    return value.strip() if isinstance(value, str) else ""


def _pin(conn_id: str, description) -> None:
    global _pinned_id, _pinned_desc
    _pinned_id = str(conn_id)
    _pinned_desc = (description or "").strip() or None


# ---------------------------------------------------------------------------
# SAP GUI scripting security prompts.
#
# With "Notify when a script opens a connection" (WarnOnConnection) or
# "Notify when a script attaches to SAP GUI" (WarnOnAttach) switched on --
# WarnOnConnection is ON by default -- SAP GUI shows a modal "A script is
# trying to open a connection" box. A synchronous OpenConnection(entry, True)
# then blocks until someone clicks it, i.e. until the watchdog kills the
# sweep. Checked once per process; the open itself is asynchronous (see
# open_connection) so a prompt can no longer hang the sweep.
# ---------------------------------------------------------------------------

_SECURITY_KEY = r"Software\SAP\SAPGUI Front\SAP Frontend Server\Security"
_security_checked = False


def _read_security_flag(name: str):
    """Value of HKCU\\...\\Security\\<name> as int, or None (unset / non-Windows)."""
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SECURITY_KEY) as key:
            value, _type = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def check_scripting_security_prompts(force: bool = False) -> list:
    """
    Warn (once per process) when SAP GUI will prompt on scripted
    connect/attach. Returns the names of the options that are on.
    WarnOnConnection counts as on when the value is absent (SAP's default).
    """
    global _security_checked
    if _security_checked and not force:
        return []
    _security_checked = True
    enabled = []
    try:
        import winreg  # type: ignore  # noqa: F401
        on_windows = True
    except ImportError:
        on_windows = False
    if not on_windows:
        return enabled
    conn_flag = _read_security_flag("WarnOnConnection")
    if conn_flag is None or conn_flag != 0:
        enabled.append("WarnOnConnection")
    attach_flag = _read_security_flag("WarnOnAttach")
    if attach_flag:
        enabled.append("WarnOnAttach")
    if enabled:
        labels = {"WarnOnConnection": "'Notify when a script opens a connection'",
                  "WarnOnAttach": "'Notify when a script attaches to SAP GUI'"}
        log.warning(
            "SAP GUI scripting security prompt(s) enabled: "
            + ", ".join(labels[n] for n in enabled)
            + ". Every unattended sweep will wait on a pop-up. Switch them off in SAP GUI "
              "Options -> Accessibility & Scripting -> Scripting (HKCU\\" + _SECURITY_KEY + ").")
    return enabled


def _open_timeout() -> float:
    import os as _os
    try:
        return max(1.0, float(_os.environ.get("IBO_SAP_OPEN_CONNECTION_TIMEOUT", "20") or 20))
    except ValueError:
        return 20.0


def open_connection(description: str, timeout: float | None = None):
    """
    Open the Logon Pad entry `description` THROUGH SCRIPTING and pin the
    connection it creates. No window, no keystrokes.

    The call is ASYNCHRONOUS (OpenConnection(entry, False)): a synchronous
    open blocks forever behind SAP GUI's "a script is opening a connection"
    prompt. The new connection is then found by comparing connection ids
    before/after (and matching the entry's Description), polled for up to
    `timeout` seconds (IBO_SAP_OPEN_CONNECTION_TIMEOUT, default 20) until it
    has a session. Raises when it does not appear -- the caller then falls
    back to the Logon Pad.
    """
    import time as _t
    check_scripting_security_prompts()
    timeout = _open_timeout() if timeout is None else float(timeout)
    application = _engine()
    before = connection_ids()
    if before is None:
        raise RuntimeError("SAP GUI scripting engine is not reachable.")
    returned = application.OpenConnection(description, False)

    conn_id = None
    if returned is not None:
        try:
            rid = str(returned.Id)
            if rid and rid not in before:
                conn_id = rid
        except Exception:
            conn_id = None
    if conn_id:
        _pin(conn_id, _description(returned) or description)
    else:
        conn_id = pin_new_connection(before, timeout=timeout, description=description)
        if not conn_id:
            raise RuntimeError(
                f"OpenConnection('{description}') did not produce a new connection within "
                f"{timeout:.0f}s (a SAP GUI security prompt may be waiting).")

    # Wait until the connection has its first session (async open returns early).
    deadline = _t.time() + timeout
    conn = None
    while True:
        conn = _find_pinned(application)
        try:
            if conn is not None and int(conn.Children.Count) > 0:
                break
        except Exception:
            pass
        if _t.time() >= deadline:
            stale = _pinned_id
            unpin()
            raise RuntimeError(
                f"Connection '{description}' ({stale}) opened but no session appeared "
                f"within {timeout:.0f}s.")
        _t.sleep(0.5)
    log.info(f"Monitor connection opened via scripting and pinned: {_pinned_id} ('{description}').")
    return conn


def pin_new_connection(before, timeout: float = 30.0, description=None):
    """
    Pin the connection that was not open in `before`. Returns its id.

    With `description` (the Logon Pad entry just opened), a new connection
    whose Description does not match is never pinned, and when several new
    connections appear and the match is not unique nothing is pinned: a
    connection someone else opened in the same seconds must not become the
    monitor's.
    """
    import time as _t
    deadline = _t.time() + timeout
    before = before or set()
    want = (description or "").strip().lower()
    while _t.time() < deadline:
        now = connection_ids()
        if now:
            new = sorted(now - before)
            if new:
                chosen = None
                descs = {}
                if want:
                    try:
                        for c in _children(_engine()):
                            descs[str(c.Id)] = _description(c)
                    except Exception:
                        descs = {}
                    matching = [cid for cid in new if descs.get(cid, "").lower() == want]
                    if len(matching) == 1:
                        chosen = matching[0]
                    elif not matching and len(new) == 1 and not descs.get(new[0]):
                        chosen = new[0]          # Description not readable: the only new one
                elif len(new) == 1:
                    chosen = new[0]
                if chosen:
                    _pin(chosen, descs.get(chosen) or description)
                    log.info(f"Monitor connection pinned: {_pinned_id} "
                             f"({len(now) - 1} other connection(s) on this machine left untouched)")
                    return _pinned_id
        _t.sleep(0.5)
    log.warning("Could not identify the monitor's own SAP connection unambiguously "
                f"(entry '{description or '?'}', before={sorted(before)}, "
                f"now={sorted(connection_ids() or [])}); not pinning.")
    return None


def register_identity(client=None, user=None, system=None) -> None:
    """Record who the monitor is logged on as (see _identity)."""
    _identity.clear()
    for key, value in (("client", client), ("user", user), ("system", system)):
        value = str(value or "").strip()
        if value:
            _identity[key] = value


def identity() -> dict:
    return dict(_identity)


def pinned_connection_id():
    return _pinned_id


def unpin() -> None:
    global _pinned_id, _pinned_desc
    _pinned_id = None
    _pinned_desc = None
    _identity.clear()


def _find_pinned(application, conn_id=None, desc=None):
    if conn_id is None:
        conn_id, desc = _pinned_id, _pinned_desc
    for c in _children(application):
        try:
            if str(c.Id) != conn_id:
                continue
        except Exception:
            continue
        # Connection ids are reused once a connection closes: same id with a
        # different Logon Pad entry is someone else's connection.
        found = _description(c)
        if desc and found and found.lower() != desc.lower():
            log.warning(f"Connection {conn_id} is now '{found}', not the monitor's '{desc}'; "
                        "treating the monitor's connection as gone.")
            return None
        return c
    return None


def close_pinned_connection(timeout: float = 8.0) -> bool:
    """
    End the monitor's own connection only. Runs in a helper thread with a
    timeout, because a hung session can block a COM call forever and this is
    what the watchdog calls when a sweep has hung. Returns True when the
    connection is gone (or there was none).
    """
    global _pinned_id
    if not _pinned_id:
        return True
    import threading as _th
    from sap_gui.reliability import com_thread
    done = {"ok": False, "err": None}
    target_id, target_desc = _pinned_id, _pinned_desc

    def _close():
        with com_thread():          # COM must be initialised in this thread
            _close_in_apartment()

    def _close_in_apartment():
        try:
            conn = _find_pinned(_engine(), target_id, target_desc)
            if conn is None:
                done["ok"] = True
                return
            try:
                conn.CloseConnection()
            except Exception:
                # Older GUI releases: close each session window instead.
                for s in _children(conn):
                    try:
                        s.findById("wnd[0]").close()
                        try:
                            s.findById("wnd[1]/usr/btnSPOP-OPTION1").press()   # "Yes, log off"
                        except Exception:
                            pass
                    except Exception:
                        pass
            done["ok"] = True
        except Exception as exc:  # noqa: BLE001
            done["err"] = exc

    t = _th.Thread(target=_close, daemon=True, name="close-monitor-connection")
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.error(f"Closing the monitor connection {target_id} did not return in {timeout:.0f}s "
                  "(session hung). Other users' sessions were NOT touched.")
        return False
    if done["err"] is not None:
        log.warning(f"Closing the monitor connection {target_id} failed: {done['err']}")
        return False
    log.info(f"Monitor connection {target_id} closed; other sessions untouched.")
    _pinned_id = None
    return True


def _matches_identity(session, ident: dict) -> bool:
    try:
        info = session.Info
        have = {"user": str(info.User or ""), "client": str(info.Client or ""),
                "system": str(info.SystemName or "")}
    except Exception:
        return False
    return all(have[k].strip().lower() == str(v).strip().lower() for k, v in ident.items())


def get_scripting_session(quiet: bool = False, system=None, client=None, user=None):
    """
    Returns a session of the MONITOR's connection via the scripting engine.

    With a pinned connection (the normal case, see above) only that
    connection is considered. Without one, only a session whose
    Info.SystemName / Info.Client / Info.User match the monitor's identity
    (the arguments, else what login() registered; client AND user required)
    is accepted -- and only when exactly one connection matches. It never
    falls back to "the first" or "the only" connection: that could be a
    consultant's session. Raises RuntimeError with a clear message otherwise.

    quiet=True for callers that POLL while a logon screen is still opening
    (a fresh connection has no session for a second or two -- longer through
    the SSH tunnel). They log their own result once; logging every poll as
    an ERROR put 105 "no sessions attached" lines on the Errors page.
    """
    try:
        application = _engine()
        if application.Children.Count == 0:
            raise RuntimeError("No SAP connections found in scripting engine.")
        if _pinned_id:
            connection = _find_pinned(application)
            if connection is None:
                raise RuntimeError(f"The monitor's SAP connection {_pinned_id} is no longer open.")
        else:
            ident = dict(_identity)
            for key, value in (("system", system), ("client", client), ("user", user)):
                if str(value or "").strip():
                    ident[key] = str(value).strip()
            if not (ident.get("user") and ident.get("client")):
                raise RuntimeError(
                    f"{application.Children.Count} SAP connection(s) open on this machine and the "
                    "monitor's own one is not identified (none pinned, no system/client/user to "
                    "match); not driving a session that may belong to someone else.")
            owners = []
            for conn in _children(application):
                hits = [sess for sess in _children(conn) if _matches_identity(sess, ident)]
                if hits:
                    owners.append((conn, hits[0]))
            if len(owners) != 1:
                raise RuntimeError(
                    f"{len(owners)} SAP connection(s) match the monitor's identity "
                    f"(system={ident.get('system', '*')}, client={ident['client']}, "
                    f"user={ident['user']}); need exactly one. Not driving a session that may "
                    "belong to someone else.")
            session = owners[0][1]
            log.info(f"Scripting session acquired by identity match: "
                     f"System={session.Info.SystemName}, Client={session.Info.Client}, "
                     f"User={session.Info.User}")
            return session

        if connection.Children.Count == 0:
            raise RuntimeError(
                "SAP connection found but no sessions attached. "
                "Try fully closing SAP Logon/SAP GUI and logging in fresh."
            )

        session = connection.Children(0)
        log.info(
            f"Scripting session acquired: System={session.Info.SystemName}, "
            f"Client={session.Info.Client}, User={session.Info.User}"
        )
        return session

    except Exception as e:
        (log.debug if quiet else log.error)(f"Failed to get scripting session: {e}")
        raise
