# InfraBeatOps -- operations features (October 2026)

What was added, where it lives, and how to switch each part on. Nothing
here changes how sweeps collect data; everything is on top of the existing
alerts, wall and dashboard. All of it runs in both deployments
(`IBO_ROLE=all` on the Windows VM and on the Ubuntu VM).

## For the Basis team

| Feature | Where | How |
|---|---|---|
| **Maintenance windows** | Wall card -> *Alert* menu -> *Maintenance window*; Operations -> Maintenance | No alert mail of any kind, no scheduled sweeps, card in grey with the reason. `*` = all systems. Max 7 days (`IBO_MAINT_MAX_HOURS`). |
| **Snooze / Acknowledge / Ignore until fixed** | Buttons in every alert mail (needs `IBO_PUBLIC_URL`), *Alert* menu on the card, Operations -> Now | Snooze = no repeats until a time. Acknowledge = no repeats until the problem clears (wall shows who). Ignore until fixed = the same, **and sweeps are parked** until someone presses *Resume*. Recovery mails are never snoozed. |
| **Parked sweeps** | Card tag *SWEEPS PARKED -> resume*; Operations -> Now | Automatic on CREDENTIALS (`IBO_AUTO_PARK`), so a wrong password never locks the SAP user. A manual *Monitor this system* still runs. |
| **On-call rotation** | `config/oncall.yaml` (copy the `.example`); Operations -> On call | Basis alerts go to whoever is on shift; covers for leave on the page; optional `shift_source` URL to ask your shift tool. The person on call is copied on every critical alert. |
| **Ticket creation** | `.env`: `IBO_TICKETS=email|webhook|jira|servicenow` + its settings (see `core/tickets.py`); Operations -> Tickets | SYSTEM DOWN and repeated (red) critical alerts open one ticket per system+alert with the critical metrics, the AI summary and up to 3 screenshots. Jira / ServiceNow are polled every 5 min; closing the ticket clears the alert and saves the resolution to the knowledge base. |
| **One timeline per system** | Sidebar -> Timeline; *Timeline* link in mails and on cards | Alerts, state changes, sweeps, VPN, maintenance, acks, tickets, actions, runbook ticks, config changes. Kept 180 days. |

## Smarter monitoring

| Feature | Where | How |
|---|---|---|
| **Learned normal values** | Operations -> Learned normal; detail text on every graded metric | Per system, metric and hour of the week. Needs 3 weeks of readings at that hour (or 5 days at that hour). `IBO_LEARNED_MODE=smart` (default): a warning that is normal for the hour is graded normal, an unusual value becomes a warning, critical never changes. `annotate` only adds the note; `off`. |
| **Capacity forecasting** | Operations -> Capacity; daily mail to Basis when something runs out within 30 days | File systems are now read from CCMS RZ20 (hourly, `IBO_CCMS_FILESYSTEMS`) and appear as `disk_<mount>` metrics; SSH `disk_*` metrics are forecast too. `config/capacity.yaml.example` for other limits. |
| **Knowledge base** | Operations -> Knowledge base; "Last time this was fixed by ..." in alert mails and tickets | Filled when an incident is resolved with a summary, when a ticket is closed with notes, or by hand. |

## Automation, with approval

| Feature | Where | How |
|---|---|---|
| **One-click actions** | Card -> *More* -> One-click actions; Operations -> Actions | `IBO_ACTIONS_ENABLED=1` **and** the action ticked for the system (Systems page). Every action shows exactly what it will touch, needs a reason and a click, and is logged on the timeline. `IBO_ACTIONS_TWO_PERSON=1` for a second approver. Lock release refuses young locks and users with pending updates. **Try on QAS first**; `restart_job` needs the RFC user to have XBP (S_XMI_PROD) and batch admin authorisations. |
| **Runbooks** | `config/runbooks.yaml`; sidebar -> Operations -> Runbooks; *Runbook* link in mails and on cards | One checklist per alert type / metric; ticks are recorded with name and time. 13 runbooks included -- edit the YAML freely. |

## Running the platform itself

| Feature | Where | How |
|---|---|---|
| **Add systems from the web page** | Systems page (Edit / Add) | Validation before saving (client digits, hosts, e-mails, VPN check syntax, missing `.env` variables), backup to `config/backups`, atomic write, read-back check, *Test connection* button. New fields: customer, network/VPN check, probe, live refresh, Basis/manager mails, allowed actions, notes. |
| **Thresholds from the web page** | Sidebar -> Thresholds | Edits `config/thresholds.yaml` keeping comments and order. |
| **Health page** | Sidebar -> Health | Tunnel (each forward, far end), VPN watchdogs (local and over the tunnel), both dashboards, services, Windows-VM heartbeats, last sweep per system (with parked / maintenance / reachability), mail delivery (SMTP settings, last delivered / failed, queue), on-call, tickets, actions. *Send test mail* button. |
| **Automatic log-on (Windows VM)** | `packaging\windows\enable_autologon.ps1` (elevated) | Uses Sysinternals Autologon (password stored as an LSA secret). `-Lock` locks the screen 2 min after log-on; `-Disable` turns it off. |
| **Ubuntu watches the Windows VM** | automatic | The Windows dashboard posts a heartbeat every minute to each remote source with a token (Ubuntu, through the tunnel). After 5 min of silence Ubuntu mails `[WINDOWS VM SILENT]` to Basis + manager, every 30 min, then `[RECOVERED]`. Set `IBO_PEER_PROBE_<WINVMNAME>=10.1.0.x:3389` on Ubuntu so the mail can say "VM up, dashboard/tunnel down" vs "VM off". |

## Settings added to `.env` (all optional; see `.env.example`)

`IBO_PUBLIC_URL`, `IBO_INTERNAL_DOMAINS`, `IBO_AUTO_PARK`, `IBO_LEARNED_MODE`, `IBO_FORECAST_WARN_DAYS`,
`IBO_FORECAST_HOUR`, `IBO_TICKETS` (+ provider settings), `IBO_TICKET_KINDS`, `IBO_ACTIONS_ENABLED`,
`IBO_ACTIONS_TWO_PERSON`, `IBO_ACTION_LOCK_MIN_AGE_MIN`, `IBO_PEER_PUSH`, `IBO_PEER_SILENT_MINUTES`,
`IBO_PEER_PROBE_<NAME>`, `IBO_CCMS_FILESYSTEMS`.

## Install

Stop the dashboard, unzip over the project folder, start it again. New tables
are created on first use (same `dashboard/ibops.sqlite`). Then:

1. `.env`: set `IBO_PUBLIC_URL` (the address people open the dashboard at) and
   `IBO_INTERNAL_DOMAINS=infrabeat.com`, or the mails come without buttons.
2. `copy config\oncall.yaml.example config\oncall.yaml` and fill in the team.
3. Open **Health** and check every row.

Tests: `pytest tests/test_operations_features.py` (23 tests).
