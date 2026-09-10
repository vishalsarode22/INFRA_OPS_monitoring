# Retiring the SM69 external commands

The function module no longer needs section 9 (the SXPG block), because
CPU, memory and load now come from `/SDF/SMON_HEADER` — a plain table read
via `RFC_READ_TABLE`.

## Why this is worth doing

`SXPG_COMMAND_EXECUTE` requires the authorisation object `S_LOG_COM`, which
grants **remote command execution on the application server**. It was by far
the most sensitive grant in `Z_OBS_MONITOR_RFC`; everything else is read-only.

Removing it means the monitoring account can no longer run OS commands at
all — a meaningful reduction in what a compromised monitoring credential
could do.

SMON also provides more than the commands did: dialog and update queue
lengths, session counts, and database round-trip time, none of which are
readable from `/proc`.

## Steps

**1. SE37 → `Z_GET_OBSERVABILITY_DATA` → Change → Source code**

Delete the whole of section 9 — from the comment line

```abap
* --- 9. OS METRICS VIA RFC (SXPG -- no SSH, no database) ----------------
```

down to (but not including)

```abap
* --- 10. LAST SUCCESSFUL DB BACKUP --------------------------------------
```

**2. Remove the now-unused declarations**

```abap
        lt_exec       TYPE TABLE OF btcxpm,
        ls_exec       TYPE btcxpm,
        lv_status     TYPE extcmdexex-status,
        lv_kb         TYPE p LENGTH 16,
        lv_avail_kb   TYPE p LENGTH 16,
```

Keep `lt_token`, `lv_line`, `lv_tok` and `lv_dec` — the response-time block
still uses them.

**3. Leave the Export parameters in place**

`EV_CPU_UTIL_PCT`, `EV_MEM_UTIL_PCT`, `EV_TOTAL_RAM_GB` and `EV_LOAD_1M` can
stay on the Export tab. They will return their initial value and the Python
side no longer reads them. Removing them would change the RFC signature,
which is a needless breaking change for any other caller.

Set them explicitly to -1 near the top so they never read as a healthy zero:

```abap
  EV_CPU_UTIL_PCT = -1.
  EV_MEM_UTIL_PCT = -1.
  EV_TOTAL_RAM_GB = -1.
  EV_LOAD_1M      = -1.
```

**4. Pretty Printer → Check → Activate → F8**

Everything except the four OS values should be unchanged.

**5. PFCG → `Z_OBS_MONITOR_RFC` → remove `S_LOG_COM`**

Then regenerate the profile and run User Comparison.

**6. SM69 → delete `ZOBS_LOADAVG`, `ZOBS_MEMINFO`, `ZOBS_VMSTAT`**

Optional but tidy — they are no longer called by anything.

## Prerequisite on every system

Schedule SMON in transaction `/SDF/SMON`. A 60-second interval is what TST
uses and is ample.

Without it, CPU, memory and load show "No data" on the dashboard. That is
the honest state, and the fix is scheduling SMON rather than re-granting
command execution.

## What still needs the function module

Everything else: the T-code counters, lock entries, dumps, jobs, queues,
IDocs, locked users, work processes and dialog response time. The FM remains
the primary collector — only its OS section is retired.
