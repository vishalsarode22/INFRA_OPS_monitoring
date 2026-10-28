"""
Web service: the dashboard UI and live RFC reads, with login.

IBO_ROLE=ui -- never sweeps. Binds to 127.0.0.1 by default; put the
reverse proxy (packaging/proxy/) in front for HTTPS and bind the proxy, not
this, to the network. IBO_WEB_HOST / IBO_WEB_PORT override.
"""
from __future__ import annotations

import os

os.environ.setdefault("IBO_ROLE", "ui")

from services import common  # noqa: E402

log = common.get_logger("services.web")


def main() -> None:
    import uvicorn
    from core import store
    store.conn()
    common.start_heartbeat("web")
    from dashboard.serve import trust_proxy
    host = os.getenv("IBO_BIND_HOST") or os.getenv("IBO_WEB_HOST") or "127.0.0.1"
    port = int(os.getenv("IBO_BIND_PORT") or os.getenv("IBO_WEB_PORT") or "8000")
    log.info(f"web service on http://{host}:{port} (IBO_ROLE={os.environ['IBO_ROLE']})")
    uvicorn.run("dashboard.app:app", host=host, port=port, proxy_headers=trust_proxy(),
                forwarded_allow_ips=os.getenv("IBO_TRUSTED_PROXIES", "127.0.0.1"))


if __name__ == "__main__":
    main()
