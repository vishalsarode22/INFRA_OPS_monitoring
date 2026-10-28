"""
Teams that receive alerts, per system (config/alert_teams.yaml), edited on
the Thresholds & alerts page.

    standard:                 teams and addresses for every system
      Basis: [basis@x.com]
      ABAP:  [abap@x.com]
      FI: []  SD: []  MM: []
    systems:                  a system's own teams; a team listed here with
      CEAT S4P:               addresses replaces the standard one for that
        ABAP: [ceat-abap@x]   system, and a system can have extra teams (PP, HR)

Who gets a team's alert:
    the system's own addresses for the team, else the standard ones,
    else Basis (a team without addresses is never a lost alert),
    and Basis without any addresses = the system's normal alert receivers.

Dumps choose their teams by the error (classify_dump): memory, database and
RFC errors go to Basis; program errors go to ABAP, and when the dump's
application component starts with a team's name (FI-GL -> FI) also to that
team and to Basis.
"""
from __future__ import annotations

import os
import re

from utils.logger import get_logger

log = get_logger(__name__)

BASIS, ABAP = "Basis", "ABAP"
DEFAULT_TEAMS = (BASIS, ABAP, "FI", "SD", "MM")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 _/&.-]{0,29}$")
_MAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

# Dumps that are Basis's to look at, by runtime error name.
_BASIS_EXACT = {
    "TSV_TNEW_PAGE_ALLOC_FAILED", "TSV_TNEW_BLOCKS_NO_ROLL_MEMORY", "TSV_TNEW_OCCURS_NO_ROLL_MEMORY",
    "TSV_LIN_ALLOC_FAILED", "SYSTEM_NO_ROLL", "SYSTEM_NO_MEMORY", "SYSTEM_NO_SHM_MEMORY",
    "MEMORY_NO_MORE_PAGING", "STORAGE_PARAMETERS_WRONG_SET", "LOAD_NO_ROLL", "PXA_NO_FREE_SPACE",
    "CALL_FUNCTION_REMOTE_ERROR", "CALL_FUNCTION_SYSTEM_FAILURE", "CALL_FUNCTION_NO_RECEIVER",
    "CALL_FUNCTION_SEND_ERROR", "CALL_FUNCTION_SINGLE_LOGIN_REJ", "CALL_FUNCTION_SIGNON_REJECTED",
    "CALL_FUNCTION_SIGNON_INCOMPL", "CALL_FUNCTION_DESTINATION_NO_T", "CALL_FUNCTION_NO_AUTH",
    "RFC_NO_AUTHORITY", "SYSTEM_FAILURE", "COMMUNICATION_FAILURE",
}
_BASIS_PREFIX = ("DBIF_", "DBSQL_", "DB_ERR", "CPIC", "RFC_", "TSV_TNEW_")
_BASIS_WORDS = ("NO_ROLL", "NO_MEMORY", "ALLOC_FAILED", "NO_MORE_PAGING", "SYSTEM_FAILURE",
                "COMMUNICATION_FAILURE")


def classify_dump(error: str, exception: str = "") -> str:
    """'basis' for memory / database / RFC dumps, else 'program'."""
    for name in (str(error or "").strip().upper(), str(exception or "").strip().upper()):
        if not name:
            continue
        if name in _BASIS_EXACT or name.startswith(_BASIS_PREFIX) or any(w in name for w in _BASIS_WORDS):
            return "basis"
    return "program"


def teams_for_dump(system: str, error: str, exception: str, component: str) -> list[str]:
    """The teams a dump goes to (see the module text)."""
    if classify_dump(error, exception) == "basis":
        return [BASIS]
    out = [ABAP]
    comp = str(component or "").strip().upper()
    if comp:
        for team in team_names(system):
            t = team.upper()
            if team in (BASIS, ABAP):
                continue
            if comp == t or comp.startswith(t + "-"):
                out.append(team)
                if BASIS not in out:
                    out.append(BASIS)
    return out


# ---------------------------------------------------------------- the file

def _path() -> str:
    try:
        from core.config_loader import CONFIG_DIR
    except Exception:  # noqa: BLE001
        CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
    return os.path.join(CONFIG_DIR, "alert_teams.yaml")


