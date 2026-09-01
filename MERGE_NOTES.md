# InfraBeatOps — merged build

Two developers built SAP monitoring in different directions. This folder is
the union: the intelligence platform from one, the RFC collector from the
other, with the InfraBeat palette applied.

## What each side brought

**Base — `SAP_BASIS_MONITOR`** (kept as the foundation)

FastAPI, and the whole layer the InfraBeatOps spec describes:

| Area | Modules |
|---|---|
| Events & incidents | `core/event_engine.py`, `event_store.py`, `events.py`, `incidents.py`, `incident_store.py`, `incident_actions.py` |
| Correlation | `core/correlation.py`, config-driven via `config/correlation_rules.yaml` |
| Baselines & anomaly | `core/baseline.py`, `baseline_integration.py`, `change_detector.py`, `trend_engine.py` |
| AI RCA | `evaluation/ai_schemas.py`, `ai_sanitizer.py`, `ai_context.py`, `providers/gemini.py` |
| Evidence | `reporting/evidence.py` — evidence IDs, lineage, execution history |
| Collection | SAP GUI scripting + OCR (`sap_gui/`, 4.8k lines), SSH (`collectors/`) |
| Packaging | `packaging/` — installer, systemd unit, Windows launchers |

**Merged in — the RFC branch**

That codebase had no RFC path at all. This is the gap it filled:

| File | What |
|---|---|
| `collectors/rfc_collector.py` | **New.** pyrfc collection, ported to this project's `MetricResult` model |
| `abap/Z_GET_OBSERVABILITY_DATA.abap` | Corrected function module — ~20 counters in one round trip |
| `core/orchestrator.py` | RFC step added to the cycle |
| `core/config_loader.py` | Nested `${VAR}` resolution for the `rfc:` block |
| `tests/test_rfc_collector.py` | **New.** 16 tests, no live SAP system needed |

## Why RFC matters here

GUI scripting needs a Windows host with SAP GUI and a foreground session.
SSH needs OS access. RISE and hosted systems grant neither. RFC needs only a
service account and a network route, so it is the only path that works on
those systems — and it is far cheaper than driving the GUI for counters.

A system can now use any combination of the three. See
`config/systems.yaml.example`.

## The rule that governs all of it

**A failed read is UNKNOWN, never healthy.**

The RFC branch shipped with several violations of this, all fixed here and
covered by tests:

- The ABAP module overwrote four real measurements with test constants.
- Every dashboard card returned a hardcoded `0` tagged `RFC-LIVE`.
- Five exports were declared and never assigned, returning 0 forever.
- `server_availability_pct` was hardcoded to 100.
- An unreachable system produced no cards, which the scorer read as 100/100.

Concretely, in `rfc_collector.py`:

- `-1` from ABAP means "could not read" → `Status.UNKNOWN`, `value=None`.
- `0` means unavailable **only** for CPU, memory and RAM — a live host is
  never at 0% CPU with 0 GB.
- `0` for load average is a **real reading**. An idle system reports 0.00,
  and hiding it would discard a correct measurement.
- A missing export produces **no metric**, not a zero.

## Operational fixes carried over

**One connection per cycle.** The old code opened an RFC logon per card —
~25 per system per minute. That fills the security audit log and risks
locking the monitoring account.

**Failure cooldown.** A dead system is parked (5 min; 15 min after an auth
failure, because retrying a wrong password is how accounts get locked).
Without it, a `WSAETIMEDOUT` host blocks every request for ~21s on Windows.

**Route strings preserved.** A complete SAProuter route ends in `/H/` so the
library can append the target host. Stripping it produced
`NiPGetHostByName: H/<target> not found`. Routes may also carry a router
password in a `/W/<secret>/` segment, so they must never be reformatted.

## Palette

`dashboard/static/index.html` is fully CSS-variable driven, so the brand
applies at the token level.

Dark is default (`#08111C` / `#0D1B2A` / `#0066B3` / `#00A8E8`); light is
`html[data-theme="light"]`. A second, competing `html[data-theme="light"]`
block already existed — the winner depended on source order — and has been
retired.

Two deliberate choices:

- **Status colours are not brand colours.** If the alert red matches the
  logo red, operators stop seeing it as an alarm.
