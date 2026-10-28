"""
Every value the dashboard can grade, in one list.

For each metric key:
    label      what a Basis consultant calls it (shown on the Thresholds page)
    group      section on the Thresholds page
    unit       %, ms, min, count, days, ...
    direction  "high"  -> a higher value is worse (most metrics)
               "low"   -> a lower value is worse (days to expiry, free WPs,
                          uptime, cache hit %); warning is ABOVE critical
               "state" -> not a number: a service that should be running
                          (Dispatcher, ICM, gateway). Graded with `severity`.
    default    (warning, critical) shipped with the program, or None = not
               graded until someone sets a limit. For "state": the severity
               when the service is not running ("critical"/"warning"/"off").
    where      where the value is shown: wall, detail, sweep (Excel/PDF), checks
    stored     the history series it is written to (dashboard history, in the
               SQLite database dashboard/ibops.sqlite, table `lines`, path
               dashboard/history/<SYSTEM>/<YYYY-MM>.jsonl) -- or "" if the
               value is shown but not kept.

Limits are looked up per system by core/thresholds.py:
    config/thresholds_systems.yaml (this system)  ->  config/thresholds.yaml
    (all systems)  ->  `default` here.

This module only DESCRIBES metrics. It reads nothing and grades nothing.
"""
from __future__ import annotations

HIGH, LOW, STATE = "high", "low", "state"

_G_OS = "Host (OS)"
_G_WP = "Work processes (SM50 / SM66)"
_G_RESP = "Response time (SMLG / ST03)"
_G_AVAIL = "Availability"
_G_USERS = "Users"
_G_ERR = "Errors and jobs"
_G_LOCK = "Locks and queues"
_G_SQL = "Expensive SQL (SQLM / ST03)"
_G_DB = "Database (ASE)"
_G_CHK = "Daily checks"
_G_HANA = "HANA"
_G_CUSTOM = "Custom alerts (checked every 5 min)"
_G_GUI = "SAP GUI checks (ST03N, SMICM, ST02, ST04, ST05, STMS, STRUST)"

_HIST = "history"   # written to dashboard history under the same key


def _m(key, label, group, unit, direction=HIGH, default=None, where=("wall",),
       stored=_HIST, tcode="", note=""):
    return key, {"label": label, "group": group, "unit": unit, "direction": direction,
                 "default": default, "where": list(where),
                 "stored": key if stored == _HIST else stored, "tcode": tcode, "note": note}


