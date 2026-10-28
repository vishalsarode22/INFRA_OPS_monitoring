"""
Cross-process logon guard -- stops the monitor from locking its own account.

WHAT HAPPENED
-------------
Production: "Password logon no longer possible - too many failed attempts".
The failed-logon cooldown was an in-memory dict, so every process had its
own: the dashboard's pool workers, the sweep process and the background perf
refresh each learned separately that the password was wrong -- and each spent
one more failed attempt doing so. The auth cooldown also expired after 900 s
and the logon was simply tried again. A wrong password in .env therefore cost
a few failed attempts per process every 15 minutes, which is exactly what
SAP's login/fails_to_user_lock counts.

WHAT THIS DOES
--------------
A small SQLite database under the project's runtime directory, shared by
every process. Each logon target is keyed by

    (ashost|mshost, sysnr, client, user, fingerprint(password))

When a logon fails because of the credentials (RFC_LOGON_FAILURE, "Name or
password is incorrect", "Password logon no longer possible", user locked),
the key is BLOCKED -- in every process, across restarts, with no expiry.
No further logon is attempted for that key until either

  * the password changes (a different fingerprint is a different key), or
  * an operator calls clear_logon_block(system) -- or runs
        python -m collectors.rfc_logon_guard --clear <SYSTEM>

REASONS (review fix)
--------------------
Not every refusal is permanent. The block records WHY:

  * invalid_password -- "Name or password is incorrect": blocked until the
    password changes or an operator clears it (every further try would
    count towards the lock).
  * locked          -- "user is locked", "too many failed attempts",
    "Password logon no longer possible": SAP may auto-unlock at midnight or
    an admin may unlock in SU01 without changing the password. ONE logon is
    retried every IBO_LOGON_LOCKED_RETRY_HOURS (default 6) -- claimed
    atomically in the shared store, so only one process spends it. If that
    retry is refused as wrong password, the block becomes invalid_password.
  * expired         -- "password expired / must be changed": blocked until
    the password changes or an operator clears it.

The dashboard lists blocks (GET /api/logon-blocks) and an operator can clear
one (POST /api/logon-blocks/{system}/clear).

Logons for the same key are also serialised across processes (a lock file
per key), so two processes starting at the same moment with a bad password
spend ONE failed attempt between them, not two.

The fingerprint is a salted PBKDF2-SHA256 of the password (16 hex chars), not
a bare sha256: a fast unsalted hash of a password on disk would be a gift to
anyone who can read the file. The salt is a per-installation random value
kept next to the database.

This module never logs or stores a password.
"""

from __future__ import annotations

import hashlib
import json
import os as _os
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from utils.logger import get_logger
from utils.paths import BASE_DIR

log = get_logger(__name__)

BLOCKED_MESSAGE = ("Logon blocked after failed password -- fix credentials in .env, "
                   "then clear (clear_logon_block('{system}'))")

CLEAR_HINT = ("clear it from the dashboard or with "
              "python -m collectors.rfc_logon_guard --clear {system}")

REASON_WRONG_PASSWORD = "invalid_password"
REASON_LOCKED = "locked"
REASON_EXPIRED = "expired"

_REASON_TEXT = {
    REASON_WRONG_PASSWORD: "Logon blocked after failed password (name or password is incorrect) "
                           "-- fix credentials in .env",
    REASON_LOCKED: "Logon blocked: SAP user is locked / too many failed attempts",
    REASON_EXPIRED: "Logon blocked: the SAP password has expired or must be changed "
                    "-- set a new password in SAP and in .env",
}

_EXPIRED_MARKERS = ("PASSWORD HAS EXPIRED", "PASSWORD EXPIRED", "PASSWORD IS NO LONGER VALID",
                    "INITIAL PASSWORD", "MUST BE CHANGED", "CHANGE YOUR PASSWORD",
                    "CHANGE THE PASSWORD")
_LOCKED_MARKERS = ("USER IS LOCKED", "USER LOCKED", "TOO MANY FAILED ATTEMPTS",
                   "PASSWORD LOGON NO LONGER POSSIBLE", "LOCKED BY ADMINISTRATOR")


