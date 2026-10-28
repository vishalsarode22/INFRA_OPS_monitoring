# InfraBeatOps — round 2 features (October 2026)

This package is cumulative: it contains round 1 (operations features) and
round 2. Unzip it over the existing install on each VM. Nothing in SAP is
changed by installing it, and every new feature that sends SMS, calls,
WhatsApp messages or logs on to SAP is **off until you configure it**.

---

## 1. What is new

| Area | What it does | Where you see it |
|---|---|---|
| **Real-time exception handling** | Every unhandled error is caught: web requests, sweeps, background loops, job queue, threads, and JavaScript errors in the browser. Errors are grouped by type, so 500 repeats are one line with a count. A **new** error type is mailed to Basis once (then at most every 6 h, max 20/day). Crashed background loops restart on their own (10 s, 30 s, 1 min, 2 min, 5 min). A web page that fails shows an error ID instead of a stack trace. | **Errors** page (left menu, red badge), Health page rows "errors" and "loops" |
| **Extra SAP checks** | Certificate expiry, SAP licence expiry, expected jobs that did not finish by their time, SAP housekeeping jobs, jobs running far past their normal runtime, watched users locked / expiring, failing RFC destinations, spool number range filling, failed transport imports, HANA (licence, memory, alerts, replication, last backup). Run every 30 min in a separate worker process; WARNING / CRITICAL is mailed once per problem per 12 h. A check a system cannot answer shows **UNKNOWN with the reason** — never a guessed value. | Wall checklist, mails, `/api/ops/checks/<SYS>` |
| **Escalation by time** | A SYSTEM DOWN nobody acknowledges: 15 min → mail to manager, 30 min → SMS, 45 min → phone call (MSG91). Acknowledge / Snooze / Ignore / maintenance / recovery stops it. A failed SMS is retried after 5, 15, 60 min, then given up and noted. | Operations → On call → Escalation |
| **What changed before an incident** | Every 6 h: kernel patch, support packages (CVERS), profile parameter changes (PAHI), transports imported (TPALOG). Changes go on the timeline and into alert mails and tickets. | Operations → Changes, Timeline |
| **Noise analytics + availability** | Which alerts fire most and get snoozed most (with a tip), and uptime % / outages per system. | Operations → Noise & availability |
| **Post-incident report** | One click: sequence of events, time to acknowledge, changes before, fix. HTML, PDF, and a customer version without names. | Timeline → Incident report / PDF / Customer version |
| **Ask the platform** | "What happened on PS4 last night?" — answered only from what IBOPS recorded. Without AI it lists what mattered first; with `IBO_ENABLE_AI=1` the AI writes the answer from the same facts. | Operations → Ask |
| **Weekly digest** | Monday 08:00 mail: availability, alerts, noise, changes, capacity. | Mail; preview at Operations |
| **Business calendar** | Month-end, year-end and holidays are compared with other month-ends / holidays, so busy-by-design days do not raise "unusual" flags. | `config/business_calendar.yaml` |
| **Job runtime prediction** | A running job is flagged when it runs much longer than its own normal (from 30 days of history). | Check "job running long" |
| **Synthetic transaction** | Every 10 min: a fresh logon + two reads, timed — what a user feels. Off by default (every run is a logon in the security audit log). | Operations, learned baselines |
| **Cross-system correlation** | Two or more systems on the same network / subnet / DB host / customer in trouble at once → one [CORRELATED] mail. | Mail, Timeline |
| **More one-click actions** | Unlock a user (protected users never: SAP*, DDIC, TMSADM by default) and cancel a running job. Shown first, logged, always a person's click. | Operations → Actions |
| **Approval policies** | Per system/action: one person confirms, or a second person must approve. Admins can add a time-boxed standing approval for planned work. `IBO_ACTIONS_TWO_PERSON=1` forces two-person for everything. | `config/action_policy.yaml`, Operations → Actions |
| **Housekeeping** | SAP_REORG_* and the performance collector job are checked. | Check "housekeeping jobs" |
| **Failover** | The Ubuntu VM can watch a system *only while* the Windows VM is silent (`standby_for:`). Hand-over and hand-back are mailed and on the timeline. Screenshots cannot fail over. | Wall (grey STANDBY card), Health → failover |
| **WhatsApp alerts** | SYSTEM DOWN, VPN down, credentials, critical… also go to WhatsApp — even when SMTP is down. Max one message per alert per 30 min. | WhatsApp |
| **Microsoft sign-in (Entra ID)** | "Sign in with Microsoft" on the login page. Local accounts keep working. | Login page |
| **Configuration as code** | Export all configuration as one zip (secrets removed), import on another VM with a dry run first. | Systems page (admin) |
| **Mobile view** | Bottom navigation and one-column layout on phones. | Any page on a phone |