CATALOGUE: dict[str, dict] = dict([
    # ---------------------------------------------------------------- host
    _m("cpu", "CPU used", _G_OS, "%", default=(80, 90), where=("wall", "detail", "sweep"),
       tcode="ST06 / SMON"),
    _m("memory", "Memory used", _G_OS, "%", default=(80, 90), where=("wall", "detail", "sweep"),
       tcode="ST06 / SMON",
       note="Only graded when the figure excludes reclaimable page cache."),
    _m("swap", "Swap used", _G_OS, "%", default=(20, 40), where=("sweep",), tcode="OS (SSH)"),
    _m("disk", "File system used (fullest mount)", _G_OS, "%", default=(80, 90),
       where=("detail", "sweep"), stored="disk_<mount>", tcode="RZ20 / OS"),
    _m("load_1m", "Load average (1 min)", _G_OS, "", default=(4, 8), where=("detail", "sweep"),
       tcode="OS (SSH)"),

    # ------------------------------------------------------- work processes
    _m("sap.sm50.wp_utilization", "Work processes in use", _G_WP, "%", default=(70, 90),
       where=("wall", "sweep"), stored="sap.sm50.wp_utilization", tcode="SM50",
       note="The wall's work-process bar and the sweep's SM50 check use this one limit."),
    _m("sap.wp.free.DIA", "Free dialog work processes", _G_WP, "count", LOW, None,
       stored="sap.wp.free.DIA", tcode="SM50"),
    _m("sap.wp.free.BGD", "Free background work processes", _G_WP, "count", LOW, None,
       stored="sap.wp.free.BGD", tcode="SM50"),
    _m("sap.wp.free.UPD", "Free update work processes", _G_WP, "count", LOW, None,
       stored="sap.wp.free.UPD", tcode="SM50"),
    _m("sap.wp.free.UP2", "Free update-2 work processes", _G_WP, "count", LOW, None,
       stored="sap.wp.free.UP2", tcode="SM50"),
    _m("sap.wp.free.SPO", "Free spool work processes", _G_WP, "count", LOW, None,
       stored="sap.wp.free.SPO", tcode="SM50"),
    _m("sap.sm50.priv_mode_wp", "Work processes in PRIV mode", _G_WP, "count", default=(2, 5),
       where=("wall", "sweep"), tcode="SM50"),
    _m("sap.sm50.long_running_wp", "Long-running work processes (10 min+)", _G_WP, "count",
       default=(2, 5), where=("wall", "sweep"), tcode="SM50"),
    _m("sap.sm50.longest_running_min", "Longest-running work process", _G_WP, "min",
       default=(60, 90), tcode="SM50"),
    _m("sap.sm66.max_instance_saturation_pct", "Busiest instance: work processes busy", _G_WP, "%",
       default=(80, 90), where=("detail", "sweep"), tcode="SM66"),
    _m("sap.sm66.wp_saturation_pct", "All instances: work processes busy", _G_WP, "%",
       default=(80, 90), where=("detail", "sweep"), tcode="SM66"),

    _m("dialog_queue", "Dialog queue length", _G_WP, "count", default=(5, 20),
       where=("detail",), tcode="SMON"),
    _m("update_queue", "Update queue length", _G_WP, "count", default=(5, 20),
       where=("detail",), tcode="SMON"),

    # ------------------------------------------------------------ response
    _m("sap.smlg.response_time", "Instance response time (5-min STAT average)", _G_RESP, "ms",
       default=(1500, 2000), where=("wall", "sweep"), stored="sap.smlg.response_time",
       tcode="SMLG / STAT",
       note="Per instance. Average of all dialog steps in the last 5 minutes (STAT). "
            "Reaching critical on any instance starts an automatic RCA (once per system "
            "per hour)."),
    _m("sap.st03.dialog_resp_ms", "Dialog response, whole system", _G_RESP, "ms",
       default=(1500, 3000), where=("detail", "sweep"), tcode="ST03N"),
    _m("sap.st03.max_instance_resp_ms", "Dialog response, slowest instance (median)", _G_RESP, "ms",
       default=(1500, 3500), where=("detail", "sweep"), tcode="ST03N"),
    _m("db_rtt_ms", "Database round trip", _G_RESP, "ms", default=(50, 200),
       where=("detail",), tcode="SMON"),
    _m("sap.st03.db_time_pct", "Database share of response time", _G_RESP, "%", default=(50, 70),
       where=("detail", "sweep"), tcode="ST03N"),

    # -------------------------------------------------------- availability
    _m("sap.dispatcher.state", "Dispatcher running", _G_AVAIL, "", STATE, "critical",
       where=("wall", "detail"), tcode="SM50 / sapcontrol"),
    _m("sap.icm.state", "ICM running", _G_AVAIL, "", STATE, "critical",
       where=("wall", "detail"), tcode="SMICM"),
    _m("sap.gateway.state", "Gateway running", _G_AVAIL, "", STATE, "critical",
       where=("wall",), tcode="SMGW"),
    _m("sap.uptime_minutes", "Time since last restart", _G_AVAIL, "min", LOW, (30, None),
       stored="sap.uptime_minutes", tcode="SM51",
       note="Lower is worse: a warning means the system restarted recently."),

    # --------------------------------------------------------------- users
    _m("sap.al08.user_logons", "Users logged on (all instances)", _G_USERS, "count",
       default=(150, 250), where=("wall", "sweep"), tcode="AL08"),
    _m("sap.instance.users", "Users on one instance", _G_USERS, "count", default=None,
       stored="sap.instance.users", tcode="SMLG / SM04",
       note="Per instance; the busiest instance is stored."),

    # ------------------------------------------------------ errors and jobs
    _m("sap.st22.dumps", "ABAP dumps today", _G_ERR, "count", default=(5, 10),
       where=("wall", "sweep"), tcode="ST22"),
    _m("sap.sm37.cancelled_jobs", "Cancelled jobs today", _G_ERR, "count", default=(5, 10),
       where=("wall", "sweep"), tcode="SM37"),
    _m("sap.sm21.errors", "System log errors", _G_ERR, "count", default=(5, 20),
       where=("sweep",), tcode="SM21"),
    _m("sap.sm13.failed_updates", "Failed updates", _G_ERR, "count", default=(1, 10),
       where=("sweep",), tcode="SM13"),
    _m("sap.we02.failed_idocs", "Failed IDocs", _G_ERR, "count", default=(1, 10),
       where=("sweep",), tcode="WE02"),
    _m("sap.sost.errors", "Mails in error (SOST)", _G_ERR, "count", default=(1, 10),
       where=("sweep",), tcode="SOST"),
    _m("sap.su01.locked_users", "Locked users", _G_ERR, "count", default=(5, 20),
       where=("sweep",), tcode="SU01"),

    # ---------------------------------------------------- locks and queues
    _m("sap.sm12.lock_count", "Lock entries", _G_LOCK, "count", default=(500, 2000),
       where=("wall", "sweep"), tcode="SM12"),
    _m("sap.sm12.locks_per_user_max", "Most locks held by one user", _G_LOCK, "count",
       default=(200, 1000), where=("detail", "sweep"), tcode="SM12"),
    _m("sap.sm12.users_with_many_locks", "Users holding many locks", _G_LOCK, "count",
       default=(2, 5), where=("detail", "sweep"), tcode="SM12"),
    _m("sap.sm12.oldest_lock_minutes", "Oldest lock", _G_LOCK, "min", default=(1440, 2880),
       where=("detail", "sweep"), tcode="SM12"),
    _m("sap.smq1.entries", "Outbound queue entries (SMQ1)", _G_LOCK, "count", default=(10, 50),
       where=("wall", "sweep"), tcode="SMQ1",
       note="Any SYSFAIL / CPICERR / STOP entry makes it at least a warning."),
    _m("sap.smq2.entries", "Inbound queue entries (SMQ2)", _G_LOCK, "count", default=(10, 50),
       where=("wall", "sweep"), tcode="SMQ2",
       note="Any SYSFAIL / CPICERR / STOP entry makes it at least a warning."),
    _m("sap.sm58.stuck_entries", "Failed tRFC today (SM58)", _G_LOCK, "count", default=(1, 10),
       where=("wall", "sweep"), tcode="SM58"),
    _m("sap.sm58.entries_today", "SM58 entries displayed (today)", _G_LOCK, "count",
       default=(200, 1000), where=("wall", "sweep"), tcode="SM58",
       note="SM58's 'Number of Entries Displayed' (today, all users). The detail shows failed and in "
            "execution. qRFC entries waiting in SMQ1 queues are not counted, as in SM58."),

    # -------------------------------------------------------- expensive SQL
    _m("sap.sqlm.expensive_programs", "Expensive programs", _G_SQL, "count", default=(3, 10),
       where=("detail", "sweep"), tcode="SQLM"),
    _m("sap.sqlm.top_program_total_s", "Top program SQL time", _G_SQL, "s", default=(300, 1800),
       where=("detail", "sweep"), tcode="SQLM"),
    _m("sap.st03.top_user_memory_mb", "Top user memory", _G_SQL, "MB", default=(4096, 8192),
       where=("detail", "sweep"), tcode="ST03N"),
    _m("sap.st03.top_report_db_ms", "Top report DB time", _G_SQL, "ms", default=(60000, 300000),
       where=("detail", "sweep"), tcode="ST03N"),

    # -------------------------------------------------------------- database
    _m("db.ase.active_statements", "ASE active statements", _G_DB, "count", default=(40, 80),
       where=("sweep",), tcode="DBACOCKPIT"),
    _m("db.ase.long_running_statements", "ASE long-running statements", _G_DB, "count",
       default=(1, 5), where=("sweep",), tcode="DBACOCKPIT"),
    _m("db.ase.blocked_sessions", "ASE blocked sessions", _G_DB, "count", default=(1, 5),
       where=("sweep",), tcode="DBACOCKPIT"),
    _m("db.ase.expensive_statement_count", "ASE expensive statements", _G_DB, "count",
       default=(5, 20), where=("sweep",), tcode="DBACOCKPIT"),
    _m("db.ase.top_statement_avg_lio", "ASE top statement logical I/O", _G_DB, "",
       default=(100000, 1000000), where=("sweep",), tcode="DBACOCKPIT"),
    _m("db.ase.data_cache_hit_pct", "ASE data cache hit ratio", _G_DB, "%", LOW, (95, 90),
       where=("sweep",), tcode="DBACOCKPIT"),
    _m("db.ase.engine_cpu_pct", "ASE engine CPU", _G_DB, "%", default=(80, 90),
       where=("sweep",), tcode="DBACOCKPIT"),
    _m("db.ase.memory_pool_max_used_pct", "ASE memory pool used", _G_DB, "%", default=(85, 95),
       where=("sweep",), tcode="DBACOCKPIT"),

    # ---------------------------------------------------------- daily checks
    # default None: graded by the check's own rule (config/checks.yaml) until
    # a limit is set here.
    _m("sap.strust.cert_days_left", "Certificate expires in", _G_CHK, "days", LOW, None,
       where=("wall", "checks"), tcode="STRUST",
       note="Without a limit here: checks.yaml cert_warning / cert_critical (30 / 7)."),
    _m("sap.slicense.days_left", "SAP licence expires in", _G_CHK, "days", LOW, None,
       where=("wall", "checks"), tcode="SLICENSE",
       note="Without a limit here: checks.yaml licence_warning / licence_critical (30 / 7)."),
    _m("sap.sm37.missed_jobs", "Scheduled jobs that did not run", _G_CHK, "count", HIGH, None,
       where=("wall", "checks"), tcode="SM37"),
    _m("sap.sm36.housekeeping_missing", "Missing housekeeping jobs", _G_CHK, "count", HIGH, None,
       where=("wall", "checks"), tcode="SM36"),
    _m("sap.sm37.overdue_jobs", "Overdue jobs", _G_CHK, "count", HIGH, None,
       where=("wall", "checks"), tcode="SM37"),
    _m("sap.su01.user_issues", "User validity issues", _G_CHK, "count", HIGH, None,
       where=("wall", "checks"), tcode="SU01"),
    _m("sap.sm59.failing_destinations", "Failing RFC destinations", _G_CHK, "count", HIGH, None,
       where=("wall", "checks"), tcode="SM59"),
    _m("sap.sp01.spool_requests", "Spool requests", _G_CHK, "count", HIGH, None,
       where=("wall", "checks"), tcode="SP01"),
    _m("sap.stms.failed_imports", "Failed transport imports", _G_CHK, "count", HIGH, None,
       where=("wall", "checks"), tcode="STMS"),

    # ----------------------------------------------------------------- HANA
    _m("hana.license_days_left", "HANA licence expires in", _G_HANA, "days", LOW, None,
       where=("wall", "checks"), tcode="HANA"),
    _m("hana.memory_used_pct", "HANA memory used", _G_HANA, "%", HIGH, None,
       where=("wall", "checks"), tcode="HANA", note="Without a limit here: 85 / 95."),
    _m("hana.alerts_high", "HANA high alerts", _G_HANA, "count", HIGH, None,
       where=("wall", "checks"), tcode="HANA"),
    _m("hana.replication", "HANA replication problems", _G_HANA, "count", HIGH, None,
       where=("wall", "checks"), tcode="HANA"),
    # ------------------------------------------- custom alerts (core/custom_alerts.py)
    _m("sap.sm66.bgd_saturation_pct", "Background work processes busy (all instances)", _G_CUSTOM, "%",
       default=(70, 80), where=("checks",), tcode="SM66",
       note="Busy background work processes of all instances together."),
    _m("sap.st22.user_dumps_10min", "ABAP dumps by one user (15 min)", _G_CUSTOM, "count",
       default=(11, 11), where=("checks",), tcode="ST22",
       note="Per user, over the time window set on this row (default 15 minutes): more than 10 alerts. Memory, database and RFC dumps go to Basis; program dumps to ABAP, "
            "and to FI / SD / MM (and Basis) when the dump's component is theirs."),
    _m("sap.sm37.user_cancelled_15min", "Cancelled jobs by one user (15 min)", _G_CUSTOM, "count",
       default=(10, 15), where=("checks",), tcode="SM37",
       note="Per user who scheduled the jobs. Basis gets an 'Approve: mail the user' button."),
    _m("sap.sm37.longest_active_job_min", "Longest-running background job", _G_CUSTOM, "min",
       default=(60, 90), where=("checks",), tcode="SM37", note="Per job (SM37 'active')."),
    _m("sap.sm66.longest_wp_min", "Longest-running work process (all instances)", _G_CUSTOM, "min",
       default=(60, 90), where=("checks",), tcode="SM66",
       note="Any work process type, busy on one request."),
    _m("sap.sm12.user_old_locks", "Locks held 15 min+ by one user", _G_CUSTOM, "count",
       default=(50, 100), where=("checks",), tcode="SM12", note="Per user."),
    _m("sap.sm12.lock_held_min", "Longest-held lock", _G_CUSTOM, "min",
       default=(15, 30), where=("checks",), tcode="SM12"),
    _m("sap.sm12.users_same_table_15min", "Users locking the same table (15 min)", _G_CUSTOM, "count",
       default=(3, 5), where=("checks",), tcode="SM12", note="Per table: different users, locks set in 15 min."),
    _m("sap.sm58.failed_today", "Failed tRFC today (SYSFAIL + CPICERR)", _G_CUSTOM, "count",
       default=(11, 25), where=("checks",), tcode="SM58"),
    _m("sap.smq2.waiting_10min", "Inbound queue entries waiting 10 min+", _G_CUSTOM, "count",
       default=(1, 5), where=("checks",), tcode="SMQ2"),
    _m("sap.smq2.error_10min", "Inbound queue entries in error 10 min+", _G_CUSTOM, "count",
       default=(1, 5), where=("checks",), tcode="SMQ2", note="SYSFAIL, CPICERR, STOP, RETRY ..."),
    _m("hana.last_backup_hours", "Hours since last HANA backup", _G_HANA, "h", HIGH, None,
       where=("wall", "checks"), tcode="HANA", note="Without a limit here: 26 / 50."),
    # ------------------------------- SAP GUI checks (sap_gui/tcode_actions_extra.py, core/gui_checks.py)
    _m("sap.st03n.dialog_avg_ms", "ST03N dialog average response time", _G_GUI, "ms", HIGH, (1000, 2000),
       where=("sweep",), tcode="ST03N", note="Today so far, all instances (Profile page: period)."),
    _m("sap.st03n.background_avg_ms", "ST03N background average response time", _G_GUI, "ms", HIGH, None,
       where=("sweep",), tcode="ST03N", note="Shown, not graded until a limit is set here."),
    _m("sap.st03n.wsrfc_avg_ms", "ST03N WS-RFC average response time", _G_GUI, "ms", HIGH, None,
       where=("sweep",), tcode="ST03N", note="Shown, not graded until a limit is set here."),
    _m("sap.smicm.inactive_services", "ICM services not active (SMICM)", _G_GUI, "count", HIGH, (1, 1),
       where=("sweep",), tcode="SMICM", note="Any service not active is CRITICAL."),
    _m("sap.st02.min_hit_ratio", "ST02 lowest buffer hit ratio", _G_GUI, "%", LOW, (98, 95),
       where=("sweep",), tcode="ST02",
       note="Of the buffers ticked on the Profile page (default Program, CUA, Screen, Tables, Nametab)."),
    _m("sap.st02.swaps_per_day", "ST02 buffer swaps per day (worst buffer)", _G_GUI, "count", HIGH, (1000, 10000),
       where=("sweep",), tcode="ST02", note="Swaps since the last reading, scaled to a day."),
    _m("sap.st04.memory_used_pct", "HANA memory used / allocation limit (ST04)", _G_GUI, "%", HIGH, (80, 90),
       where=("sweep",), tcode="ST04"),
    _m("sap.st04.tenant_memory_pct", "HANA tenant memory used (ST04)", _G_GUI, "%", HIGH, (80, 90),
       where=("sweep",), tcode="ST04"),
    _m("sap.st04.data_volume_pct", "HANA data volume used (ST04)", _G_GUI, "%", HIGH, (80, 90),
       where=("sweep",), tcode="ST04"),
    _m("sap.st04.log_volume_pct", "HANA log volume used (ST04)", _G_GUI, "%", HIGH, (80, 90),
       where=("sweep",), tcode="ST04"),
    _m("sap.st05.traces_on", "ST05 traces switched on", _G_GUI, "count", HIGH, (1, None),
       where=("sweep",), tcode="ST05", note="A trace left on slows the system: WARNING."),
    _m("sap.stms.unavailable_systems", "Transport systems not available (STMS)", _G_GUI, "count", HIGH, (1, 1),
       where=("sweep",), tcode="STMS", note="Status not Active, or SAP's connection test failed."),
    _m("sap.strust.min_days_left", "Own certificate expires in (STRUST)", _G_GUI, "days", LOW, (30, 7),
       where=("sweep",), tcode="STRUST", note="The own certificate of every PSE; the soonest counts."),
])

