"""Production health/readiness checks for InfraBeatOps.

These checks intentionally avoid validating or returning secret values. Liveness
means the web process is alive; readiness means required local configuration and
persistence paths are usable. External SAP/SMTP/Gemini connectivity is not a
readiness prerequisite because the application is designed to degrade safely.
"""
from __future__ import annotations

import os
from pathlib import Path

from utils.paths import BASE_DIR
from core.config_loader import configuration_summary


def _path_check(path: Path) -> dict:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def liveness() -> dict:
    return {"status": "ok", "service": "InfraBeatOps"}


def readiness() -> dict:
    root = Path(BASE_DIR)
    config_dir = root / "config"
    snapshot_dir = root / "dashboard" / "snapshots"

    checks = {
        "config_directory": {"ok": config_dir.is_dir()},
        "thresholds_config": {"ok": (config_dir / "thresholds.yaml").is_file()},
        "monitoring_tasks_config": {"ok": (config_dir / "monitoring_tasks.yaml").is_file()},
        "systems_config": {"ok": (config_dir / "systems.yaml").is_file()},
        "snapshot_storage": _path_check(snapshot_dir),
        "configuration": configuration_summary(),
    }

    # Provider configuration is informational only. A missing API key must not
    # make the monitoring service unready because deterministic monitoring works
    # without an LLM provider.
    provider = (os.getenv("AI_PROVIDER") or "mock").strip().lower()
    checks["ai_provider"] = {
        "ok": True,
        "provider": provider,
        "configured": provider != "gemini" or bool(os.getenv("GEMINI_API_KEY")),
    }

    required_ok = all(item.get("ok", False) for item in checks.values())
    return {
        "status": "ready" if required_ok else "not_ready",
        "service": "InfraBeatOps",
        "checks": checks,
    }
