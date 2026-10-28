import collectors.rfc_collector as rc
from notifications.whatsapp import _message_payload


def test_system_freezes_after_repeated_logon_failures():
    rc.clear_cooldown()
    for _ in range(rc._AUTH_MAX_FAILURES - 1):
        rc._park("QAS", "Name or password is incorrect (repeat logon)")
        rc._cooldowns.clear()          # pretend the timed cooldown expired
        assert rc.auth_frozen("QAS") is None
    rc._park("QAS", "Name or password is incorrect (repeat logon)")
    assert "Frozen" in rc.auth_frozen("QAS")
    remaining, err = rc.cooldown_remaining("QAS")
    assert remaining > 10 ** 6 and "Fix the credentials" in err
    # a timeout is not an auth failure and must not count
    rc.clear_cooldown("QAS")
    rc._park("QAS", "connection timed out")
    rc._park("QAS", "Name or password is incorrect")
    assert rc.auth_frozen("QAS") is None
    rc.clear_cooldown()


def test_success_resets_counter():
    rc.clear_cooldown()
    rc._park("PRD", "Name or password is incorrect")
    rc.note_success("PRD")
    assert rc._auth_failures.get("PRD") is None
    rc.clear_cooldown()


def test_whatsapp_uses_template_when_configured():
    p = _message_payload("m1", "cap", "9198", "ibops_alert", "en", ["QAS · 10:00", "SM50", "cpu 95%"])
    assert p["type"] == "template"
    assert p["template"]["name"] == "ibops_alert"
    assert p["template"]["components"][0]["parameters"][0]["image"]["id"] == "m1"
    assert [x["text"] for x in p["template"]["components"][1]["parameters"]] == ["QAS · 10:00", "SM50", "cpu 95%"]
    assert _message_payload("m1", "cap", "9198", "", "en", [])["type"] == "image"