# Two names for one figure: the SAP GUI sweep reads ST22 as
# sap.st22.dump_count, the RFC read and the wall as sap.st22.dumps. Only the
# first-named key is listed; saving it also saves the alias.
ALIASES = {"sap.st22.dumps": "sap.st22.dump_count"}

# Keys that once existed in code but that no reader produces any more. Not
# listed on the Thresholds page unless config/thresholds.yaml still has them.
DEAD_KEYS = {"sap.sm58.stuck_trfc", "sap.smq1.stuck_queues", "sap.smq2.stuck_queues",
             "sap.slg1.app_log_errors"}

GROUP_ORDER = [_G_OS, _G_WP, _G_RESP, _G_AVAIL, _G_USERS, _G_ERR, _G_LOCK, _G_SQL, _G_DB,
               _G_CHK, _G_HANA, _G_CUSTOM, _G_GUI]

STATE_SEVERITIES = ("critical", "warning", "off")


def entry(key: str) -> dict | None:
    return CATALOGUE.get(key)


def direction(key: str) -> str:
    e = CATALOGUE.get(key)
    return e["direction"] if e else HIGH


def default_limits(key: str) -> tuple:
    e = CATALOGUE.get(key)
    d = e.get("default") if e else None
    if isinstance(d, (tuple, list)) and len(d) == 2:
        return tuple(d)
    return (None, None)


def default_severity(key: str) -> str:
    e = CATALOGUE.get(key)
    d = e.get("default") if e else None
    return d if isinstance(d, str) and d in STATE_SEVERITIES else "critical"
