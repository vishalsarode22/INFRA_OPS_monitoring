"""
Sending mail through Microsoft Graph instead of SMTP.

Microsoft switches off password (Basic) sign-in for SMTP by default from the
end of 2026. Graph needs no mailbox password: an app registered in Entra ID
(client id + secret) is allowed to send as ONE mailbox (Mail.Send, limited by
IT to that mailbox).

    IBO_MAIL_TRANSPORT=graph
    IBO_GRAPH_TENANT_ID=<directory (tenant) id>
    IBO_GRAPH_CLIENT_ID=<application (client) id>
    IBO_GRAPH_CLIENT_SECRET=<client secret VALUE, not its id>
    IBO_GRAPH_SENDER=ibops@infrabeat.com
    IBO_GRAPH_FROM_NAME=InfraBeatOps Alerts        (optional display name)

With IBO_MAIL_TRANSPORT=graph EVERY mail -- alerts, reports, RCA, account
mails -- leaves through notifications/mailer.py -> this module, From the
sender mailbox. SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD are then not
needed (and not used). Remove the line to go back to SMTP.

HOW
    token  POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token
           (client credentials, scope https://graph.microsoft.com/.default),
           cached until a minute before it expires.
    send   up to ~3 MB: POST /v1.0/users/<sender>/sendMail with the whole MIME
           message (base64) -- the mail is exactly what SMTP would have sent.
           Larger (reports with screenshots): a draft is created, every
           attachment uploaded (in chunks above 3 MB), then the draft is sent.
Only the VM's outgoing HTTPS to login.microsoftonline.com and
graph.microsoft.com is needed; nothing reaches the VM.

Check it from the project folder:
    .venv\\Scripts\\python.exe -m notifications.graph_mail you@infrabeat.com
"""
from __future__ import annotations

import base64
import email
import email.policy
import json
import os
import threading
import time
from email.utils import formataddr, getaddresses, parseaddr

from utils.logger import get_logger

log = get_logger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
MIME_LIMIT = 3_500_000          # base64 bytes; Graph refuses request bodies over 4 MB
CHUNK = 3_276_800               # upload chunk, a multiple of 320 KiB as Graph requires
_QUOTA_CODES = ("ErrorExceededMessageLimit", "ErrorQuotaExceeded", "MailboxStoreUnavailable",
                "ErrorMessageSubmissionBlocked", "SendQuotaExceeded")


class GraphError(Exception):
    """A refused Graph call. `permanent` = retrying will not help; `quota` = sending limit."""

    def __init__(self, message: str, status: int = 0, code: str = "", permanent: bool = False,
                 quota: bool = False):
        super().__init__(message)
        self.status, self.code, self.permanent, self.quota = status, code, permanent, quota


# ------------------------------------------------------------- settings

def _env(name: str) -> str:
    try:
        from core.config_loader import env_or_keyring
        return str(env_or_keyring(name, "") or "").strip()
    except Exception:  # noqa: BLE001
        return (os.getenv(name) or "").strip()


def enabled() -> bool:
    """
    Graph is used when IBO_MAIL_TRANSPORT=graph -- and also when the setting
    is missing but the Graph app is configured (27.10.2026: a .env without
    the line sent alerts from a personal Gmail account). Only
    IBO_MAIL_TRANSPORT=smtp switches back to SMTP.
    """
    t = (os.getenv("IBO_MAIL_TRANSPORT") or "").strip().strip('"').lower()
    if t.startswith("graph"):          # also a typo like "graphs"
        return True
    if t:
        return False
    return bool(_env("IBO_GRAPH_CLIENT_ID") and _env("IBO_GRAPH_SENDER"))


def sender() -> str:
    return _env("IBO_GRAPH_SENDER").lower()


def missing_settings() -> list[str]:
    return [k for k in ("IBO_GRAPH_TENANT_ID", "IBO_GRAPH_CLIENT_ID", "IBO_GRAPH_CLIENT_SECRET",
                        "IBO_GRAPH_SENDER") if not _env(k)]


def smtp_like_config() -> dict:
    """What config_loader.get_smtp_config returns in Graph mode (callers expect these keys)."""
    s = sender()
    return {"transport": "graph", "host": "graph.microsoft.com", "port": 443, "username": s,
            "password": "", "from_email": s, "use_tls": True, "account": "graph:" + s}


def _timeout() -> float:
    try:
        return float(os.getenv("IBO_SMTP_TIMEOUT", "20") or 20) + 10
    except ValueError:
        return 30.0


# ---------------------------------------------------------------- token

