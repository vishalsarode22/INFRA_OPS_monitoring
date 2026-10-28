"""A password set on the Settings page reaches every process without a restart."""
import os
import time

from collectors import rfc_collector as C


def test_cooldown_ends_when_a_password_changes(tmp_path, monkeypatch):
    from core import config_loader
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    C.clear_cooldown()
    with C._lock:
        C._cooldowns["X"] = (time.monotonic() + 900, "no RFC configuration", time.time() - 5)
    assert C.cooldown_remaining("X")[0] > 0
    (tmp_path / ".secrets_changed").write_text("K\n")
    assert C.cooldown_remaining("X") == (0, None)


def test_missing_config_is_a_short_wait_not_an_auth_failure():
    assert C._cooldown_for("no RFC configuration (ashost, username or password missing)") == C._COOLDOWN_DEFAULT
    assert not C._is_auth_error("no RFC configuration (ashost, username or password missing)")


def test_systems_cache_follows_the_secret_marker(tmp_path, monkeypatch):
    from core import config_loader
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("IBO_SETTINGS_SYNC", "0")
    (tmp_path / "systems.yaml").write_text("systems:\n- name: S\n  rfc:\n    password: ${T_SECRET_PW}\n")
    monkeypatch.delenv("T_SECRET_PW", raising=False)
    config_loader.invalidate_systems_cache()
    first = config_loader.get_all_systems()[0]["rfc"]["password"]
    monkeypatch.setenv("T_SECRET_PW", "new")
    (tmp_path / ".secrets_changed").write_text("T_SECRET_PW\n")
    os.utime(tmp_path / ".secrets_changed", (time.time() + 5, time.time() + 5))
    assert config_loader.get_all_systems()[0]["rfc"]["password"] == "new" and first != "new"