---

## 2. Install

### Windows VM — run in **PowerShell** (normal window, as Girish_BasisTestVM)

```powershell
$app = "C:\Users\Girish_BasisTestVM\ibops\infrabeatops"
$zip = "$env:USERPROFILE\Downloads\ibops_round2_windows.zip"
# 1. backup of the code and config (logs, screenshots, reports and .venv left out)
$bk = "$app-backup-$(Get-Date -Format yyyyMMdd-HHmm)"
robocopy $app $bk /E /XD logs screenshots reports recordings .venv __pycache__ /NFL /NDL /NJH /NJS | Out-Null
# 2. unzip over the install
Expand-Archive -Path $zip -DestinationPath $app -Force
# 3. restart ONLY the dashboard (the supervisor starts it again in ~10 s;
#    SAP GUI sessions are not touched)
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn\s+dashboard\.app' } |
  ForEach-Object { taskkill.exe /F /T /PID $_.ProcessId }
Start-Sleep 20
Invoke-WebRequest http://127.0.0.1:8000/healthz -UseBasicParsing | Select-Object StatusCode
```

### Ubuntu VM — run in the **Ubuntu terminal** (SSH session as your normal user)

```bash
APP=~/app
cd $APP && tar czf ~/ibops-backup-$(date +%Y%m%d-%H%M).tgz core collectors dashboard/*.py dashboard/static notifications services config main.py .env
unzip -o ~/ibops_round2_ubuntu.zip -d $APP
sudo systemctl restart ibops-dashboard
sleep 15 && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/healthz
```

### Optional Python packages (same windows as above)

* HANA checks need `hdbcli`:
  Windows: `& "$app\.venv\Scripts\python.exe" -m pip install hdbcli`
  Ubuntu: `~/ibops/.venv/bin/pip install hdbcli`
* `cryptography` gives nicer certificate details (works without it).

### Run the tests on the VM (optional)

Windows: `cd $app; & .\.venv\Scripts\python.exe -m pytest tests/test_round2_features.py tests/test_operations_features.py -q`
Ubuntu: `cd ~/app && ~/ibops/.venv/bin/python -m pytest tests/test_round2_features.py tests/test_operations_features.py -q`

---

## 3. After installing — what works at once

Exception handling, the Errors page, the extra checks, change tracking,
noise / availability, incident reports, Ask, the digest, correlation,
mobile view and config export work with no setup.

**First thing to do:** see which checks each system can answer.

Windows (PowerShell): `cd $app; & .\.venv\Scripts\python.exe scripts\probe_checks.py PS4`
Ubuntu: `cd ~/app && ~/ibops/.venv/bin/python scripts/probe_checks.py SARLOHAPRD`

It is read-only. Send me the output if a check shows UNKNOWN and you
expected a value.

---

## 4. Settings you choose (all optional)

### 4.1 Per system — `config/systems.yaml`

```yaml
- name: PS4
  ...
  checks:
    certificates: ["ps4host.infrabeat.com:44300"]     # default: the ICM HTTPS port 443<sysnr>
    expected_jobs:
      - {job: Z_DAILY_BILLING, by: "06:00", days: mon-fri, severity: critical}
    watch_users: [ALEREMOTE, WF-BATCH]                 # the RFC user is always watched
    rfc_destinations: [BWPCLNT100]
    spool_max: 32000                                   # else read from rspo/spool_id/max_number
  hana: {host: 10.1.0.60, port: 30015, user: MONITOR, password: "${PS4_HANA_PASSWORD}"}
  actions: [restart_job, release_lock, unlock_user, cancel_job]
  synthetic: true            # this system only
  standby_for: <Windows VM name>   # Ubuntu VM only: watch PS4 only while that VM is silent
                                   # (the name shown for it on the Health page, peers)
```

Always put times in quotes (`"06:00"`).

### 4.2 Escalation SMS / call — MSG91

1. In MSG91: create an SMS **Flow** using a **DLT-approved template**
   (India rule), e.g. `IBOPS: ##VAR1## is ##VAR2## for ##VAR3## min, not acknowledged.`
2. `.env`:
   ```
   IBO_MSG91_AUTHKEY=<auth key>
   IBO_MSG91_FLOW_ID=<flow id>
   IBO_MSG91_SENDER=IBOPS
   IBO_MSG91_SMS_VARS=VAR1=system,VAR2=state,VAR3=minutes
   ```
3. Phones: `phone:` for each person in `config/oncall.yaml`, managers in
   `config/escalation.yaml` → `manager_phones`. 10-digit Indian numbers are fine.
4. Copy `config/escalation.yaml.example` to `config/escalation.yaml`.
5. Operations → On call → **Send test SMS**.

