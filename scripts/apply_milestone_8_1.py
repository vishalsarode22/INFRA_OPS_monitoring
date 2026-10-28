
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "core" / "config_loader.py"
LOGGER = ROOT / "utils" / "logger.py"
HEALTH = ROOT / "core" / "production_health.py"
ENV_EXAMPLE = ROOT / ".env.example"
REQ = ROOT / "requirements.txt"

def read(p): return p.read_text(encoding="utf-8")
def write(p, s): p.write_text(s, encoding="utf-8")

s = read(CONFIG)
if "def validate_configuration(" not in s:
    addition = r'''
# Milestone 8.1: non-secret configuration validation.
def validate_configuration(include_optional: bool = False) -> dict:
    """Return configuration health without exposing secret values."""
    required = ["SAP_CLIENT", "SAP_USERNAME", "SAP_PASSWORD"]
    if include_optional:
        required += ["SSH_HOST", "SSH_USERNAME", "SSH_PASSWORD"]
    missing = [name for name in required if not os.getenv(name)]
    provider = (os.getenv("AI_PROVIDER") or "mock").strip().lower()
    invalid = {}
    if provider not in {"mock", "gemini"}:
        invalid["AI_PROVIDER"] = "unsupported"
    for name in ("SMTP_PORT", "SSH_PORT"):
        value = os.getenv(name)
        if value:
            try:
                port = int(value)
                if not 1 <= port <= 65535:
                    invalid[name] = "out_of_range"
            except ValueError:
                invalid[name] = "not_integer"
    return {
        "valid": not missing and not invalid,
        "missing": missing,
        "invalid": invalid,
        "ai_provider": {
            "provider": provider,
            "configured": provider != "gemini" or bool(os.getenv("GEMINI_API_KEY")),
        },
    }

def configuration_summary() -> dict:
    """Safe configuration summary suitable for diagnostics/API responses."""
    result = validate_configuration()
    return {
        "valid": result["valid"],
        "missing": list(result["missing"]),
        "invalid": dict(result["invalid"]),
        "ai_provider": dict(result["ai_provider"]),
    }
'''
    s += addition
    write(CONFIG, s)

s = read(LOGGER)
if "class SecretRedactionFilter" not in s:
    s = s.replace("import os\n", "import os\nimport re\n", 1)
    addition = r'''
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

'''
    s = s.replace("_configured_loggers = {}\n", addition + "_configured_loggers = {}\n", 1)
    s = s.replace("        fh.setLevel(logging.DEBUG)\n", "        fh.setLevel(logging.DEBUG)\n        fh.addFilter(SecretRedactionFilter())\n", 1)
    s = s.replace("        ch.setLevel(logging.INFO)\n", "        ch.setLevel(logging.INFO)\n        ch.addFilter(SecretRedactionFilter())\n", 1)
    s = s.replace("        eh.setLevel(logging.ERROR)\n", "        eh.setLevel(logging.ERROR)\n        eh.addFilter(SecretRedactionFilter())\n", 1)
    write(LOGGER, s)

s = read(HEALTH)
if "from core.config_loader import configuration_summary" not in s:
    s = s.replace("from utils.paths import BASE_DIR\n",
                  "from utils.paths import BASE_DIR\nfrom core.config_loader import configuration_summary\n", 1)
if '"configuration": configuration_summary()' not in s:
    s = s.replace('        "snapshot_storage": _path_check(snapshot_dir),\n',
                  '        "snapshot_storage": _path_check(snapshot_dir),\n        "configuration": configuration_summary(),\n', 1)
write(HEALTH, s)

env = read(ENV_EXAMPLE)
if "NEVER commit .env" not in env:
    write(ENV_EXAMPLE, "# NEVER commit .env or real credentials to source control.\n" + env)

if not REQ.exists():
    REQ.write_text(
        "fastapi\nstarlette\nuvicorn\npydantic\nrequests\nPyYAML\npython-dotenv\n"
        "pandas\nopenpyxl\nreportlab\nPillow\npytest\nhttpx2\n",
        encoding="utf-8",
    )
else:
    req = read(REQ)
    if "httpx2" not in req.lower():
        write(REQ, req.rstrip() + "\nhttpx2\n")

print("Milestone 8.1 applied safely.")
