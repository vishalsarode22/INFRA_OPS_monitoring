from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = Path(__file__).with_name("evidence_index_8_11_patch.py")

if not (ROOT / "reporting" / "evidence.py").exists():
    raise SystemExit("Run from the SAP_BASIS_MONITOR project root.")

exec(compile(PATCH.read_text(encoding="utf-8"), str(PATCH), "exec"))
print("Milestone 8.11 applied.")
