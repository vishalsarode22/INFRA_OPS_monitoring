"""
Opens SAP transactions (T-codes) via SAP GUI Scripting -- deterministic,
does not rely on OS-level keyboard focus like the earlier keystroke-based
approach, which proved unreliable for multi-step navigation.
"""

import time
from utils.logger import get_logger

log = get_logger(__name__, "application")


def open_tcode(session, tcode: str, wait_seconds: float = 2.0, max_busy_wait: float = 8.0) -> bool:
    """
    Opens a T-code using the scripting API's command field.
    Waits for the session to report not-busy (data finished loading)
    rather than a fixed sleep, since some T-codes take longer to
    populate data than others.
    """
    log.info(f"Opening T-code via scripting: {tcode}")

    try:
        session.findById("wnd[0]/tbar[0]/okcd").text = f"/n{tcode}"
        session.findById("wnd[0]").sendVKey(0)  # Enter
    except Exception as e:
        log.error(f"Scripting call failed while opening {tcode}: {e}")
        raise

    # Wait for initial screen transition
    time.sleep(wait_seconds)

    # Then wait for SAP to report "not busy" (data finished loading),
    # up to max_busy_wait seconds total.
    end_time = time.time() + max_busy_wait
    while time.time() < end_time:
        try:
            if not session.findById("wnd[0]").Busy:
                break
        except Exception:
            break
        time.sleep(0.3)

    # Small extra settle time after busy clears, for rendering to finish
    time.sleep(0.5)

    try:
        current_tcode = session.Info.Transaction
        log.info(f"After opening {tcode}, session.Info.Transaction = '{current_tcode}'")
    except Exception as e:
        log.warning(f"Could not read session.Info.Transaction: {e}")
        current_tcode = ""

    return tcode.upper() in current_tcode.upper()

def wait_until_not_busy(session, max_busy_wait: float = 8.0, settle_time: float = 0.5):
    """
    Waits until the SAP session reports not-busy (data finished loading),
    then a small extra settle time for rendering. Shared by open_tcode()
    and the per-tcode action functions.
    """
    end_time = time.time() + max_busy_wait
    while time.time() < end_time:
        try:
            if not session.findById("wnd[0]").Busy:
                break
        except Exception:
            break
        time.sleep(0.3)
    time.sleep(settle_time)


def recover_session(session):
    """
    Best-effort recovery from a mid-transaction failure, used between
    retry attempts in collect_tcode_evidence(). Closes any stray popup
    windows (wnd[1], wnd[2], ...) that might be blocking input, then
    sends a bare "/n" to cancel back to the easy-access screen -- a
    clean, known-good starting point for the next attempt.
    Never raises: if recovery itself fails, the caller's retry will
    simply also fail and move on, which is an acceptable outcome (this
    is a best-effort safety net, not a guarantee).
    """
    try:
        # Close any open popups/dialogs, highest index first
        for i in range(4, 0, -1):
            try:
                popup = session.findById(f"wnd[{i}]")
                popup.sendVKey(12)  # F12 = Cancel, the safest universal "close this" key
                time.sleep(0.3)
            except Exception:
                continue
    except Exception as e:
        log.debug(f"recover_session: popup cleanup step failed (continuing): {e}")

    try:
        session.findById("wnd[0]/tbar[0]/okcd").text = "/n"
        session.findById("wnd[0]").sendVKey(0)
        time.sleep(1.5)
        wait_until_not_busy(session)
        log.info("Recovered session back to easy-access screen for retry.")
    except Exception as e:
        log.warning(f"recover_session: could not return to easy-access screen: {e}")


def goto_tcode(session, tcode: str, wait_seconds: float = 1.5):
    """
    Navigates to a T-code without the verification/return-value logic
    of open_tcode() -- used as the first step inside action functions
    in tcode_actions.py, which then perform additional recorded steps.
    """
    session.findById("wnd[0]/tbar[0]/okcd").text = f"/n{tcode}"
    session.findById("wnd[0]").sendVKey(0)
    time.sleep(wait_seconds)
    wait_until_not_busy(session)