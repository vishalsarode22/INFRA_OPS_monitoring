"""
Plain-language explanations for the Alert history page.

The raw history rows say things like kind="repeat", metric="sap.st22.dumps",
recipients="a@x, b@x". This turns each row into:

    category   Value alert / System reachability / Monitoring report / ...
    headline   "ABAP dumps today is CRITICAL: 3900"
    why        "Still above the limit 60 min after the last mail, so a reminder
                went out. A repeat goes to Basis, the Basis manager in copy."
    people     [{"address": ..., "role": "Manager"}, {"role": "Basis"}, ...]
    outcome    "Delivered by email" / "Not sent: daily limit reached ..."

and builds the "Who gets what" card for one system from the same settings
the senders use (core/recipients.py, core/alert_engine.py, core/reachability.py),
so the page cannot describe rules the code does not follow.
"""
from __future__ import annotations

import os

ROLE_ORDER = ("Manager", "Basis", "Client", "Infrabeat", "Other")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _split(text) -> list[str]:
    import re
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        return [str(a).strip() for a in text if "@" in str(a)]
    return [a.strip() for a in re.split(r"[,;\s]+", str(text)) if "@" in a]


def roles(system_cfg: dict) -> dict:
    """Who is who for one system (lists of addresses). Never raises."""
    out = {"basis": [], "manager": [], "client": [], "report_to": [], "report_cc": [], "rca": []}
    cfg = system_cfg or {}
    try:
        from core import alert_engine
        out["basis"] = list(alert_engine.recipients(cfg)["email"]["to"])
    except Exception:  # noqa: BLE001
        pass
    try:
        from core import recipients as R
        out["manager"] = list(R.manager_internal(cfg))
        out["client"] = list(R.client(cfg))
        out["rca"] = list(R.basis_internal(cfg))
        if not out["basis"]:
            out["basis"] = list(R.basis_internal(cfg))
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.config_loader import get_smtp_config
        smtp = get_smtp_config(cfg, require_recipients=False) or {}
        out["report_to"] = _split(smtp.get("to_emails"))
        cc = _split(smtp.get("cc_emails"))
        out["report_cc"] = [a for a in cc if a.lower() not in {t.lower() for t in out["report_to"]}]
    except Exception:  # noqa: BLE001
        pass
    return out


def _internal_domains(r: dict) -> set[str]:
    """IBO_INTERNAL_DOMAINS, else the domains of the Basis and manager addresses."""
    raw = os.getenv("IBO_INTERNAL_DOMAINS") or ""
    doms = {d.strip().lower().lstrip("@") for d in raw.replace(";", ",").split(",") if d.strip()}
    if not doms:
        doms = {a.split("@", 1)[1].lower() for a in (r.get("basis") or []) + (r.get("manager") or [])
                if "@" in a}
    return doms


def role_of(address: str, r: dict) -> str:
    a = (address or "").strip().lower()
    if a in {x.lower() for x in r.get("manager") or []}:
        return "Manager"
    if a in {x.lower() for x in r.get("basis") or []} or a in {x.lower() for x in r.get("rca") or []}:
        return "Basis"
    if a in {x.lower() for x in r.get("client") or []}:
        return "Client"
    if "@" in a and a.split("@", 1)[1] in _internal_domains(r):
        return "Infrabeat"
    if a in {x.lower() for x in (r.get("report_to") or []) + (r.get("report_cc") or [])}:
        return "Client"
    return "Other"


def people(recipients_text: str, r: dict) -> list[dict]:
    seen, out = set(), []
    for a in _split(recipients_text):
        if a.lower() in seen:
            continue
        seen.add(a.lower())
        out.append({"address": a, "role": role_of(a, r)})
    out.sort(key=lambda p: ROLE_ORDER.index(p["role"]))
    return out


# ------------------------------------------------------------------ one row
_VALUE_KINDS = {"alert", "repeat", "call", "recovery", "test", "user_mail"}

