"""Mail through Microsoft Graph (IBO_MAIL_TRANSPORT=graph), against a fake Graph."""
import base64
import email
import json
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text or json.dumps(body or {})

    def json(self):
        return self._body if self._body is not None else {}


class FakeGraph:
    def __init__(self):
        self.calls, self.token_calls, self.fail = [], 0, {}

    def post(self, url, data=None, timeout=None, **kw):
        assert "login.microsoftonline.com/tenant-1/oauth2/v2.0/token" in url
        assert data["client_secret"] == "s3cret" and data["grant_type"] == "client_credentials"
        self.token_calls += 1
        if self.fail.get("token"):
            return _Resp(401, {"error": "invalid_client",
                               "error_description": "AADSTS7000215: Invalid client secret provided."})
        return _Resp(200, {"access_token": "tok", "expires_in": 3600})

    def request(self, method, url, headers=None, data=None, json=None, timeout=None):
        assert headers["Authorization"] == "Bearer tok"
        self.calls.append((method, url, headers, data, json))
        for key, status in self.fail.items():
            if key != "token" and key in url:
                return _Resp(status, {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})
        if url.endswith("/messages") and method == "POST":
            return _Resp(201, {"id": "draft-1"})
        if url.endswith("createUploadSession"):
            return _Resp(201, {"uploadUrl": "https://upload.example/session"})
        return _Resp(202, {})

    def put(self, url, data=None, timeout=None, headers=None):
        self.calls.append(("PUT", url, headers, data, None))
        return _Resp(201 if "/" + str(len(data)) else 200, {})


@pytest.fixture
def graph(monkeypatch):
    from notifications import graph_mail
    fake = FakeGraph()
    monkeypatch.setattr(graph_mail, "_http", lambda: fake)
    graph_mail._token.update(value="", until=0, key="")
    for k, v in {"IBO_MAIL_TRANSPORT": "graph", "IBO_GRAPH_TENANT_ID": "tenant-1",
                 "IBO_GRAPH_CLIENT_ID": "client-1", "IBO_GRAPH_CLIENT_SECRET": "s3cret",
                 "IBO_GRAPH_SENDER": "ibops@infrabeat.com", "IBO_GRAPH_FROM_NAME": "InfraBeatOps Alerts"}.items():
        monkeypatch.setenv(k, v)
    return graph_mail, fake


def _mime(to="basis@infrabeat.com", cc="mgr@infrabeat.com"):
    m = MIMEText("<p>CPU 95%</p>", "html")
    m["Subject"], m["From"], m["To"], m["Cc"] = "PRD: CPU critical", "user.a.personal.example@gmail.com", to, cc
    return m


def test_small_mail_is_sent_as_mime_from_the_shared_mailbox(graph):
    gm, fake = graph
    gm.send(_mime(), ["basis@infrabeat.com", "mgr@infrabeat.com", "hidden@infrabeat.com"])
    method, url, headers, data, _ = fake.calls[-1]
    assert (method, url) == ("POST", "https://graph.microsoft.com/v1.0/users/ibops@infrabeat.com/sendMail")
    assert headers["Content-Type"] == "text/plain"
    sent = email.message_from_bytes(base64.b64decode(data))
    assert sent["From"] == "InfraBeatOps Alerts <ibops@infrabeat.com>"      # personal address gone
    assert sent["Bcc"] == "hidden@infrabeat.com"                             # envelope-only address kept
    assert sent["Subject"] == "PRD: CPU critical"


def test_token_is_reused(graph):
    gm, fake = graph
    gm.send(_mime(), ["basis@infrabeat.com"])
    gm.send(_mime(), ["basis@infrabeat.com"])
    assert fake.token_calls == 1


def test_large_report_goes_by_draft_and_upload(graph):
    gm, fake = graph
    m = MIMEMultipart()
    m["Subject"], m["To"] = "PRD monitoring report", "client@customer.com"
    m.attach(MIMEText("<p>Report attached</p>", "html"))
    small = MIMEApplication(b"x" * 1000, Name="summary.xlsx")
    small.add_header("Content-Disposition", "attachment", filename="summary.xlsx")
    big = MIMEApplication(b"%PDF" + b"y" * 4_000_000, Name="report.pdf")
    big.add_header("Content-Disposition", "attachment", filename="report.pdf")
    m.attach(small)
    m.attach(big)
    gm.send(m, ["client@customer.com"])
    steps = [(c[0], c[1].rsplit("/", 1)[-1]) for c in fake.calls]
    assert steps[0] == ("POST", "messages")                                  # draft
    assert ("POST", "attachments") in steps                                  # small one inline in JSON
    assert ("POST", "createUploadSession") in steps                          # big one uploaded
    puts = [c for c in fake.calls if c[0] == "PUT"]
    assert sum(len(c[3]) for c in puts) == 4_000_004 and len(puts) == 2      # in chunks
    assert steps[-1] == ("POST", "send")
    draft = fake.calls[0][4]
    assert draft["toRecipients"][0]["emailAddress"]["address"] == "client@customer.com"
    assert draft["body"]["contentType"] == "HTML"


def test_wrong_secret_is_a_permanent_error_with_a_hint(graph):
    gm, fake = graph
    fake.fail["token"] = True
    with pytest.raises(gm.GraphError) as e:
        gm.send(_mime(), ["basis@infrabeat.com"])
    assert e.value.permanent and "secret VALUE" in str(e.value)


def test_mailer_transmit_uses_graph_and_reports_access_denied(graph, monkeypatch):
    gm, fake = graph
    from notifications import mailer
    monkeypatch.setattr(mailer, "clear_suspension", lambda *a: None)
    monkeypatch.setattr(mailer, "suspension", lambda *a: None)
    cfg = gm.smtp_like_config()
    ok, why = mailer.transmit(_mime(), cfg, ["basis@infrabeat.com"])
    assert ok and why == "" and fake.calls[-1][1].endswith("/sendMail")
    fake.fail["sendMail"] = 403
    ok, why = mailer.transmit(_mime(), cfg, ["basis@infrabeat.com"])
    assert not ok and why.startswith("permanent") and "Mail.Send" in why


def test_config_needs_no_smtp_password_in_graph_mode(graph, monkeypatch):
    from core.config_loader import get_smtp_config
    for k in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "ALERT_FROM_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    cfg = get_smtp_config(None, require_recipients=False)
    assert cfg["from_email"] == "ibops@infrabeat.com" and cfg["host"] == "graph.microsoft.com"
    monkeypatch.delenv("IBO_GRAPH_CLIENT_SECRET")
    with pytest.raises(ValueError, match="IBO_GRAPH_CLIENT_SECRET"):
        get_smtp_config(None, require_recipients=False)


def test_off_by_default(monkeypatch):
    from notifications import graph_mail
    monkeypatch.delenv("IBO_MAIL_TRANSPORT", raising=False)
    assert not graph_mail.enabled()