_token = {"value": "", "until": 0.0, "key": ""}
_token_lock = threading.Lock()


def _http():
    import requests
    return requests


def _graph_error(resp, what: str) -> GraphError:
    status = resp.status_code
    code, text = "", ""
    try:
        body = resp.json()
        err = body.get("error")
        if isinstance(err, dict):
            code, text = str(err.get("code") or ""), str(err.get("message") or "")
        else:                                   # the token endpoint
            code, text = str(err or ""), str(body.get("error_description") or "")
    except Exception:  # noqa: BLE001
        text = (resp.text or "")[:300]
    hint = ""
    if "AADSTS7000215" in text or "AADSTS7000222" in text:
        hint = " -- the client secret is wrong or expired; use the secret VALUE (not its ID)"
    elif "AADSTS700016" in text:
        hint = " -- IBO_GRAPH_CLIENT_ID is not an app in this tenant"
    elif "AADSTS90002" in text or "AADSTS900023" in text:
        hint = " -- IBO_GRAPH_TENANT_ID is wrong"
    elif status == 403 or code in ("ErrorAccessDenied", "AccessDenied"):
        hint = (" -- the app may not send as this mailbox: ask IT for Mail.Send (application) with admin "
                "consent, scoped to " + (sender() or "the sender mailbox"))
    elif status == 404 or code in ("ErrorInvalidUser", "ResourceNotFound", "MailboxNotEnabledForRESTAPI"):
        hint = f" -- no mailbox {sender()} (check IBO_GRAPH_SENDER)"
    quota = any(c in code for c in _QUOTA_CODES) or "quota" in text.lower()
    transient = status in (408, 429) or status >= 500
    return GraphError(f"Graph {what}: HTTP {status} {code} {text}".strip()[:280] + hint, status, code,
                      permanent=not transient, quota=quota)


def token() -> str:
    missing = missing_settings()
    if missing:
        raise GraphError("Graph mail is switched on (IBO_MAIL_TRANSPORT=graph) but .env lacks "
                         + ", ".join(missing), permanent=True)
    tenant, client, secret = _env("IBO_GRAPH_TENANT_ID"), _env("IBO_GRAPH_CLIENT_ID"), _env("IBO_GRAPH_CLIENT_SECRET")
    key = f"{tenant}|{client}|{hash(secret)}"
    with _token_lock:
        if _token["value"] and _token["key"] == key and time.time() < _token["until"]:
            return _token["value"]
        try:
            resp = _http().post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                                data={"client_id": client, "client_secret": secret,
                                      "scope": "https://graph.microsoft.com/.default",
                                      "grant_type": "client_credentials"}, timeout=_timeout())
        except Exception as exc:  # noqa: BLE001 -- network: transient
            raise GraphError(f"Graph sign-in: cannot reach login.microsoftonline.com "
                             f"({type(exc).__name__}: {exc})"[:300]) from None
        if resp.status_code != 200:
            raise _graph_error(resp, "sign-in")
        body = resp.json()
        _token.update(value=body["access_token"], key=key,
                      until=time.time() + max(60, int(body.get("expires_in", 3600)) - 60))
        return _token["value"]


def _call(method: str, url: str, what: str, **kw):
    headers = kw.pop("headers", {})
    headers["Authorization"] = "Bearer " + token()
    try:
        resp = _http().request(method, url, headers=headers, timeout=_timeout(), **kw)
    except Exception as exc:  # noqa: BLE001
        raise GraphError(f"Graph {what}: cannot reach graph.microsoft.com ({type(exc).__name__}: {exc})"[:300]) from None
    if resp.status_code == 401:                 # token revoked or rotated: one fresh try
        with _token_lock:
            _token["value"] = ""
        headers["Authorization"] = "Bearer " + token()
        resp = _http().request(method, url, headers=headers, timeout=_timeout(), **kw)
    if resp.status_code >= 300:
        raise _graph_error(resp, what)
    return resp


# ----------------------------------------------------------------- send

def _prepare(msg, envelope: list[str]) -> None:
    """From = the sender mailbox; every envelope address must be in To/Cc/Bcc (Graph reads headers)."""
    s = sender()
    name = (os.getenv("IBO_GRAPH_FROM_NAME") or "").strip() or parseaddr(str(msg.get("From") or ""))[0]
    del msg["From"]
    msg["From"] = formataddr((name, s)) if name else s
    for h in ("Sender", "Return-Path"):
        del msg[h]
    shown = {a.lower() for _n, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", [])
                                                 + msg.get_all("Bcc", [])) if a}
    hidden = [e for e in envelope if e and e.lower() not in shown]
    if hidden:
        del msg["Bcc"]
        msg["Bcc"] = ", ".join(hidden)