_EVENT = {
    # metric (ledger kind) -> (category, headline, why)
    "not_reachable": ("System reachability", "System NOT REACHABLE",
                      "IBOPS could not reach {system} over RFC {down} checks in a row (VPN, tunnel or "
                      "the SAP system itself). Treated as DOWN: the Basis manager and Basis are told, "
                      "again every 5 min while it stays down."),
    "system_down": ("System reachability", "SYSTEM DOWN",
                    "{system} did not answer {down} checks in a row. The Basis manager and Basis are "
                    "told, again every 5 min while it stays down."),
    "recovered": ("System reachability", "Reachable again",
                  "IBOPS reached {system} again. This closes the earlier 'not reachable / down' mail "
                  "and goes to the same people."),
    "vpn_down": ("System reachability", "VPN not connected",
                 "The VPN to {system}'s network is down, so IBOPS cannot read it. Basis only "
                 "(the manager is not woken for our own access problem)."),
    "credentials": ("System reachability", "SAP refused the logon",
                    "{system} answered but refused IBOPS's RFC user (wrong password or locked user). "
                    "Basis only. Fix the password in .env, then clear the logon block."),
    "collector_down": ("System reachability", "Tunnel / collector problem",
                       "The SSH tunnel or collector machine IBOPS uses for {system} is not working. "
                       "Basis only."),
    "rca": ("Performance analysis", "Performance analysis (RCA)",
            "A performance problem triggered an automatic analysis of {system}; the PDF with the "
            "likely causes went to Basis. Never to the client."),
    "diagnostic": ("Performance analysis", "Diagnostic analysis",
                   "An automatic diagnostic for {system}; internal only."),
    "report": ("Monitoring report", "Monitoring report (PDF + Excel)",
               "The scheduled monitoring sheet for {system}: to the report list (the client) with "
               "the Cc list. Sent only when the SAP GUI screenshots were captured."),
    "final_report": ("Monitoring report", "Monitoring report (PDF + Excel)",
                     "The scheduled monitoring sheet for {system}: to the report list (the client) "
                     "with the Cc list."),
    "report_sent": ("Monitoring report", "Monitoring report sent", "The monitoring sheet for {system}."),
    "report_held": ("Monitoring report", "Report held back",
                    "The monitoring run for {system} was incomplete, so the sheet was NOT sent to the client."),
    "critical": ("Value alert", "Critical values after a sweep",
                 "A monitoring sweep of {system} found CRITICAL values; summary to Basis."),
    "tcode_failure": ("Sweep problem", "T-code capture failed",
                      "A sweep of {system} could not capture one or more T-code screens."),
    "sweep_failure": ("Sweep problem", "Sweep failed", "A monitoring sweep of {system} failed."),
    "monitor_down": ("Sweep problem", "Monitoring stopped", "IBOPS itself stopped monitoring {system}."),
    "job_owner": ("Owner mail", "Mail to a job's owner",
                  "A background job failed or ran long; the user who scheduled it was told (Basis in copy)."),
    "dump_owner": ("Owner mail", "Mail to the team behind a dump spike",
                   "Many ABAP dumps from one source; the owning team was told (Basis in copy)."),
}

_OUTCOME = {
    "suppressed-cap": "Not sent: a mail limit was reached",
    "suppressed": "Not sent",
    "duplicate": "Not sent: the same mail went out a moment ago",
    "held": "Held: the system is acknowledged, snoozed or in maintenance",
}


def _fmt_limits(limits: str) -> str:
    s = str(limits or "").strip()
    return s.replace(">=", "≥")


