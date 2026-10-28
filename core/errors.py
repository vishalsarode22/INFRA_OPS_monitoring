"""
Real-time exception handling: every unhandled error, wherever it happens,
is caught, grouped, shown live and -- the first time it is seen -- mailed.

WHERE ERRORS ARE CAUGHT
    web       any exception escaping an HTTP endpoint (FastAPI handler in
              dashboard/app.py); the browser gets a 500 with an error id
    thread    any exception escaping any thread (threading.excepthook)
    process   an exception escaping the main thread (sys.excepthook)
    loop      a supervised background loop that crashed or stopped
              (core/supervisor.py restarts it and reports here)
    sweep     a sweep attempt that failed (main.py)
    log       any log record at ERROR level or above, from any module --
              the code already logs most handled failures that way
    browser   JavaScript errors and failed calls on the dashboard pages
              (shell.js posts them to /api/errors/client)

GROUPING
Errors are grouped by a signature: the exception type plus where it was
raised (the innermost project frames), with numbers, quoted values and
addresses stripped from the message. "Connection to 10.1.0.193 timed out"
and "... to 10.1.0.37 ..." are one group; each group counts its
occurrences and keeps the latest traceback.

WHO HEARS ABOUT IT
A NEW group (or one that comes back after being marked resolved) goes on
the IBOPS timeline and, for the sources in IBO_ERROR_MAIL_SOURCES (default
web, thread, process, loop, sweep, log), is mailed to Basis -- once per group per
IBO_ERROR_MAIL_HOURS (default 6), at most IBO_ERROR_MAIL_DAILY_CAP (20) a day.
Browser errors are shown but not mailed by default (a closed laptop lid
produces "failed to fetch").

The Errors page (/errors) polls every 10 seconds; the sidebar shows the
number of open groups.

NEVER RAISES, NEVER LOOPS
capture() swallows its own failures, and a thread-local guard stops an
error raised while reporting an error from being reported again (mail
failing -> logged at ERROR -> captured -> mail failing ...).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import sys
import threading
import time
import traceback
from datetime import datetime

_guard = threading.local()
_installed = False
_rate: dict[str, list[float]] = {}
_rate_lock = threading.Lock()
_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCES = ("web", "thread", "process", "loop", "sweep", "log", "browser", "job")

_DDL = """
CREATE TABLE IF NOT EXISTS errors (
    signature    TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    etype        TEXT NOT NULL,
    message      TEXT NOT NULL,
    location     TEXT NOT NULL DEFAULT '',
    traceback    TEXT NOT NULL DEFAULT '',
    context      TEXT NOT NULL DEFAULT '',
    host         TEXT NOT NULL DEFAULT '',
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    count        INTEGER NOT NULL DEFAULT 1,
    resolved_at  TEXT,
    resolved_by  TEXT,
    last_error_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_err_last ON errors(last_seen);
"""
_ready: set = set()


def _db():
    from core import store
    c = store.conn()
    if store.DB_PATH not in _ready:
        c.executescript(_DDL)
        _ready.add(store.DB_PATH)
    return c


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------- signature

_NOISE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0x?"),
    (re.compile(r"\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b"), "<ip>"),
    (re.compile(r"'[^']{0,200}'"), "'?'"),
    (re.compile(r'"[^"]{0,200}"'), '"?"'),
    (re.compile(r"\d+"), "#"),
]


_sysnames = {"at": 0.0, "re": None}


def _system_pattern():
    """System names from systems.yaml, so 'X failed on PS4' and 'X failed on PRD' are one group."""
    now = time.monotonic()
    if now - _sysnames["at"] > 300:
        _sysnames["at"] = now
        try:
            from core.config_loader import get_all_systems
            names = sorted({str(c.get("name")) for c in get_all_systems() if c.get("name")}, key=len, reverse=True)
            _sysnames["re"] = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b") if names else None
        except Exception:
            pass
    return _sysnames["re"]


def _normalise(text: str) -> str:
    text = str(text or "")[:500]
    pat = _system_pattern()
    if pat is not None:
        text = pat.sub("<system>", text)
    for pattern, repl in _NOISE:
        text = pattern.sub(repl, text)
    return " ".join(text.split())


def _project_frames(tb) -> list[str]:
    out = []
    for fr in traceback.extract_tb(tb) if tb else []:
        path = os.path.abspath(fr.filename)
        if path.startswith(_PROJECT) and "site-packages" not in path:
            rel = os.path.relpath(path, _PROJECT).replace("\\", "/")
            out.append(f"{rel}:{fr.name}")
    return out


def signature_for(etype: str, message: str, frames: list[str], source: str) -> str:
    key = f"{source if source == 'browser' else ''}|{etype}|{'>'.join(frames[-3:])}|{_normalise(message)[:160]}"
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------- capture

def _too_fast(sig: str, per_minute: int = 30) -> bool:
    """More than `per_minute` writes of one group a minute are counted, not stored."""
    now = time.monotonic()
    with _rate_lock:
        hits = [t for t in _rate.get(sig, []) if now - t < 60]
        hits.append(now)
        _rate[sig] = hits
        return len(hits) > per_minute


def _redact(text: str) -> str:
    try:
        from utils.logger import _redact as r
        return r(text)
    except Exception:
        return text


def capture(exc: BaseException | None = None, source: str = "thread", context: str = "",
            etype: str | None = None, message: str | None = None, tb_text: str | None = None,
            frames: list[str] | None = None, notify: bool | None = None) -> str | None:
    """
    Record one error. Returns its error id ("<signature>-<short time>") for
    showing to a user, or None when nothing was recorded. Never raises.
    """
    if getattr(_guard, "active", False):
        return None
    if exc is not None:
        # One exception is counted once, even when it is captured explicitly
        # and then logged with exc_info (or re-raised into a hook).
        seen = getattr(exc, "__ibops_error_id__", None)
        if seen:
            return seen
    _guard.active = True
    try:
        if exc is not None:
            etype = etype or type(exc).__name__
            message = message if message is not None else str(exc)
            tb = exc.__traceback__
            frames = frames if frames is not None else _project_frames(tb)
            if tb_text is None:
                tb_text = "".join(traceback.format_exception(type(exc), exc, tb))
        etype = etype or "Error"
        message = _redact(str(message or ""))[:2000]
        tb_text = _redact(str(tb_text or ""))[-12000:]
        frames = frames or []
        source = source if source in SOURCES else "thread"
        sig = signature_for(etype, message, frames, source)
        err_id = f"{sig}-{datetime.now():%H%M%S}"
        if exc is not None:
            try:
                exc.__ibops_error_id__ = err_id
            except Exception:
                pass
        if _too_fast(sig):
            return err_id
        location = frames[-1] if frames else ""
        now = _now()
        c = _db()
        row = c.execute("SELECT count, resolved_at FROM errors WHERE signature=?", (sig,)).fetchone()
        is_new = row is None
        came_back = row is not None and row["resolved_at"] is not None
        if is_new:
            c.execute("INSERT INTO errors(signature, source, etype, message, location, traceback, context, host, "
                      "first_seen, last_seen, count, last_error_id) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
                      (sig, source, etype[:120], message, location[:300], tb_text, str(context or "")[:1000],
                       socket.gethostname(), now, now, err_id))
        else:
            c.execute("UPDATE errors SET last_seen=?, count=count+1, message=?, traceback=?, context=?, host=?, "
                      "resolved_at=NULL, resolved_by=NULL, last_error_id=? WHERE signature=?",
                      (now, message, tb_text, str(context or "")[:1000], socket.gethostname(), err_id, sig))
        if is_new or came_back:
            _announce(sig, source, etype, message, location, tb_text, context, came_back, notify)
        return err_id
    except Exception:
        return None
    finally:
        _guard.active = False


def _mail_sources() -> set[str]:
    raw = os.getenv("IBO_ERROR_MAIL_SOURCES", "web,thread,process,loop,sweep,log,job")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _announce(sig, source, etype, message, location, tb_text, context, came_back, notify) -> None:
    try:
        from core import timeline
        timeline.add("IBOPS", "error", f"{'Error back' if came_back else 'New error'} ({source}): "
                     f"{etype}: {message[:160]}", f"{location} {context}".strip(), severity="warning",
                     ref=f"error:{sig}")
    except Exception:
        pass
    if notify is False or (notify is None and source not in _mail_sources()):
        return
    if os.getenv("IBO_ERROR_MAIL", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        # Mail on its own thread: capture() may be running inside a request
        # or a sweep, which must not wait for SMTP.
        threading.Thread(target=_mail, args=(sig, source, etype, message, location, tb_text, context,
                                             came_back), daemon=True, name=f"error-mail-{sig}").start()
    except Exception:
        pass


def _mail(sig, source, etype, message, location, tb_text, context, came_back) -> None:
    _guard.active = True       # failures while mailing are never captured again
    try:
        import html
        from core import recipients
        from core.config_loader import get_smtp_config
        from notifications.mailer import send_html
        to = recipients.basis(None)
        if not to:
            return
        try:
            hours = float(os.getenv("IBO_ERROR_MAIL_HOURS", "6") or 6)
        except ValueError:
            hours = 6.0
        try:
            cap = int(os.getenv("IBO_ERROR_MAIL_DAILY_CAP", "20") or 20)
        except ValueError:
            cap = 20
        from core.action_links import public_url
        link = f'<p><a href="{public_url()}/errors?sig={sig}">Open on the Errors page</a></p>' if public_url() else ""
        body = (f"<p><b>{'An error that was marked resolved is back' if came_back else 'A new kind of error'} "
                f"on {html.escape(socket.gethostname())}</b> ({html.escape(source)}).</p>"
                f"<p><code>{html.escape(etype)}: {html.escape(message[:500])}</code></p>"
                f"<p>Where: {html.escape(location or '?')}<br>Context: {html.escape(str(context or '-'))[:300]}</p>"
                f"{link}<details><summary>Traceback</summary><pre style='font-size:11px'>"
                f"{html.escape(tb_text[-6000:])}</pre></details>"
                f"<p style='color:#64748b'>One mail per error kind every {hours:g} h; further occurrences are "
                f"counted on the Errors page.</p>")
        send_html(subject=f"[IBOPS ERROR] {etype}: {message[:80]}", html=body, to=to,
                  smtp_config=get_smtp_config(None, require_recipients=False), system="IBOPS",
                  kind="exception", key=sig, min_interval_s=hours * 3600, daily_cap=cap, severity="WARNING")
    except Exception:
        pass
    finally:
        _guard.active = False


# ------------------------------------------------------------------ hooks

_BENIGN_NET = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
_BENIGN_TEXT = ("winerror 10054", "winerror 10053", "connection reset by peer", "broken pipe",
                "an existing connection was forcibly closed", "sslv3_alert_certificate_unknown",
                "tlsv1_alert_unknown_ca", "http_request", "eof occurred in violation of protocol")


def benign_network(record: logging.LogRecord) -> bool:
    """
    A browser that closed its connection (tab closed, page reloaded, PC went
    to sleep, a browser refusing the self-signed certificate). Windows'
    asyncio logs each one as an ERROR with a ConnectionResetError traceback
    (WinError 10054) -- 835 of them in a day. Not a fault of IBOPS.
    """
    exc = record.exc_info[1] if record.exc_info else None
    if exc is not None and isinstance(exc, _BENIGN_NET):
        return True
    if exc is not None and type(exc).__name__ in ("SSLError", "SSLEOFError") and \
            any(t in str(exc).lower() for t in _BENIGN_TEXT):
        return True
    if record.name.startswith(("asyncio", "uvicorn")):
        try:
            text = record.getMessage().lower()
        except Exception:  # noqa: BLE001
            return False
        return any(t in text for t in _BENIGN_TEXT)
    return False


class _BenignNetFilter(logging.Filter):
    """Drops those records from the asyncio / uvicorn loggers (log file and Errors page)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not benign_network(record)


class _ErrorLogHandler(logging.Handler):
    """Every ERROR+ log record becomes a captured error (source 'log')."""

    # Loggers whose ERROR lines are already captured with full detail elsewhere
    # (supervisor -> source "loop"), or would recurse (mailer, this module).
    _SKIP = ("core.errors", "core.supervisor", "notifications.mailer", "uvicorn.access")

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_guard, "active", False) or record.name.startswith(self._SKIP):
            return
        if benign_network(record):
            return
        try:
            if record.exc_info and record.exc_info[1] is not None:
                capture(record.exc_info[1], "log", context=f"{record.name}: {record.getMessage()[:300]}")
                return
            capture(None, "log", context=record.name, etype=f"{record.levelname} {record.name}",
                    message=record.getMessage(), tb_text="",
                    frames=[f"{os.path.relpath(record.pathname, _PROJECT).replace(os.sep, '/')}:{record.funcName}"])
        except Exception:
            pass


