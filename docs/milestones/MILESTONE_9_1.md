# InfraBeatOps — Milestone 9.1

## Memory-dump ownership: rule, attribution ladder, team routing

`SAP_DUMP_SPIKE` said "memory-class dump → Basis." That is wrong whenever a
customer report held the memory. This milestone decides the owner from who
held it, and mails that team.

### Added

- `core/dump_attribution.py` — the deterministic ladder. First match wins:
  1. one customer program ≥ 50% of the memory dumps → **ABAP**, `single_hog`
  2. customer report in PRIV, or topping per-step memory above 1 GB, with
     dumps landing on other programs → **ABAP**, `hog_with_collateral`
  3. dumps over ≥ 3 programs, no customer hog → **BASIS**, `system_starvation`
  4. standard code dominates → **FUNCTIONAL**, `standard_code`
  5. otherwise → **TRIAGE**, `unclear`
- Rule `SAP_MEMORY_DUMP_ATTRIBUTION` in `config/correlation_rules.yaml`,
  matcher registered in `core/correlation.py`. Fires alongside
  `SAP_DUMP_SPIKE`. First evidence line is machine-readable:
  `owner_team=ABAP verdict=single_hog confidence=0.90 culprit=ZMM_REPORT ...`
- `notifications/dump_owner.py` — `job_owner.py`'s sibling. Opt-in
  (`NOTIFY_DUMP_OWNERS=true`), team mailboxes from
  `DUMP_TEAM_EMAIL_ABAP` / `_FUNCTIONAL` / `_BASIS`, Basis always copied,
  once per culprit per day, TRIAGE goes to Basis flagged unattributed.
  Hooked in `main.py` after job-owner notification.

### Changed

- `SAP_DUMP_SPIKE` `confirm_in`: the memory line now defers to the
  attribution rule instead of asserting Basis.

### Inputs (all confirmed live on QAS/PRD)

| Signal | Metric |
|---|---|
| dump class, program, user | `screenshot_ST22.extra_data["dumps"]` (GUI collector) |
| who is in PRIV now | `sap.sm50.priv_mode_wp.extra_data["priv"]` |
| per-step memory by report | `sap.st03.dialog_resp_ms.extra_data["top_reports_by_memory"]` |
| per-step memory by user | `sap.st03.top_user_memory_mb.extra_data` |

The RFC-only dump path (`sap.st22.dumps`) carries no class, so the rule
cannot fire without a GUI ST22 sweep. That is by design: a verdict without
a class is a guess.

### Validation

`tests/test_dump_attribution.py`: **20 passed** — every rung, the rule
through the engine, `unmatched_rules()` clean, notifier routing with SMTP
faked.