def explain(row: dict, r: dict) -> dict:
    system = row.get("system") or ""
    metric = str(row.get("metric") or "")
    kind = str(row.get("kind") or "")
    label = row.get("label") or metric
    level = str(row.get("level") or "").upper()
    value = row.get("value")
    note = str(row.get("note") or "")
    manager_told = "manager informed" in note.lower()
    down = _env_int("IBO_DOWN_CONSECUTIVE_FAILURES", 2)

    if kind in _VALUE_KINDS:
        category = "Value alert"
        inst = f" ({row.get('instance')})" if row.get("instance") else ""
        lim = _fmt_limits(row.get("limits"))
        limit_txt = f" Limits: {lim}." if lim else ""
        if kind == "alert":
            headline = f"{label}{inst}: {level} ({value})"
            if manager_told:
                why = (f"It crossed its limit again within {_env_int('IBO_ALERT_ESCALATE_WINDOW_MIN', 120)} min "
                       f"of the last time, so the Basis manager was told too (in copy).{limit_txt}")
            else:
                why = f"First time over its {level.lower()} limit, so the Basis team was told.{limit_txt}"
        elif kind == "repeat":
            mins = _env_int("IBO_ALERT_REPEAT_CRITICAL_MIN", 60) if level == "CRITICAL" \
                else _env_int("IBO_ALERT_REPEAT_WARNING_MIN", 480)
            every = f"{mins} min" if mins < 120 else f"{mins // 60} h"
            headline = f"Reminder: {label}{inst} still {level} ({value})"
            why = (f"Still over the limit {every} after the last mail, so a reminder went out"
                   + (": reminders go to Basis, the Basis manager in copy." if manager_told else ".")
                   + f" It repeats every {every} until the value is back to normal.{limit_txt}")
        elif kind == "recovery":
            headline = f"Resolved: {label}{inst} back to normal ({value})"
            why = "Back within its limits. Closes the earlier alert; sent to the same people."
            if str(row.get("channel") or "") == "none":
                why = ("Back within its limits. No mail: the problem lasted less than "
                       f"{_env_int('IBO_ALERT_RECOVERY_MIN_MINUTES', 30)} min, so nobody was told.")
        elif kind == "call":
            headline = f"Phone call: {label}{inst} CRITICAL for 15 min"
            why = "CRITICAL for 15 minutes without anyone acknowledging it, so a call was placed."
        elif kind == "user_mail":
            headline = f"Mail to SAP user about cancelled jobs ({row.get('instance') or ''})"
            why = "Approved on this page: the user whose jobs were cancelled was told."
        else:
            headline = f"Test message ({label})"
            why = "Sent with the Test button on the Thresholds page."
    else:
        category, headline, why = _EVENT.get(metric, ("Other", label or metric, ""))
        why = why.format(system=system, down=down)
        if metric in ("not_reachable", "system_down") and not r.get("manager"):
            for a, b in (("the Basis manager and Basis are told", "Basis is told"),
                         ("The Basis manager and Basis are told", "Basis is told")):
                why = why.replace(a, b + " (no manager address is set for this system)")
        if metric == "rca" and value:
            headline = f"Performance analysis (RCA): {str(value).split('--', 1)[-1].strip()[:110]}"

    ok = bool(row.get("ok"))
    if ok:
        outcome = f"Delivered by {row.get('channel') or 'email'}"
    elif str(row.get("channel") or "") == "none":
        outcome = "No mail needed"
    else:
        key = kind if kind in _OUTCOME else ("held" if note.startswith("held") else "")
        from core import alert_ledger as _al
        base = _OUTCOME.get(key, "Not sent").format(cap=_al.cap_per_system(str(row.get("system") or "") or None),
                                                     total=_al.cap_total())
        detail = note if note and note.strip().lower() != key else ""
        outcome = base + (f" ({detail})" if detail and detail.lower() not in base.lower() else "")
    return {"category": category, "headline": headline, "why": why,
            "people": people(row.get("recipients") or "", r), "outcome": outcome,
            "group_key": f"{system}|{metric}|{row.get('instance') or ''}|{level}|"
                         f"{'value' if kind in ('alert', 'repeat') else kind}"}


# ------------------------------------------------------------ who gets what
def _caps_text(system_cfg) -> str:
    from core import alert_ledger as al
    return (f"At most {al.cap_per_system((system_cfg or {}).get('name'))} everyday mails a day for this system "
            f"and {al.cap_total()} in total. CRITICAL, DOWN, VPN and logon mails are not held by these "
            f"(only a flood guard of {al.cap_critical_per_system()} a day per system).")


