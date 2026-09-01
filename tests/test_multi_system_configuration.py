from core.config_loader import (
    get_systems,
    validate_systems_configuration,
    configuration_summary,
)


def set_env(monkeypatch):
    values = {
        "TST_SAP_USERNAME": "u1",
        "TST_SAP_PASSWORD": "p1",
        "TST_SSH_HOST": "host1",
        "TST_SSH_PORT": "22",
        "TST_SSH_USERNAME": "ssh1",
        "TST_SSH_PASSWORD": "sp1",
        "QAS_SAP_USERNAME": "u2",
        "QAS_SAP_PASSWORD": "p2",
        "QAS_SAP_CONNECTION_NAME": "Centor QAS system",
    }

    for key, value in values.items():
        monkeypatch.setenv(key, value)


def isolate_two_system_config(monkeypatch, tmp_path):
    """Make these tests independent of the real systems.yaml."""

    config_dir = tmp_path / "config"
    config_dir.mkdir()

    systems_file = config_dir / "systems.yaml"

    systems_file.write_text(
        """
systems:
  - name: TST
    client: "000"
    connection_name: NW_Test_System
    username: ${TST_SAP_USERNAME}
    password: ${TST_SAP_PASSWORD}
    language: EN
    has_gui_access: true
    has_os_access: true
    ssh_host: ${TST_SSH_HOST}
    ssh_port: ${TST_SSH_PORT}
    ssh_username: ${TST_SSH_USERNAME}
    ssh_password: ${TST_SSH_PASSWORD}
    sap_instance_nr: "02"

  - name: CENTOR_QAS
    client: "100"
    connection_name: ${QAS_SAP_CONNECTION_NAME}
    username: ${QAS_SAP_USERNAME}
    password: ${QAS_SAP_PASSWORD}
    language: EN
    has_gui_access: true
    has_os_access: false
""",
        encoding="utf-8",
    )

    import core.config_loader as config_loader

    monkeypatch.setattr(
        config_loader,
        "CONFIG_DIR",
        str(config_dir),
    )

    return systems_file


def test_system_placeholders_resolve(monkeypatch, tmp_path):
    set_env(monkeypatch)
    isolate_two_system_config(monkeypatch, tmp_path)

    systems = get_systems()

    assert len(systems) == 2

    tst = next(
        x for x in systems
        if x["name"] == "TST"
    )

    qas = next(
        x for x in systems
        if x["name"] == "CENTOR_QAS"
    )

    assert tst["username"] == "u1"
    assert tst["password"] == "p1"
    assert tst["ssh_password"] == "sp1"

    assert qas["username"] == "u2"
    assert qas["password"] == "p2"


def test_multi_system_configuration_is_valid(monkeypatch, tmp_path):
    set_env(monkeypatch)
    isolate_two_system_config(monkeypatch, tmp_path)

    result = validate_systems_configuration()

    assert result == {
        "valid": True,
        "systems_configured": 2,
        "systems_valid": 2,
    }


def test_safe_summary_has_no_secret_names(monkeypatch, tmp_path):
    set_env(monkeypatch)
    isolate_two_system_config(monkeypatch, tmp_path)

    result = configuration_summary()

    text = str(result).lower()

    assert "password" not in text
    assert "api_key" not in text
    assert result["systems_configured"] == 2


def test_missing_secret_degrades_only_that_system(
    monkeypatch,
    tmp_path,
):
    set_env(monkeypatch)
    isolate_two_system_config(monkeypatch, tmp_path)

    monkeypatch.delenv(
        "TST_SAP_PASSWORD",
        raising=False,
    )

    result = validate_systems_configuration()

    assert result["systems_configured"] == 2
    assert result["systems_valid"] == 1
    assert result["valid"] is False