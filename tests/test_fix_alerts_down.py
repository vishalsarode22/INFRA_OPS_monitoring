"""
Finding 6: an RFC connection failure was graded UNKNOWN and never mailed;
the SAP_SYSTEM_UNREACHABLE consecutive_failures setting was ignored. Now:
DOWN (and NOT REACHABLE) after N consecutive failed polls -> manager +
Basis, every 5 min; VPN -> Basis only; logon refused -> CREDENTIALS to Basis.
Finding 9: the per-user dump rule (> 10 dumps by one user in 15 min).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_fix_alerts import fx  # noqa: E402,F401

from core import reachability as R  # noqa: E402
from core import correlation  # noqa: E402  (imported before the fixture moves CONFIG_DIR)

PS4 = {"name": "PS4", "rfc": {"ashost": "10.0.0.5", "sysnr": "00"}}


@pytest.fixture
def mails(fx, monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_STATE_PATH", str(tmp_path / "reach.json"))
    sent = []
    monkeypatch.setattr(R, "_send", lambda subject, html, to, cc, cfg, system, kind, key, interval, sev:
                        sent.append({"subject": subject, "to": to, "cc": cc, "kind": kind,
                                     "interval": interval, "sev": sev, "html": html}))
    monkeypatch.setattr(R, "_on_state_change", lambda *a: None)
    monkeypatch.setattr(R, "_on_recovered", lambda *a: None)
    return sent


def test_consecutive_failures_come_from_the_correlation_rule(fx, monkeypatch):
    monkeypatch.setattr(correlation, "load_correlation_config",
                        lambda *a, **k: {"SAP_SYSTEM_UNREACHABLE": {"thresholds": {"consecutive_failures": 3}}})
    assert R.down_after() == 3
    monkeypatch.setenv("IBO_DOWN_CONSECUTIVE_FAILURES", "2")
    assert R.down_after() == 2


def test_down_after_two_polls_to_manager_and_basis_every_5_min(mails, monkeypatch):
    monkeypatch.setenv("IBO_DOWN_CONSECUTIVE_FAILURES", "2")
    R.report("PS4", R.tag(R.SYSTEM_DOWN, "PS4 is DOWN"), PS4)
    assert mails == []
    R.report("PS4", R.tag(R.SYSTEM_DOWN, "PS4 is DOWN"), PS4)
    m = mails[-1]
    assert m["to"] == ["manager@infrabeat.com"] and m["cc"] == ["basis@infrabeat.com"]
    assert m["interval"] == 300 and m["sev"] == "CRITICAL"
    assert "client@customer.com" not in m["to"] + m["cc"]


def test_untagged_rfc_connection_error_now_alerts(mails, monkeypatch):
    monkeypatch.setenv("IBO_DOWN_CONSECUTIVE_FAILURES", "2")
    err = "RFC_COMMUNICATION_FAILURE: partner '10.0.0.5:sapgw00' not reached (WSAETIMEDOUT)"
    R.report("PS4", err, PS4)
    R.report("PS4", err, PS4)
    assert mails and mails[-1]["kind"] == "not_reachable"
    assert mails[-1]["to"] == ["manager@infrabeat.com"]
    assert R.current_failures()["PS4"]["state"] == R.NOT_REACHABLE


def test_untagged_logon_refusal_is_credentials_to_basis_not_down(mails):
    R.report("PS4", "RFC_LOGON_FAILURE: Name or password is incorrect (repeat logon)", PS4)
    assert mails[-1]["kind"] == "credentials" and mails[-1]["to"] == ["basis@infrabeat.com"]
    assert mails[-1]["cc"] == []


def test_vpn_failed_is_basis_only(mails):
    R.report("PS4", R.tag(R.VPN_DOWN, "VPN not connected"), PS4)
    assert mails[-1]["kind"] == "vpn_down" and mails[-1]["to"] == ["basis@infrabeat.com"]


def test_one_failed_poll_then_ok_sends_no_recovery(mails, monkeypatch):
    monkeypatch.setenv("IBO_DOWN_CONSECUTIVE_FAILURES", "2")
    R.report("PS4", R.tag(R.SYSTEM_DOWN, "blip"), PS4)
    R.report_ok("PS4", PS4)
    assert mails == []


def test_down_mail_body_is_escaped(mails, monkeypatch):
    monkeypatch.setenv("IBO_DOWN_CONSECUTIVE_FAILURES", "1")
    R.report("PS4", R.tag(R.SYSTEM_DOWN, "<script>x</script>"), PS4)
    assert "<script>" not in mails[-1]["html"] and "&lt;script&gt;" in mails[-1]["html"]


# ------------------------------------------------------------- per-user dumps

def _raw(rows, sap_now="2026-10-14 10:00:00"):
    return {"sap_now": sap_now,
            "wp": {"instances": [], "error": "x"},
            "dumps": {"rows": rows, "error": ""},
            "cancelled": {"rows": [], "error": ""}, "locks": {"rows": [], "error": ""},
            "sm58": {"count": 0, "dests": {}, "error": ""},
            "smq2": {"rows": [], "id_kind": "entry", "error": ""}}


def _dumps(user, n, start=0):
    return [{"date": "20261014", "time": f"09{(start + i) // 60:02d}{(start + i) % 60:02d}", "user": user,
             "error": "MESSAGE_TYPE_X", "program": "ZP", "host": "h"} for i in range(n)]


def test_more_than_ten_dumps_by_one_user_alerts_once_an_hour(fx, monkeypatch):
    from core import custom_alerts as CA
    from datetime import timedelta
    (fx.tmp / "thresholds_systems.yaml").write_text(
        "systems:\n  PS4:\n    sap.st22.user_dumps_10min:\n      alert: warning\n      channels: [email]\n"
        "      critical: 10\n      inclusive: false\n", encoding="utf-8")
    fx.T._cache.clear()
    sent = []
    run = lambda raw: CA.process({"name": "PS4"}, raw, now=fx.clock.now,  # noqa: E731
                                 sender=lambda c, j: sent.append(j))
    run(_raw([]))                                   # first read: baseline
    fx.clock.tick(5)
    run(_raw(_dumps("EY_SD", 10)))                  # exactly 10: not "more than 10"
    assert [j for j in sent if j["instance"] == "EY_SD"] == []
    fx.clock.tick(5)
    run(_raw(_dumps("EY_SD", 11)))                  # 11 by one user -> user-specific alert
    alerts = [j for j in sent if j["instance"] == "EY_SD"]
    assert len(alerts) == 1 and alerts[0]["level"] == "CRITICAL"
    assert "by user EY_SD" in fx.E.shown_label(alerts[0])
    fx.clock.tick(5)
    run(_raw(_dumps("EY_SD", 14)))                  # still over, inside the hour: no repeat
    assert len([j for j in sent if j["instance"] == "EY_SD"]) == 1
