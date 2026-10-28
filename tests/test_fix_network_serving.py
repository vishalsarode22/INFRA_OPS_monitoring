"""Serving the company address (ibops.infrabeat.net): allowed hosts, HTTPS options, http->https redirect."""
import http.client
import os
import socket
import time

import pytest


def test_company_name_added_and_local_kept(monkeypatch):
    pytest.importorskip("fastapi")
    import dashboard.app as A
    monkeypatch.setenv("IBO_ALLOWED_HOSTS", "ibops.infrabeat.net,10.1.0.43")
    monkeypatch.setenv("IBO_PUBLIC_URL", "https://ibops.infrabeat.net")
    hosts = A.allowed_hosts()
    for h in ("ibops.infrabeat.net", "10.1.0.43", "localhost", "127.0.0.1"):
        assert h in hosts


def test_tls_options(tmp_path, monkeypatch):
    from dashboard import serve
    monkeypatch.delenv("IBO_TLS_CERTFILE", raising=False)
    monkeypatch.delenv("IBO_TLS_KEYFILE", raising=False)
    assert serve.tls_options() == {}
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("y")
    monkeypatch.setenv("IBO_TLS_CERTFILE", str(cert))
    monkeypatch.setenv("IBO_TLS_KEYFILE", str(key))
    assert serve.tls_options() == {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    monkeypatch.setenv("IBO_TLS_KEYFILE", str(tmp_path / "missing.pem"))
    with pytest.raises(SystemExit, match="does not exist"):
        serve.tls_options()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_http_requests_are_redirected_to_https(monkeypatch):
    from dashboard import serve
    port = _free_port()
    monkeypatch.setenv("IBO_HTTP_REDIRECT_PORT", str(port))
    monkeypatch.setenv("IBO_PUBLIC_URL", "https://ibops.infrabeat.net")
    serve.start_http_redirect("127.0.0.1", 443)
    time.sleep(0.2)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/alerts-page?system=PS4", headers={"Host": "ibops.infrabeat.net"})
    r = c.getresponse()
    assert r.status == 301 and r.getheader("Location") == "https://ibops.infrabeat.net/alerts-page?system=PS4"
