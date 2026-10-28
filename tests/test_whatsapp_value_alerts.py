"""WhatsApp value alerts (notifications/whatsapp.send_value_alert) and their wiring in core/alert_engine."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notifications import whatsapp as wa  # noqa: E402

ENV = {"WHATSAPP_ENABLED": "true", "WHATSAPP_MODE": "cloud_api", "WHATSAPP_PHONE_NUMBER_ID": "123",
       "WHATSAPP_ACCESS_TOKEN": "tok", "WHATSAPP_ALERT_TEMPLATE": "ibops_alert", "WHATSAPP_TEMPLATE_LANG": "en",
       "IBO_PUBLIC_URL": "http://10.1.0.5:8000"}
JOB = {"system": "CEAT S4P", "metric": "sap.st04.memory_used_pct", "instance": "", "label": "HANA memory used",
       "level": "CRITICAL", "kind": "alert", "value": "91%", "limits": "warning 80, critical 90",
       "detail": "tenant S4P"}


class Resp:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body or {}

    def json(self):
        return self._b


class Sender(unittest.TestCase):
    def test_not_ready_says_why(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_ENABLED": "false"}, clear=False):
            self.assertEqual(wa.value_alert_state()[0], False)
            ok, who, note = wa.send_value_alert(JOB, ["+91 98765 43210"])
            self.assertFalse(ok)
            self.assertEqual(who, "+919876543210")
            self.assertIn("WHATSAPP_ENABLED", note)
        env = dict(ENV, WHATSAPP_ACCESS_TOKEN="")
        with mock.patch.dict(os.environ, env):
            self.assertIn("WHATSAPP_ACCESS_TOKEN", wa.value_alert_state()[1])

    def test_template_message_per_number(self):
        calls = []
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(wa.requests, "post", side_effect=lambda url, **kw: calls.append((url, kw)) or Resp(200)):
            ok, who, note = wa.send_value_alert(JOB, ["9876543210", "+919876543210", "+44 7700 900123"])
        self.assertTrue(ok)
        self.assertEqual(note, "")
        self.assertEqual(len(calls), 2)                               # duplicate number sent once
        url, kw = calls[0]
        self.assertEqual(url, "https://graph.facebook.com/v21.0/123/messages")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer tok")
        body = kw["json"]
        self.assertEqual((body["to"], body["type"], body["template"]["name"]), ("919876543210", "template", "ibops_alert"))
        p = [x["text"] for x in body["template"]["components"][0]["parameters"]]
        self.assertEqual(p[0], "CRITICAL · CEAT S4P")
        self.assertTrue(p[1].startswith("HANA memory used = 91% (warning 80, critical 90) · "))
        self.assertIn("tenant S4P", p[1])
        self.assertEqual(p[2], "http://10.1.0.5:8000/system?name=CEAT%20S4P")
        self.assertTrue(all("\n" not in x for x in p))

    def test_meta_errors_in_plain_words(self):
        answers = iter([Resp(400, {"error": {"code": 132001, "message": "x"}}), Resp(200)])
        with mock.patch.dict(os.environ, ENV), mock.patch.object(wa.requests, "post", side_effect=lambda *a, **k: next(answers)):
            ok, who, note = wa.send_value_alert(JOB, ["9876543210", "9876500000"])
        self.assertTrue(ok)
        self.assertIn("+919876543210: template not found", note)

    def test_test_mode_sends_nothing(self):
        with mock.patch.dict(os.environ, dict(ENV, WHATSAPP_MODE="test")), \
                mock.patch.object(wa.requests, "post") as post:
            ok, who, note = wa.send_value_alert(JOB, ["9876543210"])
        post.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("test mode", note)

    def test_recovery_and_repeat_wording(self):
        with mock.patch.dict(os.environ, ENV):
            t = wa.value_alert_texts(dict(JOB, kind="recovery", level="NORMAL", value="70%"))
            self.assertEqual(t[0], "RECOVERED · CEAT S4P")
            self.assertTrue(t[1].startswith("HANA memory used back to normal: 70%"))
            self.assertEqual(wa.value_alert_texts(dict(JOB, kind="repeat"))[0], "CRITICAL (still) · CEAT S4P")


class Engine(unittest.TestCase):
    """alert_engine._send_all sends WhatsApp to the system's numbers and records the outcome."""

    def setUp(self):
        from core import store
        self._old = store.DB_PATH
        store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.sqlite")

    def tearDown(self):
        from core import store
        store.DB_PATH = self._old

    def test_send_all_whatsapp(self):
        from core import alert_engine as ae, ops_db
        rcp = {"email": {"to": [], "cc": []}, "whatsapp": ["+919876543210"], "sms": [], "call": []}
        job = dict(JOB, channels=["whatsapp"], batch="b1")
        with mock.patch.dict(os.environ, ENV), mock.patch.object(ae, "recipients", lambda cfg: rcp), \
                mock.patch.object(wa.requests, "post", return_value=Resp(200)):
            self.assertTrue(ae.channel_status()["whatsapp"]["ready"])
            ae._send_all({"name": "CEAT S4P"}, job)
        row = ops_db.one("SELECT * FROM alert_sends WHERE channel='whatsapp' ORDER BY id DESC LIMIT 1")
        self.assertEqual((row["system"], row["metric"]), ("CEAT S4P", "sap.st04.memory_used_pct"))
        self.assertTrue(row["ok"] in (1, True, "1"))
        self.assertIn("919876543210", str(row["recipients"] if "recipients" in row else row))


if __name__ == "__main__":
    unittest.main()
