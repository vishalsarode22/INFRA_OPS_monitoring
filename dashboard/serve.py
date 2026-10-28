"""
Start the dashboard with the configured bind address.

    python -m dashboard.serve                 # 127.0.0.1:8000 (default)
    python -m dashboard.serve --port 8001

Every start script (start.ps1, start_dashboard_hidden.ps1,
packaging/windows/*, packaging/systemd/infrabeatops.service) goes through
here, so the bind address is decided in ONE place:

    IBO_BIND_HOST   default 127.0.0.1 (this machine only). Set it, in .env or
                    the environment, to opt in to serving the network, e.g.
                    0.0.0.0 or the VM's IP. Prefer 127.0.0.1 behind an HTTPS
                    reverse proxy (packaging/proxy/) instead.
    IBO_BIND_PORT   default 8000 (--port wins)
    IBO_TRUST_PROXY 1 = honour X-Forwarded-For/-Proto from IBO_TRUSTED_PROXIES
                    (default 127.0.0.1), i.e. a reverse proxy on this host.
    IBO_ALLOWED_HOSTS  Host names the app answers (see dashboard/app.py).
    IBO_TLS_CERTFILE / IBO_TLS_KEYFILE   serve HTTPS with this certificate and
                    key (PEM files, e.g. from company IT for ibops.infrabeat.net).
                    IBO_TLS_KEYFILE_PASSWORD if the key is protected.
    IBO_HTTP_REDIRECT_PORT  with HTTPS on: also listen on this port (usually
                    80) and send every plain-http request to the https address,
                    so typing ibops.infrabeat.net in a browser still works.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except Exception:
        pass


def bind_host() -> str:
    return (os.getenv("IBO_BIND_HOST") or "127.0.0.1").strip()


def bind_port(default: int = 8000) -> int:
    try:
        return int(os.getenv("IBO_BIND_PORT") or default)
    except ValueError:
        return default


def is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def tls_options() -> dict:
    """uvicorn ssl_* arguments when a certificate is configured, else {}."""
    cert = (os.getenv("IBO_TLS_CERTFILE") or "").strip()
    key = (os.getenv("IBO_TLS_KEYFILE") or "").strip()
    if not (cert and key):
        return {}
    for label, path in (("IBO_TLS_CERTFILE", cert), ("IBO_TLS_KEYFILE", key)):
        if not os.path.isfile(path):
            raise SystemExit(f"{label}={path} does not exist -- fix the path in .env or remove both TLS lines.")
    out = {"ssl_certfile": cert, "ssl_keyfile": key}
    pw = os.getenv("IBO_TLS_KEYFILE_PASSWORD")
    if pw:
        out["ssl_keyfile_password"] = pw
    return out


def start_http_redirect(host: str, https_port: int) -> None:
    """
    With HTTPS on, a tiny listener on IBO_HTTP_REDIRECT_PORT (e.g. 80) answers
    every request with a redirect to the same path on https. Runs in a daemon
    thread; a port already in use is logged, never fatal.
    """
    raw = (os.getenv("IBO_HTTP_REDIRECT_PORT") or "").strip()
    if not raw:
        return
    try:
        port = int(raw)
    except ValueError:
        print(f"IBO_HTTP_REDIRECT_PORT={raw} is not a number; no redirect.", file=sys.stderr)
        return
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    public = (os.getenv("IBO_PUBLIC_URL") or "").strip().rstrip("/")

    class Redirect(BaseHTTPRequestHandler):
        def _go(self):
            path = self.path if self.path.startswith("/") else "/"
            if public.startswith("https://"):
                target = public + path
            else:
                name = (self.headers.get("Host") or "").split(":")[0] or "localhost"
                target = f"https://{name}{'' if https_port == 443 else f':{https_port}'}{path}"
            self.send_response(301)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_HEAD = do_POST = _go

        def log_message(self, *a):           # quiet
            pass

    try:
        srv = ThreadingHTTPServer((host, port), Redirect)
    except OSError as exc:
        print(f"http->https redirect not started on port {port}: {exc}", file=sys.stderr)
        return
    threading.Thread(target=srv.serve_forever, name="http-redirect", daemon=True).start()
    print(f"http on port {port} redirects to https", file=sys.stderr)


def trust_proxy() -> bool:
    return str(os.getenv("IBO_TRUST_PROXY", "0")).strip().lower() in ("1", "true", "yes", "on")


def main(argv=None) -> None:
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    _load_env()
    ap = argparse.ArgumentParser(prog="python -m dashboard.serve")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)
    host, port = bind_host(), args.port or bind_port()
    if not is_loopback(host):
        print(f"WARNING: IBO_BIND_HOST={host} -- the dashboard is reachable from the network. "
              f"Create logins first (python -m dashboard.manage_users add <name> --role admin), "
              f"add this host's name to IBO_ALLOWED_HOSTS, and prefer an HTTPS reverse proxy.",
              file=sys.stderr)
    import uvicorn
    tls = tls_options()
    if tls:
        start_http_redirect(host, port)
    uvicorn.run("dashboard.app:app", host=host, port=port,
                proxy_headers=trust_proxy(),
                forwarded_allow_ips=os.getenv("IBO_TRUSTED_PROXIES", "127.0.0.1"), **tls)


if __name__ == "__main__":
    main()
