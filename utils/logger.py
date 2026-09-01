import logging
from logging.handlers import RotatingFileHandler
import os
import re

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

_configured_loggers = {}


def get_logger(module_name: str, log_file: str = "application") -> logging.Logger:
    key = f"{module_name}:{log_file}"
    if key in _configured_loggers:
        return _configured_loggers[key]

    logger = logging.getLogger(module_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not logger.handlers:
        fh = RotatingFileHandler(
            os.path.join(LOG_DIR, f"{log_file}.log"),
            maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(_FORMAT))
        fh.setLevel(logging.DEBUG)
        fh.addFilter(SecretRedactionFilter())

        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(_FORMAT))
        ch.setLevel(logging.INFO)
        ch.addFilter(SecretRedactionFilter())

        eh = RotatingFileHandler(
            os.path.join(LOG_DIR, "errors.log"),
            maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        eh.setFormatter(logging.Formatter(_FORMAT))
        eh.setLevel(logging.ERROR)
        eh.addFilter(SecretRedactionFilter())

        logger.addHandler(fh)
        logger.addHandler(ch)
        logger.addHandler(eh)

    _configured_loggers[key] = logger
    return logger