from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "core" / "config_loader.py"

if not CFG.exists():
    raise SystemExit(f"Cannot find {CFG}")

backup = CFG.with_suffix(".py.pre_8_5A_repair.bak")
if not backup.exists():
    shutil.copy2(CFG, backup)

s = CFG.read_text(encoding="utf-8")

start = s.find("def _resolve_env_value(")
if start == -1:
    raise RuntimeError("Could not find _resolve_env_value in core/config_loader.py")

end = s.find("\ndef _resolve_system(", start)
if end == -1:
    raise RuntimeError("Could not find _resolve_system after _resolve_env_value")

new_function = (
    'def _resolve_env_value(value):\n'
    '    """Resolve ${ENV_VAR} references; missing variables become None."""\n'
    '    if not isinstance(value, str):\n'
    '        return value\n\n'
    '    full = _ENV_PATTERN.fullmatch(value)\n'
    '    if full:\n'
    '        return os.getenv(full.group(1))\n\n'
    '    return _ENV_PATTERN.sub(\n'
    '        lambda match: os.getenv(match.group(1), match.group(0)),\n'
    '        value,\n'
    '    )\n'
)

s = s[:start] + new_function + s[end:]
CFG.write_text(s, encoding="utf-8")
print("Milestone 8.5A resolver repair applied safely.")
