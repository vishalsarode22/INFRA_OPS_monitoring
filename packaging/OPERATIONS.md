# InfraBeatOps 8.4 — Deployment & Operations

## Windows

From the project root:

    venv\Scripts\activate
    python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8000

For packaged deployment, use `packaging\build.bat` and the existing Inno Setup
installer. Keep `.env` and `config\systems.yaml` out of source control.

The packaged launcher currently binds to `127.0.0.1` by default. Do not expose
the dashboard directly to an untrusted network without an authenticated or
restricted reverse proxy.

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
