# InfraBeatOps — Milestone 9.0

## ASE / Sybase database collector (ST04 over MDA tables)

Added `collectors/ase_collector.py`: reads the SAP ASE monitoring tables
directly with a read-only `mon_role` login. This is the same data
DBACOCKPIT shows, without an RFC hop or a transport.

### Metrics emitted (all `tcode=ST04`, `category=database`, `source=ase_collector`)

| Metric | Source | Note |
|---|---|---|
| `db.ase.active_statements` | `monProcessStatement` + `monProcessSQLText` + `monProcess` | Carries the SAP work process PID (`sysprocesses.hostprocess`) in `extra_data` for the join to `TH_WPINFO.WP_PID` |
| `db.ase.long_running_statements` | same, elapsed ≥ 60 s | |
| `db.ase.blocked_sessions` | `monProcess.BlockingSPID > 0` | Detail names the blocking SPID |
| `db.ase.expensive_statement_count` | `monCachedStatement`, `AvgLIO ≥ 50 000` | Top statements and text in `extra_data` |
| `db.ase.top_statement_avg_lio` | `monCachedStatement` | |
| `db.ase.data_cache_hit_pct` | `monDataCache` | Delta between polls; cumulative and labelled on first poll. **Low is bad.** |
| `db.ase.engine_cpu_pct` | `monEngine` | Delta between polls; deliberately absent on the first poll |
| `db.ase.memory_pool_max_used_pct` | `sp_monitorconfig 'all'` | Hottest pool by `Pct_act`; all pools in `extra_data` |

### Wiring

- `core/orchestrator.py`: runs after the RFC collector when the system has a `db:` block. Failure → `result.errors`, never a healthy zero.
- `config/systems.yaml`: optional `db:` block; `${VAR}` placeholders resolve through the existing one-level resolver. See `systems.yaml.example`.
- `config/thresholds.yaml`: `db.ase.*` entries added.
- `requirements.txt`: `pyodbc`.

### ASE prerequisites (Basis)

```sql
sp_configure 'enable monitoring', 1
sp_configure 'statement statistics active', 1
sp_configure 'per object statistics active', 1
sp_configure 'wait event timing', 1
sp_configure 'SQL batch capture', 1
sp_configure 'max SQL text monitored', 4096

-- dedicated read-only login
sp_addlogin 'ibo_monitor', '<password>'
sp_role 'grant', mon_role, ibo_monitor
```

`statement statistics active` and `SQL batch capture` cost a few percent CPU
on PRD. Enable them on TST first and watch `sp_sysmon` for a day.

### Attribution caveat

Every SAP work process connects to ASE as `SAPSR3`, so the DB login identifies
nothing. The collector emits `wp_pid` from `sysprocesses.hostprocess`; the RFC
layer's `TH_WPINFO.WP_PID` is where that becomes an SAP user and report. That
join is the next milestone, not this one.

### Validation

`tests/test_ase_collector.py`: **11 passed** (fakes only, no database).
Live validation against TST pending an ODBC driver on the monitoring host.
