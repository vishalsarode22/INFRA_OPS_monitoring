"""Job owner / dump-owning team mails wait for Basis approval (28.10.2026)."""
import json

import pytest


@pytest.fixture()
def om(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_DB_PATH", str(tmp_path / "t.sqlite"))
    from core import store, owner_mail
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t.sqlite"))
    try:
        store._local.__dict__.clear()
    except Exception:
        pass
    asked, sent = [], []
    monkeypatch.setattr(owner_mail, "_ask_basis", lambda rid, basis: asked.append((rid, list(basis))))
    monkeypatch.setattr(owner_mail, "_smtp", lambda s: {"from_email": "ibops@infrabeat.com"})
    from notifications import mailer
    monkeypatch.setattr(mailer, "send_html_now", lambda subj, html, to, smtp, cc=None, text_fallback="": sent.append((to, cc)) or True)
    from core import alert_ledger
    monkeypatch.setattr(alert_ledger, "record", lambda *a, **k: None)
    return owner_mail, asked, sent


def test_request_asks_basis_and_sends_nothing(om):
    owner_mail, asked, sent = om
    rid = owner_mail.request("CEAT S4P", "job_owner", "k1", ["owner@client-a.example"], ["basis@x.com"],
                             "subj", "<html><body>hi</body></html>", "hi", "job Z_X", ["basis@x.com"])
    assert rid and asked == [(rid, ["basis@x.com"])] and sent == []
    assert owner_mail.request("CEAT S4P", "job_owner", "k1", ["owner@client-a.example"], [], "s", "h", "", "w",
                              ["basis@x.com"]) is None                     # asked once per key per day
    assert [r["id"] for r in owner_mail.waiting(["CEAT S4P"])] == [rid]
    assert owner_mail.waiting(["OTHER"]) == []


def test_approve_sends_once_and_reject_never(om):
    owner_mail, _asked, sent = om
    a = owner_mail.request("S", "job_owner", "a", ["o@c.com"], ["b@x.com"], "s", "h", "", "w", ["b@x.com"])
    b = owner_mail.request("S", "dump_owner", "b", ["team@c.com"], [], "s", "h", "", "w", ["b@x.com"])
    assert "Done" in owner_mail.decide(a, True, by="alex")
    assert sent == [(["o@c.com"], ["b@x.com"])]
    with pytest.raises(ValueError):
        owner_mail.decide(a, True, by="again")                             # single use
    assert "Not sent" in owner_mail.decide(b, False, by="alex")
    assert len(sent) == 1 and owner_mail.row(b)["status"] == "rejected"


def test_job_owner_mail_goes_to_approval(monkeypatch, om):
    owner_mail, asked, sent = om
    from notifications import job_owner, mailer
    delivered = []
    monkeypatch.setattr(mailer, "deliver", lambda *a, **k: delivered.append(a) or True, raising=False)
    calls = []
    monkeypatch.setattr(owner_mail, "request", lambda *a, **k: calls.append(a) or 7)
    import inspect
    src = inspect.getsource(job_owner.notify_job_owners)
    assert "owner_mail.request" in src and src.index("owner_mail.request") < src.index("mailer.deliver")


def test_action_link_knows_owner_mail():
    from core import action_links
    assert "owner_mail" in action_links.ACTIONS


def test_access_rules_cover_the_new_routes():
    from core import access
    assert "owner_mail_requests" in access._ROW_TABLES