def install() -> None:
    """Hook sys / threading / logging. Idempotent; safe to call from every entry point."""
    global _installed
    if _installed:
        return
    _installed = True

    prev_sys = sys.excepthook

    def _sys_hook(t, v, tb):
        try:
            if not issubclass(t, (KeyboardInterrupt, SystemExit)):
                capture(v, "process")
        finally:
            prev_sys(t, v, tb)
    sys.excepthook = _sys_hook

    prev_thread = threading.excepthook

    def _thread_hook(args):
        try:
            if args.exc_type not in (SystemExit,) and args.exc_value is not None:
                name = getattr(args.thread, "name", "?")
                capture(args.exc_value, "thread", context=f"thread {name}")
        finally:
            prev_thread(args)
    threading.excepthook = _thread_hook

    for name in ("asyncio", "uvicorn.error", "uvicorn"):
        logging.getLogger(name).addFilter(_BenignNetFilter())

    if os.getenv("IBO_ERROR_FROM_LOGS", "1").strip().lower() not in ("0", "false", "no", "off"):
        h = _ErrorLogHandler(level=logging.ERROR)
        logging.getLogger().addHandler(h)
        # Project loggers may not propagate to root; attach to the existing ones too.
        for name, lg in list(logging.Logger.manager.loggerDict.items()):
            if isinstance(lg, logging.Logger) and not lg.propagate:
                lg.addHandler(h)
        _patch_get_logger(h)