def locked_retry_hours() -> float:
    try:
        return max(0.0, float(_os.environ.get("IBO_LOGON_LOCKED_RETRY_HOURS", "6") or 6))
    except ValueError:
        return 6.0


def classify_reason(error) -> str:
    """invalid_password | locked | expired for a refused logon's error text."""
    up = str(error or "").upper()
    if any(m in up for m in _EXPIRED_MARKERS):
        return REASON_EXPIRED
    if any(m in up for m in _LOCKED_MARKERS):
        return REASON_LOCKED
    return REASON_WRONG_PASSWORD

# Texts / keys that mean "the credentials are wrong or the user cannot log on".
_AUTH_MARKERS = (
    "RFC_LOGON_FAILURE",
    "NAME OR PASSWORD IS INCORRECT",
    "PASSWORD LOGON NO LONGER POSSIBLE",
    "TOO MANY FAILED ATTEMPTS",
    "USER IS LOCKED",
    "USER LOCKED",
    "PASSWORD IS NO LONGER VALID",
    "PASSWORD HAS EXPIRED",
    "INITIAL PASSWORD",
    "LOGIN FAILED",            # ODBC / ASE
    "LOGIN INCORRECT",
    "SQLSTATE=28000",
    "(28000)",
)

_LOGON_LOCK_WAIT_SECONDS = float(_os.environ.get("IBO_LOGON_LOCK_WAIT_SECONDS", "60") or 60)
_PBKDF2_ROUNDS = 60_000

_tlock = threading.RLock()
_key_locks: dict[str, threading.Lock] = {}
_fp_cache: dict[tuple, str] = {}


def runtime_dir() -> str:
    path = _os.environ.get("IBO_RUNTIME_DIR") or _os.path.join(BASE_DIR, "runtime")
    _os.makedirs(path, exist_ok=True)
    return path


def db_path() -> str:
    return _os.environ.get("IBO_LOGON_GUARD_DB") or _os.path.join(runtime_dir(), "rfc_logon_guard.sqlite3")


