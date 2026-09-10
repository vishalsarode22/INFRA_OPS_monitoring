# InfraBeatOps — running it

## Install

```powershell
pip install -r requirements.txt
```

`pyrfc` needs the SAP NetWeaver RFC SDK on the machine. Without it the RFC
collector degrades quietly and the GUI/SSH paths still work — but the live
wall will show every system as unreachable, so check it:

```powershell
python -c "import pyrfc; print('pyrfc ok')"
```

## Configure

```powershell
copy env.txt      .env
copy systems.yaml config\systems.yaml
python -c "from core.config_loader import get_systems; from collectors.rfc_collector import build_rfc_params; [print(s['name'],'OK' if build_rfc_params(s) else 'FAIL') for s in get_systems()]"
```

Expect four systems, all OK.

## Run

```powershell
python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8000
```

**Do NOT use `--reload`.** It was in an earlier version of this file and is
the wrong flag for this deployment. `--reload` makes uvicorn's StatReload
poll the ENTIRE install tree for file changes continuously. This tree is
~165 MB across 700+ files (logs/, reports/, recordings/), and on Windows
with Defender scanning those folders the constant file-stat storm competes
with request handling and makes every page feel slow -- the RFC layer can be
perfectly healthy and the UI still lags because the web server itself is
busy stat-ing thousands of files. `--reload` is a code-editing convenience;
in normal operation it only costs you speed.

If you are actively editing code and want auto-restart, scope the watch so
it does not poll the data directories:

```powershell
python -m uvicorn dashboard.app:app --reload `
  --reload-dir dashboard --reload-dir collectors --reload-dir core `
  --reload-include "*.py"
```

| Page | What it answers |
|---|---|
| `/wall` | **Live RFC.** CPU, memory, work processes in use vs total, dispatcher, ICM, instances, sessions. Refreshes every 5–30s. This is the NOC monitor. |
| `/overview` | Fleet status at a glance, from the last sweep |
| `/systems-page` | Registry, collection paths per system, sweep trigger |
| `/system?name=TST` | One system: live tab, all metrics, health report |
| `/reports-page` | Findings, recommended solutions, health summary |
| `/` | The original dashboard, unchanged |

## The two-machine split

**Dashboard host** — serves the UI and does live RFC reads. Cheap, read-only,
safe to poll. Needs network to the SAP application servers and nothing else.

**Monitoring host** — runs the sweep: SAP GUI scripting, screenshots, OCR,
evidence, reports, AI. Needs Windows with SAP GUI and a live desktop session.
This is the machine that must not be locked or RDP-disconnected, because GUI
scripting drives a real desktop.

They share `config/systems.yaml` and the snapshot directory. The dashboard
reads what the monitoring host writes; it never triggers a sweep itself,
because a sweep relaunches SAP Logon and would kill an operator's GUI session.

To run the sweep on its own schedule on the monitoring machine:

```powershell
python main.py
```

## Live vs collected — what the badges mean

| Badge | Source | Freshness |
|---|---|---|
| `RFC LIVE` | direct read from the application server | seconds |
| `SSH` | read on the host by the sweep | last sweep |
| `SAP GUI` | screenshot/OCR evidence from the sweep | last sweep |
| `stale` | as above, but older than 15 minutes | shown with age |
| `no data` | not collected | state unknown |

The distinction is the point. "SAP is unhealthy" and "our collector failed"
are different statements, and a tile that cannot tell them apart is worse
than no tile.

## Response time

Per-instance dialog response time (what SMLG shows) is **not** reachable over
RFC on this release: it lives in ST03 workload statistics, which need the
`SAP_COLLECTOR_FOR_PERFMONITOR` job and the `SAPWL_*` function modules.

Rather than invent a number, the wall shows the value the SAP GUI collector
captured during the last sweep, labelled with its age. To make it live,
confirm that job is scheduled in SM37 and send me the `SAPWL_WORKLOAD_*`
interface; then it becomes an RFC read like the rest.

## AI

`evaluation/health_report.py` produces findings, solutions and a summary.

Findings and severities are **always deterministic** — rules over collected
metrics. The LLM is optional and only rewrites the summary paragraph; it
cannot invent a finding, change a severity, or add a recommendation. With
`USE_MOCK_AI=true` the report is still complete, with a rule-written summary.

That ordering is deliberate: a model asked to "analyse this system" will
produce confident root causes for readings it has no evidence about, and an
operator cannot tell which is which.
