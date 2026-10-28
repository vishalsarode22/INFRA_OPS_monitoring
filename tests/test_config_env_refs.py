"""
add_system() must write ${VAR} references for every connection detail, not
only passwords, so config/systems.yaml can be committed and screen-shared.
"""

import os

import yaml

import core.config_loader as cl


def _setup(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "systems.yaml").write_text("systems: []\n", encoding="utf-8")
    monkeypatch.setattr(cl, "CONFIG_DIR", str(cfg))
    return cfg / "systems.yaml", tmp_path / ".env"


def _add(**over):
    kw = dict(
        name="Demo QAS", client="100", connection_name="Demo QAS",
        username="gui_user", password="gui-pw",
        has_gui_access=True, has_os_access=True,
        ssh_host="10.9.9.9", ssh_username="sshuser", ssh_password="ssh-pw",
        rfc_ashost="10.9.9.10", rfc_sysnr="00", rfc_username="rfc_user",
        rfc_password="rfc-pw", rfc_saprouter="/H/1.2.3.4/W/routerpw/H/",
        smtp_host="smtp.example.com", smtp_port="587",
        smtp_username="alerts@example.com", smtp_password="smtp-pw",
        sender_email="alerts@example.com",
        alert_receivers="basis@example.com", alert_receivers_cc="mgr@example.com",
    )
    kw.update(over)
    cl.add_system(**kw)


def test_no_connection_detail_is_written_inline(tmp_path, monkeypatch):
    yaml_path, env_path = _setup(tmp_path, monkeypatch)
    _add()

    text = yaml_path.read_text(encoding="utf-8")
    for secret in ("gui_user", "10.9.9.9", "sshuser", "10.9.9.10", "rfc_user",
                   "routerpw", "smtp.example.com", "alerts@example.com",
                   "basis@example.com", "mgr@example.com", "-pw"):
        assert secret not in text, f"{secret!r} leaked into systems.yaml"

    sysd = yaml.safe_load(text)["systems"][0]
    assert sysd["rfc"]["ashost"] == "${DEMO_QAS_RFC_HOST}"
    assert sysd["rfc"]["saprouter"] == "${DEMO_QAS_SAPROUTER}"
    assert sysd["alert_receivers"] == "${DEMO_QAS_ALERT_RECEIVERS}"
    # Structure that is not sensitive stays readable.
    assert sysd["client"] == "100"
    assert sysd["rfc"]["sysnr"] == "00"

    env = env_path.read_text(encoding="utf-8")
    assert "DEMO_QAS_RFC_HOST=10.9.9.10" in env
    assert "DEMO_QAS_SAPROUTER=/H/1.2.3.4/W/routerpw/H/" in env


def test_values_resolve_immediately_without_restart(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    for k in list(os.environ):
        if k.startswith("DEMO_QAS_"):
            monkeypatch.delenv(k)
    _add()
    assert os.environ["DEMO_QAS_RFC_PASSWORD"] == "rfc-pw"
    assert os.environ["DEMO_QAS_RFC_HOST"] == "10.9.9.10"


def test_existing_reference_is_kept_on_edit(tmp_path, monkeypatch):
    yaml_path, env_path = _setup(tmp_path, monkeypatch)
    _add()
    before = env_path.read_text(encoding="utf-8")
    # The edit form may send the reference back unchanged.
    _add(rfc_ashost="${DEMO_QAS_RFC_HOST}", rfc_password="")
    sysd = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["systems"][0]
    assert sysd["rfc"]["ashost"] == "${DEMO_QAS_RFC_HOST}"
    assert "${DEMO_QAS_RFC_HOST}" not in env_path.read_text(encoding="utf-8")
    assert env_path.read_text(encoding="utf-8").count("DEMO_QAS_RFC_HOST=") == 1
    assert before.count("DEMO_QAS_RFC_HOST=") == 1