def _connect() -> sqlite3.Connection:
    path = db_path()
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.execute("""CREATE TABLE IF NOT EXISTS blocks (
                       key TEXT PRIMARY KEY, system TEXT, host TEXT, sysnr TEXT,
                       client TEXT, user TEXT, fingerprint TEXT, kind TEXT,
                       error TEXT, blocked_at TEXT, pid INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(blocks)").fetchall()}
    for col in ("reason", "next_retry_at"):
        if col not in cols:
            try:
                con.execute(f"ALTER TABLE blocks ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass                     # another process added it first
    return con


def _salt() -> bytes:
    """Per-installation random salt, created on first use, kept in the DB."""
    with _tlock:
        con = _connect()
        try:
            row = con.execute("SELECT v FROM meta WHERE k = 'salt'").fetchone()
            if row:
                return bytes.fromhex(row[0])
            value = secrets.token_hex(16)
            con.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('salt', ?)", (value,))
            con.commit()
            row = con.execute("SELECT v FROM meta WHERE k = 'salt'").fetchone()
            return bytes.fromhex(row[0])
        finally:
            con.close()


def fingerprint(identity: str, password: str) -> str:
    """16 hex chars identifying this password for this identity. Not reversible."""
    memo = (db_path(), identity, hashlib.sha256(str(password).encode("utf-8")).digest())
    with _tlock:
        hit = _fp_cache.get(memo)
    if hit:
        return hit
    salt = _salt() + identity.encode("utf-8")
    fp = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, _PBKDF2_ROUNDS).hex()[:16]
    with _tlock:
        _fp_cache[memo] = fp
    return fp


class LogonTarget:
    """Who we are about to log on as. Built from pyrfc params or ASE params."""

    def __init__(self, system: str, host: str, sysnr: str, client: str, user: str,
                 password: str, kind: str = "rfc"):
        self.system = system or ""
        self.host = (host or "").strip().lower()
        self.sysnr = str(sysnr or "").strip()
        self.client = str(client or "").strip()
        self.user = (user or "").strip().upper()
        self.kind = kind
        ident = f"{kind}|{self.host}|{self.sysnr}|{self.client}|{self.user}"
        self.fingerprint = fingerprint(ident, password or "")
        self.key = f"{ident}|{self.fingerprint}"
        self.identity = ident

    @classmethod
    def from_rfc_params(cls, system: str, params: dict) -> "LogonTarget":
        host = params.get("ashost") or params.get("mshost") or ""
        sysnr = params.get("sysnr") or params.get("msserv") or params.get("group") or ""
        return cls(system, host, sysnr, params.get("client", ""), params.get("user", ""),
                   params.get("passwd", ""), "rfc")

    @classmethod
    def from_ase_params(cls, system: str, params: dict) -> "LogonTarget":
        return cls(system, f"{params.get('host', '')}:{params.get('port', '')}", "ase",
                   params.get("database", ""), params.get("username", ""),
                   params.get("password", ""), "ase")


def is_auth_failure(exc_or_text) -> bool:
    """True when an exception / error text means the credentials were refused."""
    if isinstance(exc_or_text, BaseException):
        if type(exc_or_text).__name__ == "LogonError":
            return True
        key = str(getattr(exc_or_text, "key", "") or "").upper()
        if key == "RFC_LOGON_FAILURE":
            return True
        text = f"{key} {exc_or_text}"
    else:
        text = str(exc_or_text or "")
    up = text.upper()
    return any(m in up for m in _AUTH_MARKERS)


def block_info(target: LogonTarget) -> dict | None:
    """The stored block for exactly this key (same password), or None."""
    try:
        con = _connect()
        try:
            row = con.execute("SELECT system, error, blocked_at, pid, reason, next_retry_at "
                              "FROM blocks WHERE key = ?", (target.key,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        # Fail CLOSED on a broken guard store would stop all monitoring; fail
        # open but loudly. The in-memory cooldown still applies.
        log.error(f"logon guard store unreadable ({exc}); continuing without it")
        return None
    if not row:
        return None
    return {"system": row[0], "error": row[1], "blocked_at": row[2], "pid": row[3],
            "reason": row[4] or classify_reason(row[1]), "next_retry_at": row[5]}


def blocked_message(system: str, block: dict | None = None) -> str:
    """Operator text for a blocked logon: the reason and how to clear it."""
    reason = (block or {}).get("reason") or REASON_WRONG_PASSWORD
    text = _REASON_TEXT.get(reason, _REASON_TEXT[REASON_WRONG_PASSWORD])
    if reason == REASON_LOCKED:
        nxt = (block or {}).get("next_retry_at")
        text += (f"; one automatic retry at {nxt.replace('T', ' ')}" if nxt
                 else "; retried automatically")
        text += " (unlock the user in SU01, then"
    else:
        text += " (then"
    since = (block or {}).get("blocked_at")
    if since:
        text = f"{text} {CLEAR_HINT.format(system=system)}; blocked since {since.replace('T', ' ')})"
    else:
        text = f"{text} {CLEAR_HINT.format(system=system)})"
    return text


def _retry_due(block: dict | None, now: datetime | None = None) -> bool:
    if not block or block.get("reason") != REASON_LOCKED:
        return False
    nxt = block.get("next_retry_at")
    if not nxt:
        return True
    try:
        return datetime.fromisoformat(nxt) <= (now or datetime.now())
    except ValueError:
        return True


def is_blocked_now(target: LogonTarget) -> dict | None:
    """
    The block that stops a logon right now, or None. A 'locked' block whose
    retry time has come returns None so the caller goes on to guarded_logon,
    which claims the single retry atomically.
    """
    block = block_info(target)
    if block and _retry_due(block):
        return None
    return block


def _claim_retry(target: LogonTarget) -> bool:
    """Atomically move next_retry_at forward; True for the ONE process that wins."""
    from datetime import timedelta
    now = datetime.now()
    nxt = (now + timedelta(hours=locked_retry_hours())).isoformat(timespec="seconds")
    try:
        con = _connect()
        try:
            cur = con.execute(
                "UPDATE blocks SET next_retry_at = ? WHERE key = ? AND reason = ? "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?)",
                (nxt, target.key, REASON_LOCKED, now.isoformat(timespec="seconds")))
            con.commit()
            won = (cur.rowcount or 0) == 1
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.error(f"[{target.system}] could not claim locked-user retry ({exc})")
        return False
    if won:
        log.warning(f"[{target.system}] user {target.user} was locked; trying ONE logon now "
                    f"(next retry not before {nxt}).")
    return won


def record_failure(target: LogonTarget, error: str, reason: str | None = None) -> dict:
    """Block this key everywhere. Never stores the password. Returns the block."""
    from datetime import timedelta
    reason = reason or classify_reason(error)
    now = datetime.now()
    nxt = None
    if reason == REASON_LOCKED:
        nxt = (now + timedelta(hours=locked_retry_hours())).isoformat(timespec="seconds")
    since = now.isoformat(timespec="seconds")
    try:
        con = _connect()
        try:
            old = con.execute("SELECT blocked_at, reason, next_retry_at FROM blocks WHERE key = ?",
                              (target.key,)).fetchone()
            if old and old[0]:
                since = old[0]                      # keep "blocked since" across retries
                if reason == REASON_LOCKED and old[1] == REASON_LOCKED and old[2] \
                        and old[2] > (nxt or ""):
                    nxt = old[2]
            con.execute("""INSERT OR REPLACE INTO blocks
                           (key, system, host, sysnr, client, user, fingerprint, kind,
                            error, blocked_at, pid, reason, next_retry_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (target.key, target.system, target.host, target.sysnr, target.client,
                         target.user, target.fingerprint, target.kind, str(error or "")[:500],
                         since, _os.getpid(), reason, nxt))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.error(f"[{target.system}] could not persist logon block ({exc})")
    if reason == REASON_LOCKED:
        tail = (f"One logon will be retried at {nxt} (IBO_LOGON_LOCKED_RETRY_HOURS); unlock the "
                f"user in SU01 or {CLEAR_HINT.format(system=target.system)}.")
    else:
        tail = (f"All further logons with this password are BLOCKED in every process until the "
                f"password in .env changes -- or {CLEAR_HINT.format(system=target.system)}.")
    log.error(f"[{target.system}] RFC logon refused for user {target.user} [{reason}] "
              f"({str(error or '')[:160]}). {tail}")
    return {"system": target.system, "error": str(error or "")[:500], "blocked_at": since,
            "reason": reason, "next_retry_at": nxt}


def record_success(target: LogonTarget) -> None:
    """A logon worked: drop stale blocks for the same identity (old passwords)."""
    try:
        con = _connect()
        try:
            # Every block for this identity goes: old passwords, and this
            # key's own 'locked' block after a successful retry.
            prefix = target.identity + "|"
            con.execute("DELETE FROM blocks WHERE substr(key, 1, ?) = ?",
                        (len(prefix), prefix))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def clear_logon_block(system: str | None = None) -> int:
    """
    Operator action: allow logons again for `system` (all systems when None).
    Call after fixing the credentials in .env (or unlocking the user in SU01).
    Returns how many blocks were removed.
    """
    try:
        con = _connect()
        try:
            if system is None:
                cur = con.execute("DELETE FROM blocks")
            else:
                cur = con.execute("DELETE FROM blocks WHERE system = ?", (system,))
            con.commit()
            n = cur.rowcount or 0
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.error(f"clear_logon_block({system!r}) failed: {exc}")
        return 0
    log.warning(f"logon block cleared for {system or 'ALL systems'} ({n} entr{'y' if n == 1 else 'ies'})")
    return n


def list_blocks(system: str | None = None) -> list[dict]:
    """Current blocks (no secrets): for the dashboard / CLI."""
    try:
        con = _connect()
        try:
            q = ("SELECT system, kind, host, sysnr, client, user, error, blocked_at, reason, "
                 "next_retry_at FROM blocks")
            args: tuple = ()
            if system is not None:
                q += " WHERE system = ?"
                args = (system,)
            rows = con.execute(q + " ORDER BY blocked_at", args).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    cols = ("system", "kind", "host", "sysnr", "client", "user", "error", "blocked_at",
            "reason", "next_retry_at")
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["reason"] = d.get("reason") or classify_reason(d.get("error"))
        if d["reason"] != REASON_LOCKED:
            d["next_retry_at"] = None
        out.append(d)
    return out


def logon_block_status(system: str) -> dict:
    """{blocked, message, blocks} for a system name, whatever its password now is."""
    blocks = list_blocks(system)
    return {"blocked": bool(blocks),
            "message": blocked_message(system, blocks[-1]) if blocks else None,
            "blocks": blocks}


# ---------------------------------------------------------------------------
# Cross-process serialisation of logons per key
# ---------------------------------------------------------------------------

@contextmanager
def logon_lock(target: LogonTarget, wait_seconds: float | None = None):
    """
    Hold an exclusive lock for this logon key across threads AND processes.
    Yields True when acquired, False when it could not be had in time (the
    caller must then NOT attempt the logon).
    """
    wait = _LOGON_LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    with _tlock:
        tl = _key_locks.setdefault(target.key, threading.Lock())
    deadline = time.monotonic() + wait
    if not tl.acquire(timeout=max(wait, 0.0)):
        yield False
        return
    fh = None
    try:
        lock_dir = _os.path.join(_os.path.dirname(db_path()) or ".", "logon_locks")
        _os.makedirs(lock_dir, exist_ok=True)
        name = hashlib.sha256(target.key.encode("utf-8")).hexdigest()[:24] + ".lock"
        fh = open(_os.path.join(lock_dir, name), "a+b")
        got = False
        while True:
            try:
                if sys.platform.startswith("win"):
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        try:
            yield got
        finally:
            if got:
                try:
                    if sys.platform.startswith("win"):
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        tl.release()


def guarded_logon(target: LogonTarget, connect):
    """
    Run `connect()` (the actual logon) under the guard.

    Returns (connection, error, blocked). Never raises.
      * blocked=True: no logon was attempted (or the attempt was refused for
        credentials and the key is now blocked); error is the operator message.
    """
    block = block_info(target)
    if block and not _retry_due(block):
        return None, blocked_message(target.system, block), True
    with logon_lock(target) as got:
        if not got:
            return None, (f"another process is logging on to {target.system} as "
                          f"{target.user}; skipped this attempt"), False
        # Re-check: the process we waited for may have just been refused.
        block = block_info(target)
        if block:
            # Only a 'locked' block whose retry is due, and only the ONE
            # process that claims it in the shared store, may try.
            if not (_retry_due(block) and _claim_retry(target)):
                return None, blocked_message(target.system, block_info(target) or block), True
        try:
            conn = connect()
        except Exception as exc:  # noqa: BLE001
            text = f"{type(exc).__name__}: {str(exc).strip()}"
            if is_auth_failure(exc):
                new_block = record_failure(target, text)
                return None, (f"{blocked_message(target.system, new_block)}. "
                              f"SAP said: {text[:240]}"), True
            if block:
                # The retry hit a network error: stay blocked until the next slot.
                return None, (f"{blocked_message(target.system, block_info(target) or block)}. "
                              f"Retry failed: {str(exc)[:200]}"), True
            return None, str(exc), False
    record_success(target)
    return conn, None, False


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="IBOPS RFC/DB logon guard")
    ap.add_argument("--list", action="store_true", help="show current logon blocks")
    ap.add_argument("--clear", metavar="SYSTEM", help="clear the block for SYSTEM ('ALL' for every system)")
    args = ap.parse_args(argv)
    if args.clear:
        n = clear_logon_block(None if args.clear.upper() == "ALL" else args.clear)
        print(f"cleared {n} block(s)")
        return 0
    print(json.dumps(list_blocks(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
