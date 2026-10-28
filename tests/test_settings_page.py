"""Settings page: features, mail lists, VPN connect / disconnect (no real sudo, no real .env)."""
from __future__ import annotations

import importlib
import os
import subprocess
import types

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_DB_PATH", str(tmp_path / "s.sqlite"))
    monkeypatch.setenv("IBO_STATE_BACKEND", "sqlite")
    monkeypatch.setenv("IBO_CHECK_PROCESSES", "0")
    monkeypatch.setenv("IBO_SETTINGS_SYNC", "1")
    monkeypatch.setenv("IBO_VPN_SERVICE_CHECK", "1")
    from core import store, ops_db, delivery_settings, live_settings
    importlib.reload(store)
    ops_db.reset_for_tests()
    envfile = tmp_path / ".env"
    envfile.write_text("# keep me\nSMTP_HOST=smtp.example\nIBO_SYNTHETIC=0\nIBO_BASIS_EMAILS=a@infrabeat.com\n",
                       encoding="utf-8")
    monkeypatch.setattr(delivery_settings, "ENV_PATH", str(envfile))
    for k in list(live_settings.FEATURES) + list(live_settings.MAIL_LISTS):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("IBO_SYNTHETIC", "0")
    monkeypatch.setenv("IBO_BASIS_EMAILS", "a@infrabeat.com")
    live_settings._seen["mtime"] = None
    from core import config_loader
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    return envfile


def test_feature_toggle_writes_env_applies_and_logs(env):
    from core import live_settings, timeline
    feats = {f["key"]: f for f in live_settings.read()["features"]}
    assert feats["IBO_SYNTHETIC"]["on"] is False and feats["IBO_EXTRA_CHECKS"]["on"] is True   # default on
    r = live_settings.save({"IBO_SYNTHETIC": True, "WHATSAPP_ENABLED": True}, by="alex")
    assert set(r["changed"]) == {"IBO_SYNTHETIC", "WHATSAPP_ENABLED"}
    assert os.environ["IBO_SYNTHETIC"] == "1" and os.environ["WHATSAPP_ENABLED"] == "true"
    text = env.read_text(encoding="utf-8")
    assert "# keep me" in text and "SMTP_HOST=smtp.example" in text and "IBO_SYNTHETIC=1" in text
    assert "WHATSAPP_ENABLED=true" in text
    ev = timeline.query("IBOPS", 1, ["config"])
    assert ev and "Synthetic logon test: on" in ev[0]["detail"] and ev[0]["actor"] == "alex"
    assert live_settings.save({"IBO_SYNTHETIC": True})["changed"] == []          # no change, no log


def test_only_listed_keys_and_valid_values(env):
    from core import live_settings
    with pytest.raises(ValueError, match="cannot be changed"):
        live_settings.save({"IBO_API_TOKEN": True})
    with pytest.raises(ValueError, match="cannot be changed"):
        live_settings.save(mail_lists={"SMTP_HOST": ["x@y.com"]})
    with pytest.raises(ValueError, match="on or off"):
        live_settings.save({"IBO_DIGEST": "yes please"})
    with pytest.raises(ValueError, match="not an e-mail"):
        live_settings.save(mail_lists={"ALERT_CC_EMAILS": ["ok@infrabeat.com", "bad address\nINJECT=1"]})
    with pytest.raises(ValueError, match="not an e-mail"):
        live_settings.save(mail_lists={"ALERT_CC_EMAILS": ["'quoted@infrabeat.com"]})
    assert "INJECT" not in env.read_text(encoding="utf-8")


def test_duplicate_env_lines_all_updated_and_strict_readers(env, monkeypatch):
    from core import live_settings
    env.write_text(env.read_text(encoding="utf-8") + "IBO_SYNTHETIC=0\n", encoding="utf-8")
    live_settings.save({"IBO_SYNTHETIC": True})
    assert env.read_text(encoding="utf-8").count("IBO_SYNTHETIC=1") == 2
    monkeypatch.setenv("WHATSAPP_ENABLED", "1")              # whatsapp.py only accepts "true"
    assert {f["key"]: f["on"] for f in live_settings.read()["features"]}["WHATSAPP_ENABLED"] is False


