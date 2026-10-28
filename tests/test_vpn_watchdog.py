"""
VPN watchdog retry rules (packaging/vpn/ibops_vpn_watchdog.py) and how the
dashboard turns its status into card text and mail (core/reachability.py).
"""
import importlib.util
import json
import os

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "packaging", "vpn", "ibops_vpn_watchdog.py")
_spec = importlib.util.spec_from_file_location("ibops_vpn_watchdog", _PATH)
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)

from core import reachability as R  # noqa: E402


# ------------------------------------------------------------ retry rules

def test_drop_then_three_failures_retries_2_5_15_minutes_then_stops():
    p = W.Policy()
    assert p.on_drop(uptime=3600)[:2] == (W.RETRYING, 120)
    assert p.on_failure("network")[:2] == (W.RETRYING, 300)
    assert p.on_failure("network")[:2] == (W.RETRYING, 900)
    state, delay, reason = p.on_failure("auth")
    assert state == W.STOPPED and delay is None and "user/password" in reason
    assert p.snapshot()["stopped"] is True


def test_success_resets_the_three_try_budget():
    p = W.Policy()
    p.on_drop(uptime=3600); p.on_failure("network")      # two used
    p.on_connected(); p.on_held()                        # tunnel held > 5 min
    assert p.on_drop(uptime=3600)[:2] == (W.RETRYING, 120)


def test_gateway_says_in_use_backs_off_30m_1h_2h_then_every_2h_forever():
    p = W.Policy()
    delays = [p.on_failure("in_use")[1] for _ in range(6)]
    assert delays == [1800, 3600, 7200, 7200, 7200, 7200]
    assert not p.snapshot()["stopped"]


def test_two_quick_drops_mean_someone_else_is_using_the_account():
    p = W.Policy()
    p.on_connected()
    assert p.on_drop(uptime=40)[0] == W.RETRYING          # first quick drop: normal retry
    p.on_connected()
    state, delay, reason = p.on_drop(uptime=30)           # second: do not fight
    assert state == W.IN_USE and delay == 1800 and "another session" in reason


def test_wrong_password_during_in_use_switches_to_the_three_try_rule():
    p = W.Policy()
    p.on_failure("in_use"); p.on_failure("in_use")
    assert p.on_failure("auth")[:2] == (W.RETRYING, 120)
    p.on_failure("auth"); p.on_failure("auth")
    assert p.on_failure("auth")[0] == W.STOPPED           # never an endless password loop


def test_certificate_change_is_an_ordinary_failure_with_its_reason():
    p = W.Policy()
    state, delay, reason = p.on_failure("cert")
    assert state == W.RETRYING and delay == 120 and "certificate changed" in reason


def test_stopped_survives_a_restart_until_reset():
    p = W.Policy()
    for _ in range(4):
        p.on_failure("auth")
    again = W.Policy(p.snapshot())
    assert again.stopped
    again.reset()
    assert not again.stopped and again.failures == 0


@pytest.mark.parametrize("text,kind", [
    ("ERROR:  Could not authenticate to gateway. Please check the password", "auth"),
    ("ERROR:  Gateway certificate validation failed, and the certificate digest is not in the local whitelist", "cert"),
    ("ERROR:  User already logged in", "in_use"),
    ("ERROR:  connect: Connection timed out", "network"),
])
def test_classify_openfortivpn_output(text, kind):
    assert W.classify(text) == kind


# ------------------------------------------------------ dashboard side

@pytest.fixture
def status_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_VPN_STATUS_DIR", str(tmp_path))
    return tmp_path


def _status(d, **st):
    (d / "pune_ttml.json").write_text(json.dumps(dict(name="pune_ttml", **st)))


SAR = {"name": "SARLOHA_QAS", "rfc": {"ashost": "192.0.2.127", "sysnr": "00"},
       "network": {"name": "Pune_TTML", "vpn": True, "check": "openfortivpn:pune_ttml"}}


def test_card_text_for_each_watchdog_state(status_dir, monkeypatch):
    monkeypatch.setattr(R, "_all_systems", lambda: [SAR])
    monkeypatch.setattr(R, "tcp_open", lambda *a, **k: False)
    _status(status_dir, state="retrying", reason="the tunnel dropped", attempt="1 of 3",
            next_attempt="2026-10-06 13:05:00")
    msg = R.diagnose_unreachable("SARLOHA_QAS", SAR, "192.0.2.127", 3300)
    assert msg.startswith("[VPN NOT CONNECTED]") and "attempt 1 of 3" in msg and "13:05" in msg
    _status(status_dir, state="in_use", reason="the VPN account is in use by another session",
            next_attempt="2026-10-06 13:35:00")
    assert "Not pushing the other session out" in R.diagnose_unreachable("SARLOHA_QAS", SAR, "192.0.2.127", 3300)
    _status(status_dir, state="stopped", reason="the gateway rejected the user/password")
    assert "sudo ibops-vpn reset pune_ttml" in R.diagnose_unreachable("SARLOHA_QAS", SAR, "192.0.2.127", 3300)
    _status(status_dir, state="up")
    assert R.state_of(R.diagnose_unreachable("SARLOHA_QAS", SAR, "192.0.2.127", 3300)) == R.SYSTEM_DOWN


def test_stopped_and_in_use_get_their_own_mail(status_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_all_systems", lambda: [SAR])
    monkeypatch.setattr(R, "_STATE_PATH", str(tmp_path / "reach.json"))
    monkeypatch.setenv("IBO_STATE_BACKEND", "files")
    sent = []
    monkeypatch.setattr(R, "_send", lambda *a, **k: sent.append(a))
    _status(status_dir, state="retrying", reason="the tunnel dropped", attempt="1 of 3")
    R.check_vpn_watchdogs()
    _status(status_dir, state="stopped", reason="the gateway rejected the user/password")
    R.check_vpn_watchdogs()
    keys = [a[7] for a in sent]                 # _send(..., key, ...)
    assert keys == ["Pune_TTML", "Pune_TTML:stopped"]


def test_no_watchdog_file_falls_back_to_other_evidence(status_dir, monkeypatch):
    monkeypatch.setattr(R, "_all_systems", lambda: [SAR])
    monkeypatch.setattr(R, "tcp_open", lambda *a, **k: False)
    msg = R.diagnose_unreachable("SARLOHA_QAS", SAR, "192.0.2.127", 3300)
    assert R.state_of(msg) == R.NOT_REACHABLE


def test_mail_subject_names_the_vpn_and_the_situation(status_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_all_systems", lambda: [SAR])
    monkeypatch.setattr(R, "_STATE_PATH", str(tmp_path / "reach.json"))
    monkeypatch.setenv("IBO_STATE_BACKEND", "files")
    sent = []
    monkeypatch.setattr(R, "_send", lambda *a, **k: sent.append(a))
    _status(status_dir, state="in_use", reason="the VPN account is in use by another session")
    R.check_vpn_watchdogs()
    subject = sent[-1][0]
    assert "Pune_TTML -- account in use elsewhere" in subject and ":in_use" not in subject


def test_probe_targets_come_from_comment_lines_in_the_config(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "CONF_DIR", str(tmp_path))
    (tmp_path / "x.conf").write_text("host = gw\n# ibops-probe = 192.0.2.127:3300\n"
                                     "#ibops-probe=192.0.2.123:3300\npassword = s\n")
    assert W.probe_targets("x") == [("192.0.2.127", 3300), ("192.0.2.123", 3300)]
    assert W.probe_ok([]) is None                          # no targets: no opinion
    assert W.probe_ok([("127.0.0.1", 1)], timeout=0.5) is False