def routing(system_cfg: dict, r: dict | None = None) -> dict:
    """The rules for one system, in words, with the addresses each one reaches."""
    r = r or roles(system_cfg)
    from core import alert_timing as _t
    cfg_t = system_cfg or {}
    crit = _t.get(cfg_t, "repeat_critical_min")
    warn = _t.get(cfg_t, "repeat_warning_min")
    window = _t.get(cfg_t, "escalate_window_min")
    raise_after = _t.get(cfg_t, "raise_after")
    down = _t.get(cfg_t, "down_after")
    recovery_min = _t.get(cfg_t, "recovery_min_minutes")
    rca_cap = _t.get(cfg_t, "rca_daily_cap")
    own_down = _t.system_value(cfg_t, "down_repeat_min")
    own_access = _t.system_value(cfg_t, "access_repeat_min")
    down_every = own_down or (_t.env_value("down_repeat_min") if os.getenv("IBO_DOWN_REPEAT_MIN") else 5)
    access_every = (f"every {own_access} min" if own_access else
                    f"every {_t.env_value('access_repeat_min')} min" if os.getenv("IBO_ACCESS_REPEAT_MIN")
                    else "every 30–60 min")
    special = {k: r.get(k) or [] for k in ("recovery", "down", "access")}

    def hours(m):
        return f"{m // 60} h" if m >= 60 and m % 60 == 0 else f"{m} min"
    mgr_or_basis = r["manager"] or r["basis"]
    rules = [
        {"when": "A value crosses its limit for the first time",
         "detail": f"after {raise_after} readings in a row over the limit (Thresholds page decides which values alert)",
         "to": ["Basis"], "cc": [], "how_often": "once"},
        {"when": "The same value is still bad (reminder)",
         "detail": f"CRITICAL: every {hours(crit)} · WARNING: every {hours(warn)}, until it is back to normal",
         "to": ["Basis"], "cc": ["Manager"], "how_often": f"every {hours(crit)} / {hours(warn)}"},
        {"when": "A value goes bad again soon after it recovered",
         "detail": f"within {window} min of the last alert",
         "to": ["Basis"], "cc": ["Manager"], "how_often": "each time"},
        {"when": "A value is back to normal",
         "detail": f"only if the problem lasted {recovery_min} min or more",
         "to": ["Back to normal list"] if special["recovery"] else ["same people as the alert"], "cc": [],
         "how_often": "once"},
        {"when": "System NOT REACHABLE / DOWN",
         "detail": f"after {down} failed checks in a row",
         "to": ["System down list"] if special["down"] else ["Manager", "Basis"], "cc": [],
         "how_often": f"every {down_every} min while down, then 'Reachable again'"},
        {"when": "VPN down, tunnel down, or SAP refuses IBOPS's logon",
         "detail": "our own access problem, not the client's system",
         "to": ["Access problem list"] if special["access"] else ["Basis"], "cc": [],
         "how_often": f"{access_every} while it lasts"},
        {"when": "Performance analysis (RCA)",
         "detail": f"automatic, at most {rca_cap} a day per system" if rca_cap else "automatic, no daily cap",
         "to": ["Basis"], "cc": [], "how_often": "when triggered"},
        {"when": "Scheduled monitoring report (PDF + Excel)",
         "detail": "only when the SAP GUI screenshots were captured",
         "to": ["Report To list"], "cc": ["Report Cc list"], "how_often": "at each scheduled run"},
    ]
    return {
        "system": (system_cfg or {}).get("name"),
        "lists": [
            {"role": "Basis", "addresses": r["basis"],
             "meaning": "Basis consultants: first alerts, access problems, RCA"},
            {"role": "Manager", "addresses": r["manager"],
             "meaning": "Basis manager: reminders, repeat breaches, system down"
                        + ("" if r["manager"] else " -- NOT SET, so these go to Basis instead")},
            {"role": "Report To list", "addresses": r["report_to"],
             "meaning": "who receives the monitoring report (normally the client)"},
            {"role": "Report Cc list", "addresses": r["report_cc"], "meaning": "copied on the report"},
        ],
        "client_never": "The client never gets alerts, reminders, down mails or RCAs -- only the report.",
        "rules": rules,
        "caps": _caps_text(system_cfg),
        "where_to_change": [
            "Systems page → Edit the system → 'Basis e-mails for this system' and 'Manager e-mails' "
            "(empty = the on-call rotation / ALERT_MANAGER_EMAILS in .env)",
            "Systems page → Edit → 'Recipients' and 'CC recipients' = who gets the monitoring report",
            "Thresholds & alerts page = which values send alerts, and their limits",
        ],
        "_manager_fallback": not r["manager"] and bool(mgr_or_basis),
    }