- **Light-theme status colours are darkened** (`#15803D`, `#B45309`,
  `#B91C1C`). The dark-theme greens and ambers fail WCAG AA on white.

New `.src-*` badges show collector provenance — RFC cyan, SSH green, GUI
purple, NO DATA grey — because "SAP is unhealthy" and "our collector failed"
are different statements.

## Setup

```bash
pip install -r requirements.txt        # pyrfc needs the NetWeaver RFC SDK
cp .env.example .env                   # fill in
cp config/systems.yaml.example config/systems.yaml
python -m uvicorn dashboard.app:app --reload
```

Without pyrfc the RFC collector degrades quietly and GUI/SSH still work.

## Credentials — do this first

No `.env` or `config/systems.yaml` ships here. Both existed in the source
archives with live values (35 populated keys in one `.env` alone), and both
were committed to git in the other project.

Treat every credential in those archives as compromised: rotate the SAP,
SSH root, SMTP and database passwords, then purge them from git history with
`git filter-repo`. Adding them to `.gitignore` now does not remove them from
history.

Also worth fixing while you are there: one system was being monitored as
`DDIC`, and an SSH collector was connecting as `root`. Use a dedicated
service account with `S_RFC` scoped to the observability function group.

## Not done

Honest list.

- The FastAPI routes still expose GUI/SSH data. RFC metrics flow through
  `MonitoringResult` into events, correlation and incidents, but no endpoint
  yet reports RFC-specific health.
- Response time (ST03 workload) has no collector. It needs the
  `SAPWL_WORKLOAD_*` interface and the `SAP_COLLECTOR_FOR_PERFMONITOR` job
  scheduled — check SM37 first; if that job is absent, ST03 is empty.
- Last backup returns N/A on Sybase. `SDBAH` is only populated when backups
  run through the DBA Planning Calendar. Check SE16 → `SDBAH`.
- SMLG / server availability has no honest source. `TH_SERVER_LIST` reports
  which servers are *registered*, not which are responding.
- The 43-section spec's Incident Story, Evidence Explorer, Runbooks, RBAC
  and Audit Trail screens are unbuilt. The backend for incidents,
  correlation and evidence exists; the UI does not.

## Excel template reporting

`reporting/infrabeatops_template_writer.py` fills the InfraBeatOps Advanced
SAP Monitoring workbook from a `MonitoringResult`.

`config/templates/InfraBeatOps_Advanced_SAP_Monitoring_Template.xlsx` is
copied per run, never edited in place — overwriting it would destroy the
Dashboard sheet's COUNTIF formulas on the first write.

**The workbook is the threshold authority.** Its Configuration sheet is keyed
by the same canonical metric names the collectors emit, so `load_thresholds()`
reads it and `grade()` applies it. That collapses what were three competing
sources of truth (`config/thresholds.yaml`, the `_THRESHOLDS` dict in
`rfc_collector.py`, and this sheet) into one an operator can edit in Excel.
It also honours the `Direction` column: `LOWER` means a smaller number is
worse, which is how availability percentages must be graded.

**Blank means blank.** A check with no collector is written as
`NOT COLLECTED`, greyed, with the value cell left empty — never 0, never
HEALTHY. Of the 38 checklist rows, 9 currently fill and 29 do not. A report
that showed 38 green would be a lie in a format people archive and forward.

**AI attribution is cycle-level, and labelled as such.** `AIAnalysis` is
produced once per cycle, so writing it against each actionable metric
attributes a root cause to findings it never examined — the first run pasted
an SMLG response-time hypothesis onto an unrelated disk-space warning. The
sheet now gets one row naming every finding it covers, and the checklist
back-fill is tagged `[HYPOTHESIS · cycle-level]`. Per-metric attribution
needs per-metric analysis, which the AI layer does not yet produce.
`finding_status` is written verbatim so a hypothesis is never displayed as a
confirmed cause, and Analyst Validation is left blank for a human to sign.

Known gap: `sap.st22.dump_count` in the Configuration sheet vs
`sap.st22.dumps` from the collector. Both are accepted by `CHECK_METRICS`,
but the threshold lookup only matches the latter — worth settling on one.