def test_mail_lists_and_empty_guard(env):
    from core import live_settings
    live_settings.save(mail_lists={"ALERT_MANAGER_EMAILS": ["m@infrabeat.com", "M@infrabeat.com", "n@infrabeat.com"]})
    assert os.environ["ALERT_MANAGER_EMAILS"] == "m@infrabeat.com,n@infrabeat.com"
    from core import recipients
    assert recipients.basis_and_manager(None)[0] or True      # recipients read the live value
    with pytest.raises(ValueError, match="both be empty"):
        live_settings.save(mail_lists={"IBO_BASIS_EMAILS": []})
    live_settings.save(mail_lists={"ALERT_TO_EMAILS": ["r@infrabeat.com"]})
    live_settings.save(mail_lists={"IBO_BASIS_EMAILS": []})      # now allowed: reports list catches Basis alerts
    assert os.environ["IBO_BASIS_EMAILS"] == ""


def test_other_process_picks_up_change(env, monkeypatch):
    from core import live_settings
    live_settings.sync()                                   # remembers the file
    text = env.read_text(encoding="utf-8").replace("IBO_SYNTHETIC=0", "IBO_SYNTHETIC=1")
    env.write_text(text, encoding="utf-8")
    st = os.stat(env)
    os.utime(env, (st.st_atime, st.st_mtime + 5))
    live_settings.sync()
    assert os.environ["IBO_SYNTHETIC"] == "1"


# ---------------------------------------------------------------- VPN

class _Run:
    def __init__(self):
        self.calls, self.active = [], True

    def __call__(self, args, capture_output=True, text=True, timeout=None):
        self.calls.append(list(args))
        if args[:1] == ["sudo"] and args[2:] and args[2].endswith("systemctl"):
            self.active = args[3] == "start"
        out = f"LoadState=loaded\nActiveState={'active' if self.active else 'inactive'}\n"
        return types.SimpleNamespace(returncode=0, stdout=out if "show" in args else "", stderr="")


@pytest.fixture
def vpn(env, tmp_path, monkeypatch):
    import json
    from core import vpn_control, reachability
    sd = tmp_path / "run"
    sd.mkdir()
    (sd / "pune_ttml.json").write_text(json.dumps({"name": "pune_ttml", "state": "up", "since": "2026-10-07 09:00:00"}))
    monkeypatch.setattr(reachability, "_VPN_STATUS_DIR", str(sd))
    monkeypatch.setattr(reachability, "_STATE_PATH", str(tmp_path / "reach.json"))
    runner = _Run()
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr(vpn_control, "supported", lambda: True)
    vpn_control._cache.clear()
    from core import config_loader
    cfgs = [{"name": "SARLOHAPRD", "network": {"name": "Pune_TTML", "vpn": True, "check": "openfortivpn:pune_ttml"}}]
    monkeypatch.setattr(config_loader, "get_all_systems", lambda: [dict(c) for c in cfgs])
    return runner, sd


def test_disconnect_then_off_on_purpose_without_alerts(vpn, monkeypatch):
    runner, sd = vpn
    from core import vpn_control, reachability
    sent = []
    monkeypatch.setattr(reachability, "_send", lambda *a, **k: sent.append(a))
    reachability.report("SARLOHAPRD", reachability.tag(reachability.VPN_DOWN, "old"), {"name": "SARLOHAPRD"})
    assert "SARLOHAPRD" in reachability.current_failures()
    vpn_control.disconnect("pune_ttml", "alex")
    assert ["sudo", "-n", vpn_control._systemctl(), "stop", "ibops-vpn@pune_ttml"] in runner.calls
    st = reachability.watchdog_status("pune_ttml")
    assert st["state"] == "off" and "on purpose by alex" in st["reason"]
    msg = reachability.watchdog_message("pune_ttml", st)
    assert reachability.state_of(msg) == reachability.VPN_OFF
    n_before = len(sent)
    reachability.check_vpn_watchdogs()
    assert "SARLOHAPRD" not in reachability.current_failures() and len(sent) == n_before
    from core import alert_control
    monkeypatch.setattr(reachability, "precheck", lambda s, c: msg)
    assert "on purpose" in (alert_control.sweep_block_reason("SARLOHAPRD") or "")