def load() -> dict:
    try:
        import yaml
        with open(_path(), "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        data = {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"alert_teams.yaml unreadable: {exc}")
        data = {}
    std = data.get("standard") if isinstance(data.get("standard"), dict) else {}
    systems = data.get("systems") if isinstance(data.get("systems"), dict) else {}
    for t in DEFAULT_TEAMS:
        std.setdefault(t, [])
    return {"standard": {str(k): _clean_list(v) for k, v in std.items()},
            "systems": {str(s): {str(k): _clean_list(v) for k, v in (t or {}).items()}
                        for s, t in systems.items() if isinstance(t, dict)}}


def _clean_list(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,;\s]+", v)
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def team_names(system: str | None = None) -> list[str]:
    data = load()
    names = list(data["standard"])
    for n in (data["systems"].get(system or "") or {}):
        if n not in names:
            names.append(n)
    first = [t for t in DEFAULT_TEAMS if t in names]
    return first + sorted(n for n in names if n not in first)


def addresses(system: str, team: str) -> list[str]:
    """A team's own addresses for this system (no Basis fallback)."""
    data = load()
    own = (data["systems"].get(system) or {}).get(team) or []
    return list(own or data["standard"].get(team) or [])


def own_addresses(system: str, team: str) -> list[str]:
    """Only this system's own team on the Alert teams page (not the standard team)."""
    return list((load()["systems"].get(system) or {}).get(team) or [])


def basis_for(system_cfg: dict) -> tuple[list[str], str]:
    """
    (addresses, where from) for the Basis people of one system. Most specific wins:
      1. this system's own Basis team on the Alert teams page
      2. the Basis team box of this system on the Systems page
      3. the standard Basis team on the Alert teams page (all systems)
      4. on call, IBO_BASIS_EMAILS, ALERT_TO_EMAILS
    (Before 27 Oct 2026 the standard team beat the Systems-page box, so editing
    a system's Basis box changed nothing while a standard team was set.)
    """
    from core import recipients as rcp
    system = str(system_cfg.get("name") or "")
    own = own_addresses(system, BASIS)
    if own:
        return own, "Alert teams page (this system's Basis team)"
    field = rcp._split(system_cfg.get("basis_emails"))
    if field:
        return field, "Systems page (Basis team box)"
    std = list(load()["standard"].get(BASIS) or [])
    if std:
        return std, "Alert teams page (standard Basis team, all systems)"
    return rcp.basis(system_cfg), "on call / .env"


def resolve(system_cfg: dict, teams: list[str]) -> tuple[list[str], list[str]]:
    """(addresses, notes) for alerting `teams` on this system, with the Basis fallbacks."""
    system = str(system_cfg.get("name") or "")
    out, notes, seen = [], [], set()

    def add(addrs):
        for a in addrs:
            if a.lower() not in seen:
                seen.add(a.lower())
                out.append(a)

    def basis():
        got, src = basis_for(system_cfg)
        if src != "on call / .env":
            try:
                from core import recipients as rcp
                return rcp.strip_client(got, system_cfg)[0]
            except Exception:  # noqa: BLE001
                return got
        try:
            from core.alert_engine import recipients
            return list((recipients(system_cfg).get("email") or {}).get("to") or [])
        except Exception:  # noqa: BLE001
            return got

    for team in teams or [BASIS]:
        got = addresses(system, team) if team != BASIS else basis()
        if not got and team != BASIS:
            notes.append(f"{team} has no address: sent to Basis")
            got = basis()
        add(got)
    return out, notes


def save(system: str | None, teams: dict, by: str = "") -> dict:
    """Write the standard teams (system None/'') or one system's own teams. Validated."""
    import yaml
    from core import timeline
    from core.config_editor import write_text_safely
    clean: dict = {}
    for name, addrs in (teams or {}).items():
        name = str(name).strip()
        if not _NAME.match(name):
            raise ValueError(f"{name!r} is not a usable team name (letters, digits, up to 30 characters).")
        vals = _clean_list(addrs)
        for a in vals:
            if not _MAIL.match(a):
                raise ValueError(f"{name}: {a!r} is not a mail address.")
        clean[name] = vals
    data = load()
    if system:
        data["systems"][system] = clean
    else:
        for t in (BASIS, ABAP):
            clean.setdefault(t, data["standard"].get(t, []))
        data["standard"] = clean
    text = ("# Alert teams and their mail addresses (Thresholds & alerts page).\n"
            "# standard: every system; systems: a system's own teams.\n"
            "# A team without addresses sends to Basis.\n"
            + yaml.safe_dump(data, sort_keys=True, default_flow_style=False, allow_unicode=True))

    def check(parsed):
        part = (parsed or {}).get("systems", {}).get(system) if system else (parsed or {}).get("standard")
        if {k: list(v or []) for k, v in (part or {}).items()} != clean:
            raise RuntimeError("write check failed")
    write_text_safely("alert_teams.yaml", text, check)
    timeline.add(system or "IBOPS", "config", "Alert teams changed",
                 ", ".join(f"{k}: {len(v)}" for k, v in clean.items()), actor=by or timeline.actor_name())
    return clean