def send(msg, envelope: list[str]) -> None:
    """Send a prepared MIME message as the sender mailbox. Raises GraphError."""
    _prepare(msg, envelope)
    raw = msg.as_bytes()
    b64 = base64.b64encode(raw)
    url = f"{GRAPH}/users/{sender()}"
    if len(b64) <= MIME_LIMIT:
        _call("POST", url + "/sendMail", "send", data=b64, headers={"Content-Type": "text/plain"})
        return
    _send_large(raw, url)


def _addrs(values) -> list[dict]:
    return [{"emailAddress": {"address": a, **({"name": n} if n else {})}}
            for n, a in getaddresses(values or []) if a]


def _send_large(raw: bytes, url: str) -> None:
    """Draft + attachment upload + send, for mails too big for one request."""
    m = email.message_from_bytes(raw, policy=email.policy.default)
    body = m.get_body(preferencelist=("html", "plain"))
    content = body.get_content() if body is not None else ""
    ctype = "HTML" if body is not None and body.get_content_subtype() == "html" else "Text"
    draft = {"subject": str(m.get("Subject") or ""), "body": {"contentType": ctype, "content": content},
             "toRecipients": _addrs(m.get_all("To")), "ccRecipients": _addrs(m.get_all("Cc")),
             "bccRecipients": _addrs(m.get_all("Bcc")), "replyTo": _addrs(m.get_all("Reply-To"))}
    created = _call("POST", url + "/messages", "draft", json=draft).json()
    mid = created["id"]
    try:
        for part in m.iter_attachments():
            data = part.get_content()
            if isinstance(data, str):
                data = data.encode("utf-8")
            name = part.get_filename() or "attachment"
            cid = (part.get("Content-ID") or "").strip("<> ")
            inline = bool(cid) and (part.get_content_disposition() or "inline") == "inline"
            ctype_a = part.get_content_type()
            if len(data) < 3_000_000:
                _call("POST", f"{url}/messages/{mid}/attachments", "attach", json={
                    "@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                    "contentType": ctype_a, "contentBytes": base64.b64encode(data).decode(),
                    "isInline": inline, **({"contentId": cid} if cid else {})})
                continue
            item = {"attachmentType": "file", "name": name, "size": len(data), "contentType": ctype_a,
                    "isInline": inline, **({"contentId": cid} if cid else {})}
            session = _call("POST", f"{url}/messages/{mid}/attachments/createUploadSession", "upload",
                            json={"AttachmentItem": item}).json()
            up = session["uploadUrl"]           # pre-authorised: no bearer token on the chunks
            for start in range(0, len(data), CHUNK):
                chunk = data[start:start + CHUNK]
                r = _http().put(up, data=chunk, timeout=_timeout() + 60, headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(data)}"})
                if r.status_code >= 300:
                    raise _graph_error(r, f"upload {name}")
        _call("POST", f"{url}/messages/{mid}/send", "send")
    except Exception:
        try:
            _call("DELETE", f"{url}/messages/{mid}", "delete draft")
        except Exception:  # noqa: BLE001
            pass
        raise


# ------------------------------------------------------------ self-test

def main(argv=None) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(root, ".env"))
    except Exception:  # noqa: BLE001
        pass
    if not argv:
        print("usage: python -m notifications.graph_mail <your address>")
        return 2
    missing = missing_settings()
    if missing:
        print("Missing in .env: " + ", ".join(missing))
        return 1
    print(f"1/2 signing in to Microsoft as app {_env('IBO_GRAPH_CLIENT_ID')[:8]}... ", end="", flush=True)
    try:
        token()
        print("ok")
    except GraphError as exc:
        print("FAILED\n    " + str(exc))
        return 1
    from email.mime.text import MIMEText
    msg = MIMEText("<p>Test mail from InfraBeatOps through Microsoft Graph. If you can read this, "
                   "IBO_MAIL_TRANSPORT=graph works.</p>", "html")
    msg["Subject"] = "InfraBeatOps: Graph mail test"
    msg["To"] = argv[0]
    print(f"2/2 sending a test mail from {sender()} to {argv[0]} ... ", end="", flush=True)
    try:
        send(msg, [argv[0]])
        print("ok -- check the inbox (and Sent Items of the shared mailbox)")
    except GraphError as exc:
        print("FAILED\n    " + str(exc))
        return 1
    if not enabled():
        print("\nGraph works. Add IBO_MAIL_TRANSPORT=graph to .env and restart the dashboard to send "
              "every mail this way.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
