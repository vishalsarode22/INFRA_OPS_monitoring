from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "core" / "config_loader.py"
s = CFG.read_text(encoding="utf-8")

helper = (
"import re\n\n"
"_ENV_PATTERN = re.compile(r'\\$\\{([A-Za-z_][A-Za-z0-9_]*)\\}')\n\n"
"def _resolve_env_value(value):\n"
"    if not isinstance(value, str):\n"
"        return value\n"
"    return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), m.group(0)), value)\n\n"
"def _resolve_system(system):\n"
"    return {key: _resolve_env_value(value) for key, value in system.items()}\n\n"
)
if "def _resolve_env_value(" not in s:
    s=s.replace("import os\n","import os\n"+helper,1)

old = """def get_systems() -> list[dict]:
    path = os.path.join(CONFIG_DIR, "systems.yaml")
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    return data.get("systems", [])
"""
new = """def get_systems() -> list[dict]:
    path = os.path.join(CONFIG_DIR, "systems.yaml")
    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}
    return [_resolve_system(system) for system in data.get("systems", [])]
"""
if old in s:
    s=s.replace(old,new,1)

marker="# Milestone 8.1: non-secret configuration validation."
start=s.find(marker)
if start<0:
    raise RuntimeError("Milestone 8.1 validation block not found")

validation = """# Milestone 8.1/8.5A: non-secret configuration validation.

def validate_systems_configuration() -> dict:
    try:
        systems = get_systems()
    except Exception:
        return {"valid": False, "systems_configured": 0, "systems_valid": 0}

    valid_count = 0
    for system in systems:
        required = ("name", "client", "username", "password", "connection_name")
        if not all(system.get(key) for key in required):
            continue
        if system.get("has_os_access"):
            if not all(system.get(key) for key in
                       ("ssh_host", "ssh_username", "ssh_password")):
                continue
        valid_count += 1

    return {
        "valid": bool(systems) and valid_count == len(systems),
        "systems_configured": len(systems),
        "systems_valid": valid_count,
    }

def validate_configuration(include_optional: bool = False) -> dict:
    systems = validate_systems_configuration()
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

    if systems["systems_configured"] == 0:
        invalid["systems_configuration"] = "no_valid_systems"

    return {
        "valid": systems["valid"] and not invalid,
        "systems_configured": systems["systems_configured"],
        "systems_valid": systems["systems_valid"],
        "invalid": invalid,
        "ai_provider": {
            "provider": provider,
            "configured": provider != "gemini" or bool(os.getenv("GEMINI_API_KEY")),
        },
    }

def configuration_summary() -> dict:
    result = validate_configuration()
    return {
        "valid": result["valid"],
        "systems_configured": result["systems_configured"],
        "systems_valid": result["systems_valid"],
        "invalid": dict(result["invalid"]),
        "ai_provider": dict(result["ai_provider"]),
    }
"""
s=s[:start]+validation+"\n"
CFG.write_text(s,encoding="utf-8")
print("Milestone 8.5A applied safely.")
