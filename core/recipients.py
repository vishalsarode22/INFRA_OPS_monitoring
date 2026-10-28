"""
Who gets which alert.

Two lists, resolved once here so every alert module agrees:

    BASIS    -- the consultants on shift. First-breach (yellow) alerts,
                VPN failures and credential problems go here.
    MANAGER  -- the Basis manager. Repeat breaches (orange / red) and
                SYSTEM DOWN go here, with Basis in copy.

Configuration, in order of precedence (the same keys core/escalation.py
uses, so there is one manager list, not two). For BASIS, whoever is on
call (config/oncall.yaml, core/oncall.py) comes right after the
per-system list:
    systems.yaml per system:   basis_emails: a@x.com,b@x.com
                               manager_receivers: m@x.com
    .env:                      IBO_BASIS_EMAILS=...
                               ALERT_MANAGER_EMAILS=...   (IBO_MANAGER_EMAILS also read)
    fallback for BASIS only:   ALERT_TO_EMAILS (the normal report list)

Customer addresses (alert_receivers) are NOT used for any of these. VPN,
credential and down alerts are Infrabeat-internal; the customer gets the
normal report, nothing about our access to their systems.
"""
from __future__ import annotations

import os
import re


def _split(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,;\s]+", str(raw))
    out, seen = [], set()
    for item in items:
        item = item.strip()
        if "@" in item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def basis(system_cfg: dict | None = None) -> list[str]:
    """
    Per-system basis_emails, else whoever is ON CALL now (core/oncall.py,
    config/oncall.yaml), else IBO_BASIS_EMAILS, else ALERT_TO_EMAILS.
    """
    cfg = system_cfg or {}
    own = _split(cfg.get("basis_emails"))
    if own:
        return own
    try:
        from core import oncall
        on_call = _split(oncall.basis_emails())
    except Exception:
        on_call = []
    return (on_call
            or _split(os.getenv("IBO_BASIS_EMAILS"))
            or _split(os.getenv("ALERT_TO_EMAILS")))


def manager(system_cfg: dict | None = None) -> list[str]:
    cfg = system_cfg or {}
    return (_split(cfg.get("manager_receivers")) or _split(cfg.get("manager_emails"))
            or _split(os.getenv("ALERT_MANAGER_EMAILS")) or _split(os.getenv("IBO_MANAGER_EMAILS")))


# ---------------------------------------------------- per-event overrides
#
# Three mails can go to their own people instead of Basis / manager. Empty =
# the normal rule. Per system (Systems page) or for all systems in .env:
#     recovery_emails  IBO_RECOVERY_EMAILS  "a value is back to normal"
#     down_emails      IBO_DOWN_EMAILS      SYSTEM DOWN / NOT REACHABLE (+ "reachable again")
#     access_emails    IBO_ACCESS_EMAILS    VPN / tunnel down, SAP refuses IBOPS's logon (+ fixed)
# Client addresses are removed as for every internal mail.
SPECIAL = {"recovery": ("recovery_emails", "IBO_RECOVERY_EMAILS"),
           "down": ("down_emails", "IBO_DOWN_EMAILS"),
           "access": ("access_emails", "IBO_ACCESS_EMAILS")}


def special(system_cfg: dict | None, which: str) -> list[str]:
    """The override list for one event ('recovery' | 'down' | 'access'), [] = use the normal rule."""
    key, env = SPECIAL[which]
    cfg = system_cfg or {}
    raw = cfg.get(key)
    if raw:
        try:
            from core.config_loader import _resolve_env_value
            raw = _resolve_env_value(str(raw))
        except Exception:  # noqa: BLE001
            pass
    found = _split(raw) or _split(os.getenv(env))
    return strip_client(found, system_cfg)[0] if found else []


def basis_and_manager(system_cfg: dict | None = None) -> tuple[list[str], list[str]]:
    """(to, cc): manager in To, Basis in Cc. Falls back to Basis-only."""
    m = manager_internal(system_cfg)
    b = basis_internal(system_cfg)
    if m:
        return m, [x for x in b if x.lower() not in {y.lower() for y in m}]
    return b, []


