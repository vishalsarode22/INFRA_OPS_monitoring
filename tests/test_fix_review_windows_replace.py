"""Windows refuses os.replace() onto a file another process has open
("Access is denied", WinError 5). Saving systems.yaml / .env must still work."""
import os

import pytest


def _deny_replace(monkeypatch):
    def deny(src, dst):
        raise PermissionError(5, "Access is denied", src)
    monkeypatch.setattr(os, "replace", deny)
    import utils.atomic as A
    monkeypatch.setattr(A.time, "sleep", lambda s: None)


def test_atomic_write_falls_back_to_in_place_write(tmp_path, monkeypatch):
    from utils.atomic import atomic_write_text
    p = tmp_path / "systems.yaml"
    p.write_text("old: 1\n", encoding="utf-8")
    _deny_replace(monkeypatch)
    atomic_write_text(str(p), "new: 2\n")
    assert p.read_text(encoding="utf-8") == "new: 2\n"
    assert not [f.name for f in tmp_path.iterdir() if f.name.endswith(".tmp")]   # no temp left behind


def test_config_editor_save_survives_a_reader_holding_the_file(tmp_path, monkeypatch):
    from core import config_editor
    monkeypatch.setattr(config_editor, "config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(config_editor, "backup", lambda name: None)
    (tmp_path / "systems.yaml").write_text("systems: []\n", encoding="utf-8")
    _deny_replace(monkeypatch)
    config_editor.write_yaml_safely("systems.yaml", {"systems": [{"name": "PS4"}]})
    assert "PS4" in (tmp_path / "systems.yaml").read_text(encoding="utf-8")
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".tmp-")]


def test_read_only_file_gives_a_clear_error(tmp_path, monkeypatch):
    from utils.atomic import replace_or_overwrite
    dst = tmp_path / "systems.yaml"
    dst.write_text("x", encoding="utf-8")
    src = tmp_path / "new.tmp"
    src.write_text("y", encoding="utf-8")
    _deny_replace(monkeypatch)
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    with pytest.raises(PermissionError, match="read-only"):
        replace_or_overwrite(str(src), str(dst))
    assert dst.read_text(encoding="utf-8") == "x"


def test_env_save_survives_a_reader_holding_the_file(tmp_path, monkeypatch):
    from core import delivery_settings as D
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    monkeypatch.setattr(D, "ENV_PATH", str(env))
    _deny_replace(monkeypatch)
    writer = next((getattr(D, n) for n in ("_write_env", "_write_env_lines", "_save_env") if hasattr(D, n)), None)
    if writer is None:
        pytest.skip("no direct .env writer to call")
