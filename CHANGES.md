# Changes -- 30 Sep 2026 review fixes

Numbers refer to the review list.

**Done in code**
- (rate limit) `dashboard/rate_limit.py`: per-IP sliding window on every `/api` route, 100/hour default, JSON 429 with a clear message and `Retry-After`; UI shows it as a banner. See the note in RUN.md about exempting the sidebar polls.
- (secrets) `add_system()` and the setup wizard now write `${VAR}` for hosts, usernames, saprouter, SMTP and alert addresses, not just passwords; new values are visible to the running process immediately. `scripts/migrate_systems_to_env.py` converts an existing `systems.yaml`. Complete `.env.example`.
- 3: a sweep requested while one is running is now logged, counted in `/api/run-status` (`skipped`, `skipped_total`) and written to run history instead of vanishing.
- 4: `core/escalation.py` -- yellow (first breach, consultants) / red (repeat, + manager). Persisted in `data/escalation_state.json`. New `ALERT_MANAGER_EMAILS` and per-system `manager_receivers`.
- 5: WhatsApp sends approved **template** messages when `WHATSAPP_TEMPLATE_NAME` is set (required outside the 24-hour window); the 131047 error is explained in the log.
- 6: `evaluation/ai_privacy.py` wraps Gemini/Grok/failover: usernames and e-mails become `USER_nn` before the request and are restored in the reply.
- 8: ABAP: work processes summed over all app servers (`TH_SERVER_LIST` + `TH_WPINFO srvname`), locks over all clients, `USR02` counts only the 128 "wrong password" bit, IDoc 51 looks back 7 days. **Not compiled here -- activate in TST first.**
- 9: after 3 logon failures in a row a system is frozen until its credentials change (`IBO_RFC_AUTH_MAX_FAILURES`).
- 10: SSH host keys pinned (`utils/ssh_hostkeys.py`); a changed key is refused.
- 13: scratch `check_*.py`, `apply_milestone_*`, `.bak*`, the stray patch and "New Text Document.txt" moved to `scripts/dev/` or deleted; `check_autoabaptime.py` syntax fixed.
- 14: runtime state directories gitignored; `data/` for new state.
- 15: `.gitattributes` (`* text=auto`, `export-ignore`).
- 16: every JSON state write is now atomic (`utils/atomic.py`).
- 17: `${VAR}` falls back to the OS credential store via `keyring`; `scripts/set_secret.py`.
- 18: real README; the milestone note moved to `docs/`.
- Removed `config/templates/MetroBrands_template.xlsx_files/` (a saved Excel Online page containing Microsoft access tokens).

**Needs you (cannot be done from the code)**
- 1: rotate the passwords that were in the shared zip; share code only via `git archive`.
- 2: `git filter-repo` the history and move the remote to an Infrabeat-owned org (commands in the chat reply).
- 3: per-client VMs -- one GUI desktop cannot serve 16 landscapes hourly.
- 7: moving the 19 `RFC_READ_TABLE` reads into the Z function module (ABAP work, needs TST).
- 10: a real login before exposing the port.
- 11/12: splitting the 6k-line modules and auditing 466 `except` blocks is multi-day refactoring; every collector failure already returns UNKNOWN on the RFC path -- audit the GUI path next.

Then run: `git rm -r --cached config/systems.yaml` and commit.