Voice calls: MSG91 voice is set up per account (the API is not public), so it
is configured by URL: `IBO_MSG91_VOICE_URL` and `IBO_MSG91_VOICE_BODY` (from
MSG91 support / your MSG91 panel). Until then the call step is skipped and
noted on the timeline once.

### 4.3 WhatsApp alerts

Uses the existing WhatsApp settings plus an approved **UTILITY** template
with three body parameters, e.g.:

```
IBOPS {{1}}
{{2}}
Acknowledge: {{3}}
```

`.env`: `WHATSAPP_ENABLED=true`, `WHATSAPP_ALERT_TEMPLATE=<template name>`,
optional `WHATSAPP_ALERT_RECIPIENTS=98xxxxxxxx` (else whoever is on call).
`IBO_PUBLIC_URL` must be set for the Acknowledge link.

### 4.4 Sign in with Microsoft (needs a tenant admin)

Sam has no Azure admin rights, so **someone with rights to register apps**
must do steps 1–4 once:

1. Entra admin center → App registrations → **New registration** —
   name "InfraBeatOps dashboard", "Accounts in this organizational directory only",
   Redirect URI (Web): `<IBO_PUBLIC_URL>/auth/callback`.
2. Certificates & secrets → New client secret → copy the **Value**.
3. Token configuration → Add optional claim → ID → `email` (and `groups` if
   you map Entra groups to roles).
4. Send us: Directory (tenant) ID, Application (client) ID, the secret value.

Then in `.env`:
```
IBO_ENTRA_TENANT_ID=<Directory (tenant) ID>     # "common"/"organizations" are refused
IBO_ENTRA_CLIENT_ID=<Application (client) ID>
IBO_ENTRA_CLIENT_SECRET=<secret value>
```
and copy `config/sso.yaml.example` to `config/sso.yaml` (who gets admin /
operator / viewer). A person who already has a local account keeps its
password and role.

### 4.5 Other files to copy from `.example` when wanted

`config/action_policy.yaml`, `config/business_calendar.yaml`
(fill in your holidays), `config/checks.yaml` (thresholds in days).

---

## 5. Not done, and why

* **SM21 system log and SM12 lock entries** were already monitored by the
  screenshot sweep; unchanged.
* **Lock-table fill % and number-range buffer** — no remote-enabled SAP
  function returns them reliably on every release; would need a small Z
  function module. Ask if you want it.
* **Cancel work process, delete spool, ICM reset, IDoc restart** as one-click
  actions — the function modules for these could not be verified as safe
  and remote-enabled on your releases. Unlock user and cancel job were added
  instead (both verified via standard BAPIs and checked on the system before
  they are offered).
* **SAP licence expiry** — SAP has no documented remote reader on every
  release; the check tries the known ones and says UNKNOWN otherwise.
* **Voice calls** need your MSG91 voice settings (4.2).

---

## 6. Rollback

Windows: `robocopy <backup folder> $app /E`, then restart the dashboard as in section 2.
Ubuntu: `tar xzf ~/ibops-backup-<date>.tgz -C ~/app && sudo systemctl restart ibops-dashboard`.

New database tables are additive; the old version ignores them.

---

## 7. Settings page (added 7 Oct 2026)

Left menu → **Settings** (also a button on the Health page). Admins can change things; other users only see them.

* **Features** — switch on/off: e-mail, DOWN/VPN alerts, extra checks, check mails, error mails, weekly digest,
  synthetic logon test, WhatsApp, job/dump owner mails, AI analysis, AI RCA, one-click actions, two-person approval.
  Applied at once (no restart), written to `.env` (other lines untouched, daily backup `.env.bak-YYYYMMDD`).
* **Who gets the alerts** — Basis team, Basis manager, report recipients, CC. Per-system addresses and the on-call
  rota still come first, as before.
* **Client VPN** — Connect / Disconnect for the Sarloha VPN (runs on the Ubuntu VM; the Windows page forwards the
  click). While it is off on purpose its systems show **VPN OFF**, are not swept and send no VPN/DOWN mails.
  If the VPN account is in use by someone else, Connect is refused (never pushes them off).
* Every change is on the Timeline (IBOPS) with who did it and old → new value.
* Each dashboard has its own settings (Windows and Ubuntu separately).

**One-time step on Ubuntu** so the dashboard may switch the VPN (Ubuntu terminal):

```bash
cd ~/app && sudo bash packaging/ubuntu/allow_vpn_control.sh pune_ttml
```

It allows the dashboard user exactly `systemctl start/stop ibops-vpn@pune_ttml` and `ibops-vpn reset pune_ttml` —
nothing else. Undo: `sudo rm /etc/sudoers.d/ibops-vpn-control`.
