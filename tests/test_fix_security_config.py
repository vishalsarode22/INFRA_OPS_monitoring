"""
Security fixes in configuration handling:
  2  /api/systems masks every secret at any depth (smtp_password included)
  3  editing a connection target requires re-entering its password
  4  CR/LF/NUL cannot reach .env; keys and system names are validated
"""
import json
import os

import pytest
import yaml

FAKE = {
    "T1_SAP_PASSWORD": "gui-Secret-111",
    "T1_SSH_PASSWORD": "ssh-Secret-222",
    "T1_RFC_PASSWORD": "rfc-Secret-333",
    "T1_SMTP_PASSWORD": "smtp-Secret-444",
    "T1_ROUTER_PW": "router-Secret-555",
    "T1_API_KEY": "apikey-Secret-666",
    "T1_TOKEN": "token-Secret-777",
}


@pytest.fixture()
def cfgdir(tmp_path, monkeypatch):
    from core import config_loader as cl
    conf = tmp_path / "config"
    conf.mkdir()
    (tmp_path / ".env").write_text("".join(f"{k}={v}\n" for k, v in FAKE.items()), encoding="utf-8")
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    systems = {"systems": [{
        "name": "T1", "client": "100", "language": "EN",
        "has_gui_access": True, "connection_name": "T1 conn", "username": "monuser",
        "password": "${T1_SAP_PASSWORD}",
        "has_os_access": True, "ssh_host": "10.0.0.5", "ssh_port": 22, "ssh_username": "root",
        "ssh_password": "${T1_SSH_PASSWORD}",
        "rfc": {"ashost": "10.0.0.6", "sysnr": "00", "client": "100", "username": "RFCUSER",
                "password": "${T1_RFC_PASSWORD}",
                "saprouter": "/H/router.example/W/${T1_ROUTER_PW}/H/"},
        "smtp_host": "smtp.example", "smtp_username": "mailer",
        "smtp_password": "${T1_SMTP_PASSWORD}",
        "integrations": {"servicenow": {"api_key": "${T1_API_KEY}",
                                        "nested": [{"Bearer_Token": "${T1_TOKEN}"}]}},
    }]}
    (conf / "systems.yaml").write_text(yaml.safe_dump(systems, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(cl, "CONFIG_DIR", str(conf))
    cl.invalidate_systems_cache()
    yield cl, tmp_path
    cl.invalidate_systems_cache()


# ------------------------------------------------------------------ 2

def test_api_systems_response_contains_no_secret_value(cfgdir, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import dashboard.app as app_module
    monkeypatch.setattr(app_module, "get_systems", lambda: cfgdir[0].get_systems())
    r = TestClient(app_module.app).get("/api/systems")
    assert r.status_code == 200, r.text
    body = json.dumps(r.json())
    for secret in FAKE.values():
        assert secret not in body, f"{secret} leaked in /api/systems"
    item = r.json()[0]
    assert item["smtp_password"] == "\u2022" * 8
    assert item["integrations"]["servicenow"]["api_key"] == "\u2022" * 8
    assert "/W/\u2022" in item["rfc"]["saprouter"]
    assert item["rfc"]["ashost"] == "10.0.0.6"             # non-secrets intact


def test_mask_secrets_key_names_case_insensitive():
    from core.config_loader import mask_secrets
    out = mask_secrets({"SMTP_PASSWD": "a", "clientSecret": "b", "x": [{"ApiKey": "c"}],
                        "Credentials": {"u": "d"}, "plain": "keep", "route": "/H/h/W/pw/H/"})
    assert out["SMTP_PASSWD"] != "a" and out["clientSecret"] != "b"
    assert out["x"][0]["ApiKey"] != "c" and out["Credentials"] != {"u": "d"}
    assert out["plain"] == "keep" and "pw" not in out["route"]


# ------------------------------------------------------------------ 3

def _edit_kwargs(**over):
    base = dict(name="T1", client="100", connection_name="T1 conn", username="monuser",
                has_gui_access=True, has_os_access=True, ssh_host="10.0.0.5", ssh_port=22,
                ssh_username="root", rfc_ashost="10.0.0.6", rfc_sysnr="00", rfc_client="100",
                rfc_username="RFCUSER", rfc_saprouter="/H/router.example/W/router-Secret-555/H/",
                smtp_host="smtp.example", smtp_username="mailer")
    base.update(over)
    return base


def test_unchanged_edit_keeps_stored_passwords(cfgdir):
    cl, _ = cfgdir
    cl.add_system(**_edit_kwargs(alert_receivers="ops@example.com"))
    raw = yaml.safe_load(open(os.path.join(cl.CONFIG_DIR, "systems.yaml")))["systems"][0]
    assert raw["rfc"]["password"] == "${T1_RFC_PASSWORD}"


@pytest.mark.parametrize("change,label", [
    ({"rfc_ashost": "6.6.6.6"}, "RFC password"),
    ({"rfc_sysnr": "01"}, "RFC password"),
    ({"rfc_client": "200"}, "RFC password"),
    ({"rfc_username": "OTHER"}, "RFC password"),
    ({"rfc_saprouter": "/H/evil.example/H/"}, "RFC password"),
    ({"ssh_host": "6.6.6.6"}, "SSH password"),
    ({"connection_name": "Evil conn"}, "SAP GUI password"),
    ({"smtp_host": "smtp.evil.example"}, "SMTP password"),
])
def test_changed_target_without_password_is_rejected(cfgdir, change, label):
    cl, _ = cfgdir
    before = open(os.path.join(cl.CONFIG_DIR, "systems.yaml")).read()
    with pytest.raises(ValueError, match=label):
        cl.add_system(**_edit_kwargs(**change))
    assert open(os.path.join(cl.CONFIG_DIR, "systems.yaml")).read() == before


def test_changed_target_with_new_password_is_accepted(cfgdir):
    cl, _ = cfgdir
    cl.add_system(**_edit_kwargs(rfc_ashost="10.0.0.99", rfc_password="new-rfc-pw"))
    raw = yaml.safe_load(open(os.path.join(cl.CONFIG_DIR, "systems.yaml")))["systems"][0]
    assert raw["rfc"]["ashost"].startswith("${")


def test_api_edit_with_changed_host_and_blank_password_is_400(cfgdir, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import dashboard.app as app_module
    started = []
    monkeypatch.setattr(app_module, "_run_one_system_background", lambda *a, **k: started.append(a))
    c = TestClient(app_module.app)
    csrf = c.get("/api/me").json()["csrf_token"]
    body = _edit_kwargs(rfc_ashost="6.6.6.6")
    body["monitor_now"] = True
    r = c.post("/api/systems", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400 and "Re-enter" in r.json()["detail"]
    assert started == []


# ------------------------------------------------------------------ 4

@pytest.mark.parametrize("bad", ["x\nSAPLOGON_EXE_PATH=C:\\evil.exe", "x\rY=1", "x\x00"])
def test_crlf_in_add_system_value_is_rejected_and_env_untouched(cfgdir, bad):
    cl, tmp = cfgdir
    env_before = (tmp / ".env").read_text()
    with pytest.raises(ValueError):
        cl.add_system(name="NEW1", client="100", has_gui_access=False,
                      rfc_ashost="1.2.3.4", rfc_username="U", rfc_password=bad)
    assert (tmp / ".env").read_text() == env_before
    assert "SAPLOGON_EXE_PATH" not in (tmp / ".env").read_text()


@pytest.mark.parametrize("name", ["bad/name", "x" * 65, "a\nb", "", "../etc", "név"])
def test_system_name_is_restricted(cfgdir, name):
    cl, _ = cfgdir
    with pytest.raises(ValueError):
        cl.add_system(name=name, client="100", has_gui_access=False)


def test_env_key_and_value_validator():
    from core.env_safety import validate_env_key, quote_env_value, validate_system_name
    for bad in ("lower", "1ABC", "A-B", "A\nB", "A" * 65, ""):
        with pytest.raises(ValueError):
            validate_env_key(bad)
    assert validate_env_key("SMTP_PASSWORD__ACC_1") == "SMTP_PASSWORD__ACC_1"
    assert quote_env_value("plain.value-1") == "plain.value-1"
    assert quote_env_value("has space #x") == "'has space #x'"
    assert validate_system_name("CARFOUR QAS") == "CARFOUR QAS"


def test_dotenv_reads_quoted_values_back_exactly(tmp_path, monkeypatch):
    from dotenv import dotenv_values
    from core import delivery_settings
    env = tmp_path / ".env"
    env.write_text("KEEP=1\n", encoding="utf-8")
    monkeypatch.setattr(delivery_settings, "ENV_PATH", str(env))
    for tricky in ("p@ss w#rd $HOME \"q\"", "it's #1 \\ x", "a=b c"):
        delivery_settings._rewrite_env({"SMTP_PASSWORD": tricky})
        assert dotenv_values(str(env))["SMTP_PASSWORD"] == tricky
    assert dotenv_values(str(env))["KEEP"] == "1"
    with pytest.raises(ValueError):          # dotenv would expand it even when quoted
        delivery_settings._rewrite_env({"SMTP_PASSWORD": "x${KEEP}"})


def test_delivery_settings_and_accounts_reject_newlines(tmp_path, monkeypatch):
    from core import delivery_settings, delivery_accounts
    env = tmp_path / ".env"
    env.write_text("KEEP=1\n", encoding="utf-8")
    monkeypatch.setattr(delivery_settings, "ENV_PATH", str(env))
    monkeypatch.setattr(delivery_accounts, "ACCOUNTS_PATH", str(tmp_path / "acc.yaml"))
    with pytest.raises(ValueError):
        delivery_settings.save_settings(plain={"SMTP_HOST": "h\nSAPLOGON_EXE_PATH=evil"})
    with pytest.raises(ValueError):
        delivery_settings.save_settings(secrets={"SMTP_PASSWORD": "pw\r\nX=1"})
    with pytest.raises(ValueError):
        delivery_accounts.save_accounts({"email": [{"id": "acc1"}]},
                                        {"SMTP_PASSWORD__ACC1": "pw\nSAPLOGON_EXE_PATH=x"})
    with pytest.raises(ValueError):
        delivery_settings._rewrite_env({"BAD\nKEY": "1"})
    assert env.read_text() == "KEEP=1\n"