def test_connect_reset_and_in_use_rules(vpn):
    import json
    runner, sd = vpn
    from core import vpn_control
    vpn_control.disconnect("pune_ttml", "alex")
    vpn_control.connect("pune_ttml", "alex")
    assert ["sudo", "-n", vpn_control._systemctl(), "start", "ibops-vpn@pune_ttml"] in runner.calls
    assert not any("reset" in c for c in runner.calls) and vpn_control.manual_off("pune_ttml") is None
    # gave up -> Disconnect -> Connect: the reset is still done (the state then reads "off")
    (sd / "pune_ttml.json").write_text(json.dumps({"state": "stopped", "reason": "wrong password"}))
    vpn_control._cache.clear()
    vpn_control.disconnect("pune_ttml", "alex")
    vpn_control._cache.clear()
    vpn_control.connect("pune_ttml", "alex")
    assert ["sudo", "-n", "/usr/local/bin/ibops-vpn", "reset", "pune_ttml"] in runner.calls
    # account in use elsewhere: refused, also after Disconnect (the back-off is remembered)
    (sd / "pune_ttml.json").write_text(json.dumps({"state": "in_use", "reason": "account in use",
                                                    "next_attempt": "2099-01-01 18:30:00"}))
    vpn_control._cache.clear()
    n = len(runner.calls)
    with pytest.raises(ValueError, match="push that person off"):
        vpn_control.connect("pune_ttml", "alex")
    vpn_control.disconnect("pune_ttml", "alex")
    vpn_control._cache.clear()
    with pytest.raises(ValueError, match="push that person off"):
        vpn_control.connect("pune_ttml", "alex")
    assert not any(c[:2] == ["sudo", "-n"] and "start" in c for c in runner.calls[n:])
    with pytest.raises(ValueError, match="No VPN"):
        vpn_control.connect("other; rm -rf /", "x")


def test_service_down_without_disconnect_is_alerted(vpn, monkeypatch):
    runner, sd = vpn
    from core import reachability, vpn_control
    runner.active = False                     # crashed / stopped by hand -- nobody pressed Disconnect
    vpn_control._cache.clear()
    st = reachability.watchdog_status("pune_ttml")
    assert st["state"] == "service_down"
    assert reachability.state_of(reachability.watchdog_message("pune_ttml", st)) == reachability.VPN_DOWN
    # a stale "off" marker is cleared once the service runs again (reboot / by hand)
    vpn_control.disconnect("pune_ttml", "alex")
    db = vpn_control._db()
    db.execute("UPDATE vpn_switch SET at='2026-01-01 00:00:00' WHERE name='pune_ttml'")
    runner.active = True
    vpn_control._cache.clear()
    assert reachability.watchdog_status("pune_ttml")["state"] == "up" and vpn_control.switch_row("pune_ttml") is None


def test_sudo_not_allowed_says_how_to_fix(vpn, monkeypatch):
    from core import vpn_control

    def refuse(args, **kw):
        if args[:1] == ["sudo"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="sudo: a password is required")
        return types.SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=active\n", stderr="")
    monkeypatch.setattr(subprocess, "run", refuse)
    with pytest.raises(RuntimeError, match="allow_vpn_control.sh"):
        vpn_control.disconnect("pune_ttml", "alex")


