import logging
from logging.handlers import RotatingFileHandler
import os
import re
import threading
import time

from utils.paths import BASE_DIR

LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_MAX_BYTES = 5 * 1024 * 1024  # 5MB per file
_BACKUP_COUNT = 5              # keep 5 rotated files (25MB total per log type)


_SECRET_PATTERNS = (
    re.compile(r'(?i)(gemini[_-]?api[_-]?key)\s*[:=]\s*([^\s,;]+)'),
    re.compile(r'(?i)(ssh[_-]?password)\s*[:=]\s*([^\s,;]+)'),
    re.compile(r'(?i)(smtp[_-]?password)\s*[:=]\s*([^\s,;]+)'),
    re.compile(r'(?i)(password)\s*[:=]\s*([^\s,;]+)'),
    re.compile(r'(?i)(authorization)\s*:\s*(bearer\s+)?([^\s,;]+)'),
)

def _redact(value):
    if not isinstance(value, str):
        return value
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", result)
    return result

class SecretRedactionFilter(logging.Filter):
    """Redact common credential-bearing fields before handlers receive them."""
    def filter(self, record):
        try:
            record.msg = _redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _redact(v) for k, v in record.args.items()}
                else:
                    record.args = tuple(_redact(v) for v in record.args)
        except Exception:
            pass
        return True

class _SafeRotatingFileHandler(RotatingFileHandler):
    """
    Round 35: a RotatingFileHandler that works on Windows with several
    processes writing the same file.

    The plain handler kept the file open for the life of the process, and
    every module opened its OWN handler on application.log. Once the file
    passed 5 MB, rotating it (a rename) failed with WinError 32 because other
    handles were open -- and a failed rotation DROPS the record and prints a
    traceback instead. From then on nearly every log line was lost.

    This handler:
      * opens the file only to write a record and closes it straight after,
        so another process can rename it between writes;
      * treats a failed rotation as "try again in a minute" and still writes
        the record to the current file -- a log line is never dropped because
        housekeeping failed.
    """

    RETRY_SECONDS = 60

    def __init__(self, filename, **kwargs):
        kwargs["delay"] = True
        super().__init__(filename, **kwargs)
        self._retry_after = 0.0

    def _rollover_due(self) -> bool:
        if self.maxBytes <= 0 or time.monotonic() < self._retry_after:
            return False
        try:
            return os.path.getsize(self.baseFilename) >= self.maxBytes
        except OSError:
            return False

    def _close_stream(self) -> None:
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def emit(self, record):
        try:
            if self._rollover_due():
                self._close_stream()
                try:
                    self.doRollover()
                except OSError:
                    # Someone else has the file open right now. Keep writing
                    # to it; try the rotation again later.
                    self._retry_after = time.monotonic() + self.RETRY_SECONDS
                self._close_stream()
            logging.FileHandler.emit(self, record)
        except Exception:
            self.handleError(record)
        finally:
            self._close_stream()


# One handler per log file per process. Each module used to add its own
# handler on the same file, which by itself was enough to break rotation.
_file_handlers: dict = {}
_file_handlers_lock = threading.Lock()


def _file_handler(filename: str, level: int) -> logging.Handler:
    path = os.path.join(LOG_DIR, filename)
    with _file_handlers_lock:
        h = _file_handlers.get(path)
        if h is None:
            h = _SafeRotatingFileHandler(path, maxBytes=_MAX_BYTES,
                                         backupCount=_BACKUP_COUNT, encoding="utf-8")
            h.setFormatter(logging.Formatter(_FORMAT))
            h.setLevel(level)
            h.addFilter(SecretRedactionFilter())
            _file_handlers[path] = h
        return h


_configured_loggers = {}


def get_logger(module_name: str, log_file: str = "application") -> logging.Logger:
    key = f"{module_name}:{log_file}"
    if key in _configured_loggers:
        return _configured_loggers[key]

    logger = logging.getLogger(module_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not logger.handlers:
        fh = _file_handler(f"{log_file}.log", logging.DEBUG)

        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(_FORMAT))
        ch.setLevel(logging.INFO)
        ch.addFilter(SecretRedactionFilter())

        eh = _file_handler("errors.log", logging.ERROR)

        logger.addHandler(fh)
        logger.addHandler(ch)
        logger.addHandler(eh)

    _configured_loggers[key] = logger
    return logger