# ------------------------------------------------------------ setup preview
def _resolved(value) -> tuple[list[str], str]:
    """(addresses, note) for a form value that may hold ${VAR}."""
    raw = str(value or "").strip()
    if not raw:
        return [], ""
    import re
    names = re.findall(r"\$\{([A-Za-z0-9_]+)\}", raw)
    try:
        from core.config_loader import _resolve_env_value
        val = _resolve_env_value(raw)
    except Exception:  # noqa: BLE001
        val = raw
    addrs = _split(val)
    if names:
        missing = [n for n in names if not os.getenv(n)]
        if missing:
            return addrs, f"{', '.join(missing)} is not set in .env"
        return addrs, f"from .env {', '.join(names)}"
    return addrs, ""


def _env_list(*names) -> tuple[list[str], str]:
    for n in names:
        got = _split(os.getenv(n))
        if got:
            return got, f".env {n}"
    return [], ""


def preview(system_cfg: dict | None, form: dict | None = None) -> dict:
    """
    Who gets which mail for one system, with WHERE each list comes from, for
    the Systems form (form = the values typed but not saved yet) and the
    Profiles page. The same resolution order the senders use.
    """
    cfg = dict(system_cfg or {})
    form = form or {}
    name = str(form.get("name") or cfg.get("name") or "")
    for k in ("basis_emails", "manager_receivers", "alert_receivers", "alert_receivers_cc",
              "recovery_emails", "down_emails", "access_emails", "alert_timing"):
        if k in form:
            cfg[k] = form[k]
    warnings: list[str] = []
    info: list[str] = []

    def entry(addresses, source, field=""):
        return {"addresses": list(addresses), "source": source or "not set", "field": field}

    # ---- Basis: system's own alert team > this form > standard alert team > on-call > .env
    basis = None
    try:
        from core import alert_teams
        team = list(alert_teams.own_addresses(name, alert_teams.BASIS) or []) if name else []
        std_team = list(alert_teams.load()["standard"].get(alert_teams.BASIS) or [])
    except Exception:  # noqa: BLE001
        team, std_team = [], []
    own, note = _resolved(cfg.get("basis_emails"))
    if team:
        basis = entry(team, "Alert teams page (this system's Basis team) — this replaces the Basis field",
                      "basis_emails")
        if own:
            warnings.append("The 'Basis team' field is ignored: the Alert teams page sets a Basis team "
                            "for this system, and that list wins.")
    elif own:
        basis = entry(own, "this form" + (f" ({note})" if note else ""), "basis_emails")
    elif std_team:
        basis = entry(std_team, "Alert teams page (standard Basis team, all systems; field empty)",
                      "basis_emails")
    else:
        try:
            from core import oncall
            oc = _split(oncall.basis_emails())
        except Exception:  # noqa: BLE001
            oc = []
        if oc:
            basis = entry(oc, "on-call rotation (field empty)", "basis_emails")
        else:
            got, src = _env_list("IBO_BASIS_EMAILS", "ALERT_TO_EMAILS")
            basis = entry(got, (src + " — field empty") if src else "", "basis_emails")

    # ---- Manager: form (with ${VAR}) > .env
    raw_mgr = str(cfg.get("manager_receivers") or cfg.get("manager_emails") or "").strip()
    own, note = _resolved(raw_mgr)
    if own:
        manager = entry(own, "this form" + (f" ({note})" if note else ""), "manager_receivers")
    else:
        got, src = _env_list("ALERT_MANAGER_EMAILS", "IBO_MANAGER_EMAILS")
        why = f"because {note}" if note and "not set" in note else "field empty"
        manager = entry(got, f"{src} — {why}" if src else "", "manager_receivers")
        if note and "not set" in note:
            if got:
                info.append(f"The manager field uses {note.replace(' is not set in .env', '')}, which is not set "
                            f"in .env — so {src.replace('.env ', '')} from .env is used instead.")
            else:
                warnings.append(f"The manager field uses {note} — and there is no ALERT_MANAGER_EMAILS either, "
                                "so this system has NO manager. Type the address(es) in the field.")
    if not manager["addresses"]:
        warnings.append("No Basis manager: reminders and SYSTEM DOWN mails go to the Basis team only.")

    # ---- Report: form > .env (profiles may override per profile)
    own, note = _resolved(cfg.get("alert_receivers"))
    if own:
        rto = entry(own, "this form" + (f" ({note})" if note else ""), "alert_receivers")
    else:
        got, src = _env_list("ALERT_TO_EMAILS")
        rto = entry(got, (src + " — field empty") if src else "", "alert_receivers")
    own, note = _resolved(cfg.get("alert_receivers_cc"))
    if own:
        rcc = entry(own, "this form" + (f" ({note})" if note else ""), "alert_receivers_cc")
    else:
        got, src = _env_list("ALERT_CC_EMAILS")
        rcc = entry(got, (src + " — field empty") if src else "", "alert_receivers_cc")

    from core import recipients as _rcp
    specials = {}
    for which, (field, env) in _rcp.SPECIAL.items():
        own, note = _resolved(cfg.get(field))
        if own:
            specials[which] = entry(own, "this form" + (f" ({note})" if note else ""), field)
        else:
            got, src = _env_list(env)
            specials[which] = entry(got, (src + " — field empty") if src else "the normal rule (field empty)", field)
    r = {"basis": basis["addresses"], "manager": manager["addresses"], "client": [],
         "report_to": rto["addresses"], "report_cc": rcc["addresses"], "rca": basis["addresses"],
         "recovery": specials["recovery"]["addresses"], "down": specials["down"]["addresses"],
         "access": specials["access"]["addresses"]}
    doms = _internal_domains(r)
    if doms:
        for lst, label in ((basis["addresses"], "Basis team"), (manager["addresses"], "Basis manager")):
            ext = [a for a in lst if "@" in a and a.split("@", 1)[1].lower() not in doms]
            if ext:
                warnings.append(f"{', '.join(ext)} in '{label}' is not an internal address "
                                f"({', '.join(sorted(doms))}) but would get internal alerts.")
        if rto["addresses"] and all(a.split("@", 1)[-1].lower() in doms for a in rto["addresses"]):
            info.append("The report goes only to internal addresses — no client address is in 'Report To'.")
    if not rto["addresses"]:
        warnings.append("Nobody is set to receive the monitoring report (Report To is empty here and in .env).")

    # ---- Profiles that send this system's report to their own lists
    profiles = []
    try:
        from core.profiles import load_profiles
        try:
            from core.profiles import describe as _desc
        except Exception:  # noqa: BLE001
            _desc = None
        for p in load_profiles():
            if p.get("system") != name:
                continue
            sends = "email" in (p.get("deliver") or [])
            profiles.append({
                "id": p.get("id"), "label": p.get("label") or p.get("id"),
                "schedule": (_desc(p.get("schedule") or {}) if _desc else ""),
                "enabled": bool((p.get("schedule") or {}).get("enabled")),
                "sends_email": sends,
                "to": list(p.get("email_to") or []) or rto["addresses"],
                "to_source": "the profile's own list" if p.get("email_to") else "the system's Report To",
                "cc": list(p.get("email_cc") or []) or rcc["addresses"],
                "cc_source": "the profile's own list" if p.get("email_cc") else "the system's Report Cc",
            })
    except Exception:  # noqa: BLE001
        pass
    if any(p["to_source"] == "the profile's own list" for p in profiles):
        info.append("A profile has its own report list: its scheduled report goes there instead of "
                    "'Report To' (see the Profiles page).")

    rules = routing(cfg | {"name": name}, r)["rules"]
    from core import alert_timing as _t
    timing = _t.describe(cfg)
    return {"system": name, "basis": basis, "manager": manager, "report_to": rto, "report_cc": rcc,
            "recovery": specials["recovery"], "down": specials["down"], "access": specials["access"],
            "timing": timing,
            "profiles": profiles, "rules": rules, "warnings": warnings, "info": info,
            "client_never": "The client never gets alerts, reminders, down mails or RCAs — only the report."}