def _patch_get_logger(handler) -> None:
    """Loggers made by utils.logger.get_logger from now on get the handler too."""
    try:
        from utils import logger as ulog
        if handler not in ulog.EXTRA_HANDLERS:
            ulog.EXTRA_HANDLERS.append(handler)
    except Exception:
        pass


# ----------------------------------------------------------------- queries

def listing(include_resolved: bool = False, hours: float = 168, limit: int = 300) -> list[dict]:
    cutoff = datetime.fromtimestamp(time.time() - hours * 3600).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT signature, source, etype, message, location, context, host, first_seen, last_seen, count, " \
          "resolved_at, resolved_by FROM errors WHERE last_seen>=?"
    if not include_resolved:
        sql += " AND resolved_at IS NULL"
    sql += " ORDER BY last_seen DESC LIMIT ?"
    return [dict(r) for r in _db().execute(sql, (cutoff, int(limit))).fetchall()]


def get(sig: str) -> dict | None:
    r = _db().execute("SELECT * FROM errors WHERE signature=?", (sig,)).fetchone()
    return dict(r) if r else None


def resolve(sig: str, by: str = "") -> bool:
    return bool(_db().execute("UPDATE errors SET resolved_at=?, resolved_by=? WHERE signature=? AND "
                              "resolved_at IS NULL", (_now(), by or "?", sig)).rowcount)


def summary(hours: float = 24) -> dict:
    cutoff = datetime.fromtimestamp(time.time() - hours * 3600).strftime("%Y-%m-%d %H:%M:%S")
    r = _db().execute("SELECT COUNT(*) AS groups, COALESCE(SUM(count),0) AS total FROM errors "
                      "WHERE resolved_at IS NULL AND last_seen>=?", (cutoff,)).fetchone()
    return {"open_groups": r["groups"], "occurrences": r["total"], "hours": hours}


def prune(days: int = 30) -> int:
    cutoff = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d %H:%M:%S")
    return _db().execute("DELETE FROM errors WHERE last_seen<?", (cutoff,)).rowcount


def reset_for_tests() -> None:
    _ready.clear()
    _sysnames.update(at=0.0, re=None)
    with _rate_lock:
        _rate.clear()
