"""
Who owns a memory-dump spike -- the deterministic answer.

WHY THIS EXISTS
---------------
The SAP_DUMP_SPIKE rule says "TSV_TNEW_PAGE_ALLOC_FAILED -> memory -> Basis
owns it". That is wrong often enough to matter. A SELECT with no WHERE that
drags ten million rows into an internal table produces exactly that dump,
and the fix is a line of ABAP, not a profile parameter. Routing it to Basis
costs a day of parameter-checking that finds nothing.

The discriminator is not the dump class. It is WHO HELD THE MEMORY when it
ran out, and whether the dumps landed on one program or were scattered
across innocent ones. Both are now observable:

    dumps            screenshot_ST22.extra_data["dumps"]      class, program, user
    who is in PRIV   sap.sm50.priv_mode_wp.extra_data["priv"]  user, report, elapsed
    who held memory  sap.st03.top_user_memory_mb.extra_data    user, MB, instance
    which report     sap.st03.dialog_resp_ms.extra_data["top_reports_by_memory"]

THE LADDER (first match wins)
-----------------------------
    1. One CUSTOM program takes >= 50% of the memory dumps
         -> ABAP. That program. "single_hog"
    2. A CUSTOM report is in PRIV mode now, or tops the memory ranking
       above the hog threshold, and the dumps are spread over other programs
         -> ABAP. That report is the originator; the dumps are collateral.
            "hog_with_collateral"
    3. Dumps spread over >= 3 distinct programs, nobody custom holds memory
         -> BASIS. Server starvation. "system_starvation"
    4. One STANDARD program dominates
         -> FUNCTIONAL. Customising / master data / SAP Note. "standard_code"
    5. Anything else
         -> TRIAGE. Say so; do not guess.

EVERYTHING HERE IS DETERMINISTIC. Same inputs, same verdict, every figure
it relied on is in the evidence. The model may rewrite the summary; it
touches nothing here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# Runtime errors that mean "ran out of memory". The class tells you WHAT
# happened; this module decides WHO.
MEMORY_ERRORS = {
    "TSV_TNEW_PAGE_ALLOC_FAILED", "TSV_TNEW_BLOCKS_NO_ROLL_MEMORY", "TSV_TNEW_OCCURS_NO_ROLL_MEMORY",
    "SYSTEM_NO_ROLL", "STORAGE_PARAMETERS_WRONG_SET", "MEMORY_NO_MORE_PAGING",
    "ITAB_MEMORY_LIMIT_REACHED", "SYSTEM_ROLL_IN_ERROR", "TSV_LIN_ALLOC_FAILED",
    "TSV_TNEW_PAGE_ALLOC_FAILED_ITAB",
}

TEAMS = ("ABAP", "BASIS", "FUNCTIONAL", "TRIAGE")


def is_custom(program: str) -> bool:
    p = (program or "").strip().upper()
    return p.startswith(("Z", "Y")) or (p.startswith("/") and not p.startswith(("/1", "/SDF/")))


@dataclass
class Verdict:
    team: str
    verdict_type: str
    confidence: float
    culprit_program: str = ""
    culprit_users: list[str] = field(default_factory=list)
    reason: str = ""
    evidence: list[str] = field(default_factory=list)      # the figures relied on
    collateral_programs: list[str] = field(default_factory=list)
    memory_dumps: int = 0
    distinct_programs: int = 0

    def header(self) -> str:
        """One machine-readable line; the notifier parses this."""
        return (f"owner_team={self.team} verdict={self.verdict_type} "
                f"confidence={self.confidence:.2f} culprit={self.culprit_program or '-'} "
                f"memory_dumps={self.memory_dumps} programs={self.distinct_programs}")


def parse_header(line: str) -> dict:
    """Inverse of Verdict.header(); tolerant of extra text after it."""
    out = {}
    for token in (line or "").split():
        if "=" in token:
            k, v = token.split("=", 1)
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def memory_dumps_from(metrics: dict) -> list[dict]:
    """Memory-class dumps with program and user, from the GUI ST22 result."""
    raw = metrics.get("screenshot_ST22")
    dumps = (getattr(raw, "extra_data", None) or {}).get("dumps") if raw is not None else None
    if not dumps:
        return []
    out = []
    for d in dumps:
        err = str(d.get("runtime_error") or "").strip().upper()
        if err in MEMORY_ERRORS:
            out.append({"error": err, "program": str(d.get("program") or "").strip().upper(),
                        "user": str(d.get("user") or "").strip().upper(),
                        "host": str(d.get("host") or "").strip()})
    return out


def _extra(metrics: dict, name: str, key: str, default=None):
    m = metrics.get(name)
    if m is None:
        return default
    return (getattr(m, "extra_data", None) or {}).get(key, default)


def priv_entries(metrics: dict) -> list[dict]:
    return list(_extra(metrics, "sap.sm50.priv_mode_wp", "priv", []) or [])


def top_reports_by_memory(metrics: dict) -> list[dict]:
    return list(_extra(metrics, "sap.st03.dialog_resp_ms", "top_reports_by_memory", []) or [])


def top_users_by_memory(metrics: dict) -> list[dict]:
    return list(_extra(metrics, "sap.st03.top_user_memory_mb", "top_users_by_memory", []) or [])


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

def attribute(metrics: dict, cfg: dict | None = None) -> Verdict | None:
    """
    Returns None when the threshold is not met or the dump list is not
    available -- the RFC-only path has counts but no classes, and a verdict
    without a class is a guess.
    """
    t = (cfg or {}).get("thresholds") or {}
    min_dumps = int(t.get("memory_dump_count", 3))
    hog_mb = float(t.get("hog_memory_mb", 1024))
    dominant_share = float(t.get("dominant_share", 0.5))
    spread_programs = int(t.get("spread_programs", 3))

    dumps = memory_dumps_from(metrics)
    if len(dumps) < min_dumps:
        return None

    by_prog = Counter(d["program"] for d in dumps if d["program"])
    by_user = Counter(d["user"] for d in dumps if d["user"])
    distinct = len(by_prog)
    total = len(dumps)
    dom_prog, dom_n = (by_prog.most_common(1)[0] if by_prog else ("", 0))
    dom_share = dom_n / total if total else 0.0
    users_of = lambda prog: sorted({d["user"] for d in dumps if d["program"] == prog and d["user"]})  # noqa: E731

    priv = priv_entries(metrics)
    priv_custom = [p for p in priv if is_custom(p.get("report", ""))]
    reports = top_reports_by_memory(metrics)
    hog_reports = [r for r in reports if is_custom(r.get("report", "")) and float(r.get("max_mb") or 0) >= hog_mb]
    users_mem = top_users_by_memory(metrics)

    ev = [
        f"memory dumps={total} across {distinct} program(s); classes={dict(Counter(d['error'] for d in dumps))}",
        f"dominant program={dom_prog or '-'} share={dom_share:.0%} custom={is_custom(dom_prog)}",
    ]
    if priv:
        ev.append("PRIV now: " + "; ".join(f"{p.get('user')} {p.get('report')} {p.get('elapsed_s')}s on {p.get('instance', '?')}" for p in priv[:5]))
    if reports:
        ev.append("top reports by memory: " + "; ".join(f"{r.get('report')} {r.get('max_mb')}MB ({'custom' if is_custom(r.get('report','')) else 'standard'})" for r in reports[:5]))
    if users_mem:
        ev.append("top users by memory: " + "; ".join(f"{u.get('user')} {u.get('max_mb')}MB on {u.get('instance')}" for u in users_mem[:5]))
    mem = metrics.get("memory")
    if mem is not None and getattr(mem, "value", None) is not None:
        ev.append(f"host memory={mem.display_value} ({mem.status.value})")

    common = dict(memory_dumps=total, distinct_programs=distinct)

    # 1. single custom hog
    if dom_prog and is_custom(dom_prog) and dom_share >= dominant_share:
        return Verdict(
            team="ABAP", verdict_type="single_hog", confidence=0.9,
            culprit_program=dom_prog, culprit_users=users_of(dom_prog),
            reason=f"{dom_prog} is customer code and accounts for {dom_share:.0%} of the memory dumps.",
            evidence=ev, **common,
        )

    # 2. custom originator with collateral
    originator = ""
    if priv_custom:
        originator = str(priv_custom[0].get("report") or "").upper()
        how = f"in PRIV mode now ({priv_custom[0].get('user')}, {priv_custom[0].get('elapsed_s')}s)"
    elif hog_reports:
        originator = str(hog_reports[0].get("report") or "").upper()
        how = f"holding {hog_reports[0].get('max_mb')} MB per step, above the {hog_mb:.0f} MB hog threshold"
    if originator:
        collateral = sorted(p for p in by_prog if p != originator)
        origin_users = sorted({str(p.get("user") or "").upper() for p in priv_custom} |
                              set(r for rr in hog_reports[:1] for r in (rr.get("users") or [])))
        return Verdict(
            team="ABAP", verdict_type="hog_with_collateral", confidence=0.8,
            culprit_program=originator, culprit_users=[u for u in origin_users if u],
            reason=(f"{originator} is customer code {how}. The {total} dumps across "
                    f"{distinct} program(s) are downstream of it: they ran out of memory it was holding."),
            evidence=ev, collateral_programs=collateral, **common,
        )

    # 3. starvation
    if distinct >= spread_programs:
        return Verdict(
            team="BASIS", verdict_type="system_starvation", confidence=0.75,
            culprit_program="", culprit_users=[],
            reason=(f"{total} memory dumps spread over {distinct} unrelated programs with no customer "
                    f"program holding memory -- the server is short, not one program."),
            evidence=ev, collateral_programs=sorted(by_prog), **common,
        )

    # 4. standard code dominates
    if dom_prog and not is_custom(dom_prog) and dom_share >= dominant_share:
        return Verdict(
            team="FUNCTIONAL", verdict_type="standard_code", confidence=0.6,
            culprit_program=dom_prog, culprit_users=users_of(dom_prog),
            reason=(f"{dom_prog} is SAP standard code taking {dom_share:.0%} of the memory dumps. "
                    f"Customising, master data volume or an SAP Note -- not an ABAP defect and not a "
                    f"Basis parameter."),
            evidence=ev, **common,
        )

    # 5. unclear
    return Verdict(
        team="TRIAGE", verdict_type="unclear", confidence=0.3,
        culprit_program=dom_prog, culprit_users=users_of(dom_prog) if dom_prog else [],
        reason=(f"{total} memory dumps over {distinct} program(s), no program dominant, no customer "
                f"program holding memory. Needs a person in ST22."),
        evidence=ev, **common,
    )
