# InfraBeatOps as three services

```
                 +-------------------+
   browser  -->  | reverse proxy     |  HTTPS, the only thing on the network
   (HTTPS)       | Caddy / nginx     |  packaging/proxy/
                 +---------+---------+
                           | http://127.0.0.1:8000
                 +---------v---------+      +------------------+      +------------------+
                 | services.web      |      | services.collect.|      | services.jobs    |
                 | UI, live RFC reads|      | scheduler, sweeps|      | mail, AI, maint. |
                 | login + scope     |      | SAP GUI (Windows)|      | watchdog         |
                 +---------+---------+      +---------+--------+      +---------+--------+
                           |                          |                         |
                           +----------- dashboard/ibops.sqlite (WAL) -----------+
```

Each process has one job, its own restart policy, and a heartbeat row in the
database. The web and jobs services watch each other's heartbeats and mail the
manager + Basis ("[MONITOR DOWN] ... service is not running") when one goes
stale for 5 minutes, then a recovery mail. If both die at once only an external
check can tell you: point an uptime monitor at `GET /healthz`.

## Windows VM (SAP GUI host)

```
pip install -r requirements.txt          # adds python-multipart
python scripts\ibops_user.py add admin --role admin
powershell -ExecutionPolicy Bypass -File packaging\windows\install_services.ps1
```

- `IBOPS-Web` and `IBOPS-Jobs` are Windows services (NSSM, restart on failure,
  log rotation under `logs\`).
- `IBOPS-Collector` is a **Scheduled Task at log-on**, restarted every minute on
  failure. It cannot be a service: SAP GUI scripting needs the interactive
  desktop. Keep the monitoring account logged on and the screen unlocked.
- HTTPS: run Caddy with `packaging\proxy\Caddyfile` (also under NSSM).

## Ubuntu collector (RFC only)

```
sudo cp packaging/systemd/ibops-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ibops-web ibops-collector ibops-jobs
journalctl -u ibops-collector -f
```

## Single process (lab / laptop)

`python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8000` still works
(IBO_ROLE=all): the scheduler runs in-process, mail is sent inline, the
heartbeat watchdog watches nothing. Set `IBO_STATE_BACKEND=files` to keep the
old JSON files instead of SQLite.

## State

Everything the services write at run time is in `dashboard/ibops.sqlite`:
snapshots, events, incidents, history, run history, registries, jobs,
heartbeats, the alert ledger, users and sessions. The first read of a path that
still exists as a JSON file imports it, so no manual migration. A nightly
maintenance job keeps seven daily backups in `dashboard/backups/`.

Configuration stays in files: `config/*.yaml`, `.env`, profiles.
Reports and screenshots stay under `reports/` (with retention).

## Login and customer scope

- `IBO_AUTH=1` (default). First start with no users: `IBO_ADMIN_PASSWORD` in
  `.env` creates `admin`; remove the line afterwards.
- Roles: `admin`, `operator` (run/snooze/acknowledge), `viewer`.
- Scope: add `customer: Metro` to each system in `config/systems.yaml`; create
  `python scripts/ibops_user.py add metro-noc --role viewer --customers Metro`.
  That user sees only Metro's systems on every page and API -- the filter is in
  `get_systems()`, so it covers endpoints written later too. Systems without a
  `customer:` belong to "Infrabeat".
- `X-IBO-Token` still works for scripts (admin, all customers).
- Internal lists for alerts: `IBO_BASIS_EMAILS`, `IBO_MANAGER_EMAILS`.

## New .env keys

```
IBO_ROLE=ui|collector|jobs|all        set by the service scripts
IBO_STATE_BACKEND=sqlite|files
IBO_DB_PATH=                          default dashboard/ibops.sqlite
IBO_MAIL_ASYNC=                       default on in split mode, off in single-process
IBO_AI_ASYNC=0                        1 = AI RCA runs in the jobs service, result goes to the snapshot
IBO_HEARTBEAT_STALE_SECONDS=300
IBO_EXPECTED_SERVICES=web,collector,jobs
IBO_MAINTENANCE_HOUR=03
IBO_AUTH=1
IBO_ADMIN_PASSWORD=                   first start only
IBO_WEB_HOST=127.0.0.1  IBO_WEB_PORT=8000  IBO_TRUSTED_PROXIES=127.0.0.1
```