# ------------------------------------------------------------------ the client
#
# The CLIENT never receives internal alerts (threshold alerts, SYSTEM DOWN,
# VPN, credentials, RCA, failure notices). Clients get only the scheduled
# reports with SAP GUI screenshots. The client list is:
#     systems.yaml per system:   client_emails: c@customer.com
#     .env:                      IBO_CLIENT_EMAILS=...
#   plus the report receivers (alert_receivers / ALERT_TO_EMAILS and
#   alert_receivers_cc / ALERT_CC_EMAILS) that are not ours:
#     - with IBO_INTERNAL_DOMAINS / IBO_INTERNAL_ADDRESSES set: every report
#       receiver outside those;
#     - without them: every report receiver that is not also named in an
#       internal list (basis_emails, IBO_BASIS_EMAILS, manager lists, on-call,
#       alert_teams.yaml). When no internal list exists at all nothing can be
#       told apart and the report list is treated as internal (the old
#       single-tenant setup) -- set IBO_INTERNAL_DOMAINS to be safe.
# strip_client() removes those addresses from an internal alert, whoever
# built the list; notifications/mailer.py applies it to every internal kind.

# Mail kinds that are NOT internal alerts: the scheduled report itself and
# the mails that by design go to a SAP user (job owner, approved user mail).
NON_INTERNAL_KINDS = frozenset({"report", "final_report", "job_owner", "job_user", "user_mail"})


def is_internal_kind(kind: str | None) -> bool:
    return bool(kind) and str(kind) not in NON_INTERNAL_KINDS


def _report_receivers(cfg: dict) -> list[str]:
    def pick(key, env):
        v = cfg.get(key)
        return v if v not in (None, "", []) else os.getenv(env, "")
    return _split(pick("alert_receivers", "ALERT_TO_EMAILS")) + \
        _split(pick("alert_receivers_cc", "ALERT_CC_EMAILS"))


def _explicit_internal(cfg: dict) -> set[str]:
    names: list[str] = []
    names += _split(cfg.get("basis_emails")) + _split(os.getenv("IBO_BASIS_EMAILS"))
    names += manager(cfg)
    try:
        from core import oncall
        names += _split(oncall.basis_emails())
    except Exception:
        pass
    try:
        from core import alert_teams
        data = alert_teams.load()
        for team in (data.get("standard") or {}).values():
            names += _split(team)
        for team in ((data.get("systems") or {}).get(str(cfg.get("name") or "")) or {}).values():
            names += _split(team)
    except Exception:
        pass
    return {n.lower() for n in names}


def _domains_known() -> bool:
    try:
        from core import action_links
        return bool(action_links._domains() or action_links._addresses())
    except Exception:
        return False


def client(system_cfg: dict | None = None) -> list[str]:
    """Addresses that belong to the customer for this system (see above)."""
    cfg = system_cfg or {}
    out = _split(cfg.get("client_emails")) + _split(os.getenv("IBO_CLIENT_EMAILS"))
    receivers = _report_receivers(cfg)
    if _domains_known():
        from core import action_links
        out += [a for a in receivers if not action_links.is_internal(a)]
    else:
        internal = _explicit_internal(cfg)
        if internal:
            out += [a for a in receivers if a.lower() not in internal]
    return _split(out)


def strip_client(addresses, system_cfg: dict | None = None) -> tuple[list[str], list[str]]:
    """(kept, removed): `addresses` without the client's. Never raises."""
    addrs = _split(addresses)
    try:
        blocked = {a.lower() for a in client(system_cfg)}
        if _domains_known():
            # With our domains known, an address outside them in an internal
            # alert is not ours -- unless it is named explicitly as Basis /
            # manager / team (a team member's own mailbox).
            from core import action_links
            explicit = _explicit_internal(system_cfg or {})
            blocked |= {a.lower() for a in addrs
                        if not action_links.is_internal(a) and a.lower() not in explicit}
    except Exception as exc:  # noqa: BLE001
        # Fail CLOSED (review 28.10.2026): keep only the addresses named
        # explicitly as internal (Basis / manager / team); drop the rest.
        try:
            from utils.logger import get_logger
            get_logger(__name__).error(f"strip_client failed ({type(exc).__name__}: {exc}); "
                                       f"keeping only explicit internal addresses")
        except Exception:  # noqa: BLE001
            pass
        try:
            explicit = _explicit_internal(system_cfg or {})
        except Exception:  # noqa: BLE001
            explicit = set()
        blocked = {a.lower() for a in addrs if a.lower() not in explicit}
    kept = [a for a in addrs if a.lower() not in blocked]
    removed = [a for a in addrs if a.lower() in blocked]
    return kept, removed


def basis_internal(system_cfg: dict | None = None) -> list[str]:
    """basis() without any client address (its ALERT_TO_EMAILS fallback can hold one)."""
    return strip_client(basis(system_cfg), system_cfg)[0]


def manager_internal(system_cfg: dict | None = None) -> list[str]:
    return strip_client(manager(system_cfg), system_cfg)[0]