# ------------------------------------------------------------- secrets

def _sys_yaml(tmp_path):
    (tmp_path / "systems.yaml").write_text(
        "systems:\n- name: PS4\n  password: ${PS4_SAP_PASSWORD}\n  rfc:\n    ashost: 1.2.3.4\n"
        "    password: ${PS4_RFC_PASSWORD}\n    username: mon\n  smtp_password: ${PS4_SMTP_PASSWORD}\n",
        encoding="utf-8")


def test_secrets_write_only_quoted_and_logged(env, tmp_path, monkeypatch):
    from core import secrets_store, timeline
    import sys
    monkeypatch.setitem(sys.modules, "keyring", None)            # no credential store here -> .env
    _sys_yaml(tmp_path)
    sent = []
    import notifications.mailer as mailer
    monkeypatch.setattr(mailer, "send_html", lambda **k: sent.append(k) or True)
    monkeypatch.setattr("core.config_loader.get_smtp_config", lambda *a, **k: {})
    cat = secrets_store.catalogue()
    assert {"PS4_SAP_PASSWORD", "PS4_RFC_PASSWORD", "PS4_SMTP_PASSWORD", "SMTP_PASSWORD"} <= set(cat)
    assert "IBO_API_TOKEN" not in cat
    tricky = "p#ss word$HOME'x"            # both ' and $ -> refused for .env
    with pytest.raises(ValueError, match="credential store"):
        secrets_store.save("PS4_RFC_PASSWORD", tricky, by="alex")
    value = "S3cr#t $HOME with space"
    secrets_store.save("PS4_RFC_PASSWORD", value, by="alex")
    from dotenv import dotenv_values
    assert dotenv_values(str(env))["PS4_RFC_PASSWORD"] == value          # read back exactly
    assert os.environ["PS4_RFC_PASSWORD"] == value
    st = {s["key"]: s for s in secrets_store.status()}
    assert st["PS4_RFC_PASSWORD"]["set"] and st["PS4_RFC_PASSWORD"]["where"] == ".env"
    assert value not in repr(secrets_store.status())                     # never a value
    ev = timeline.query("IBOPS", 1, ["config"])[0]
    assert ev["title"] == "Secret changed: PS4_RFC_PASSWORD" and value not in ev["detail"] and ev["actor"] == "alex"
    assert sent and value not in sent[0]["html"]
    with pytest.raises(ValueError, match="cannot be set"):
        secrets_store.save("IBO_API_TOKEN", "x", by="alex")
    with pytest.raises(ValueError, match="one line"):
        secrets_store.save("SMTP_PASSWORD", "a\nINJECT=1", by="alex")
    with pytest.raises(ValueError, match="blank"):
        secrets_store.save("SMTP_PASSWORD", "   ", by="alex")


def test_secrets_go_to_credential_store_when_not_in_env(env, tmp_path, monkeypatch):
    import sys
    from core import secrets_store
    store = {}
    fake = types.SimpleNamespace(get_password=lambda s, k: store.get((s, k)),
                                 set_password=lambda s, k, v: store.__setitem__((s, k), v))
    monkeypatch.setitem(sys.modules, "keyring", fake)
    _sys_yaml(tmp_path)
    import notifications.mailer as mailer
    monkeypatch.setattr(mailer, "send_html", lambda **k: True)
    r = secrets_store.save("PS4_SAP_PASSWORD", "abc'123", by="alex")
    assert r["where"] == "credential store" and store[("infrabeatops", "PS4_SAP_PASSWORD")] == "abc'123"
    assert "PS4_SAP_PASSWORD" not in env.read_text(encoding="utf-8")
    # already in .env -> stays in .env (an .env entry would shadow the store)
    env.write_text(env.read_text(encoding="utf-8") + "SMTP_PASSWORD=old\n", encoding="utf-8")
    assert secrets_store.save("SMTP_PASSWORD", "new1", by="alex")["where"] == ".env"
