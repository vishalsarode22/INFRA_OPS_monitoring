# InfraBeatOps 8.4 — Deployment & Operations

## Windows

From the project root:

    .venv\Scripts\activate
    python -m dashboard.serve --port 8000

(`start.ps1`, `start_dashboard_hidden.ps1` and `packaging\windows\*` all do
the same.) For packaged deployment, use `packaging\build.bat` and the
existing Inno Setup installer. Keep `.env` and `config\systems.yaml` out of
source control.

## Host binding, logins and HTTPS

The dashboard binds to **127.0.0.1** (this machine only) everywhere by
default. Serving the network is an explicit opt-in:

| Setting (`.env` or environment) | Default | Meaning |
|---|---|---|
| `IBO_BIND_HOST` | `127.0.0.1` | Address uvicorn listens on. `0.0.0.0` or the VM IP = network. |
| `IBO_BIND_PORT` | `8000` | Port (`--port` overrides). |
| `IBO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Host names the app answers (others get 400). Add the name users type, e.g. `ibops.infrabeat.local`. The host of `IBO_PUBLIC_URL` is added automatically. |
| `IBO_TRUST_PROXY` | `0` | `1` = honour `X-Forwarded-For/-Proto` from `IBO_TRUSTED_PROXIES` (default `127.0.0.1`). Set it only when a reverse proxy on this host is the sole way in. |
| `IBO_COOKIE_SECURE` | unset | `1` forces the Secure cookie flag. |
| `IBO_SESSION_IDLE_MINUTES` / `IBO_SESSION_MAX_HOURS` | `120` / `12` | Session idle and absolute expiry. |

**Before publishing to the network, create logins** (run on the dashboard host,
from the project folder; passwords are prompted, never typed on the command line):

    python -m dashboard.manage_users add admin --role admin
    python -m dashboard.manage_users add shift1 --role operator
    python -m dashboard.manage_users add noc-wall --role viewer
    python -m dashboard.manage_users list

Roles: `viewer` read-only (reports included), `operator` + run/stop/snooze/
approve/incident actions, `admin` + systems, thresholds, settings, delivery
accounts, secrets, users. While **no user exists** the dashboard serves only
loopback clients (as admin, with a "no users configured" banner) and answers
everyone else 403.

Scripts use `Authorization: Bearer <IBO_API_TOKEN>`; the token is never given
to a browser (`/api/auth-token` is gone).

**Recommended network setup**: keep `IBO_BIND_HOST=127.0.0.1`, run the HTTPS
reverse proxy from `packaging/proxy/` (Caddy or nginx) as the only thing bound
to the network, and set

    IBO_TRUST_PROXY=1
    IBO_ALLOWED_HOSTS=ibops.infrabeat.local
    IBO_PUBLIC_URL=https://ibops.infrabeat.local

Cookies are then marked Secure. Binding the app itself to `0.0.0.0` without a
proxy works (plain HTTP, passwords cross the network unencrypted) and should be
limited to an isolated admin network.

## SLES/Linux

Install the project under `/opt/infrabeatops`, create a dedicated
`infrabeatops` user, create the virtual environment, and install
`requirements.txt`.

Copy `packaging/systemd/infrabeatops.service` to:

    /etc/systemd/system/infrabeatops.service

Then:

    sudo systemctl daemon-reload
    sudo systemctl enable --now infrabeatops
    sudo systemctl status infrabeatops

Logs:

    journalctl -u infrabeatops -f

Health:

    curl http://127.0.0.1:8000/healthz
    curl http://127.0.0.1:8000/readyz

## Configuration and secrets

Do not commit `.env` or `config/systems.yaml`. Use `.env.example` as the
template and manage real credentials through your secret-management process.

AI is optional. If the configured provider is unavailable, deterministic
monitoring remains authoritative and continues.

## Backup and recovery

Back up configuration and operational state required by your retention policy:
`.env` through secure secret backup, `config/systems.yaml`,
`dashboard/snapshots/`, incident/intelligence persistence, and required reports.

Do not back up `venv/`, caches, `__pycache__/`, or build artifacts.

After a crash/restart, verify `/healthz`, then `/readyz`, then confirm scheduler
status and the next monitoring cycle.

## Scheduler

The current dashboard scheduler has a 60-second startup grace period and runs
an automatic monitoring cycle every 15 minutes.
