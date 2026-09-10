"""
OS figures (CPU, memory, load) over the CCMS monitoring interface.

Why this exists
---------------
The live read gets CPU/memory/load from two places: the Z observability
function module (only where it is installed) and /SDF/SMON_HEADER (only
where /SDF/SMON is scheduled). CENTOR_QAS has neither, has no OS access, and
so shows "No data" for all three -- honestly, but not usefully.

Every NetWeaver ABAP system has a third source that needs nothing installed
and nothing scheduled: RZ20. saposcol feeds the "Operating System" monitor,
and the BAPI_SYSTEM_MTE_* interface reads it over RFC. That is how Solution
Manager and every third-party SAP monitor read ST06 remotely.

What it needs on the SAP side
-----------------------------
* saposcol running on each host (it is, wherever ST06 shows data).
* The RFC user needs S_XMI_PROD (interface XAL) and S_RZL_ADM display.
  Without them BAPI_XMI_LOGON returns an E message and this reader says
  so; it never guesses.

How it finds the nodes
----------------------
MTE names in RZ20 are not guaranteed across releases ("CPU_Utilization",
"5minLoadAverage", "Physical Memory Free" ...), so rather than hard-coding
a path this walks the Operating System monitor tree once, matches nodes by
substring, and caches the TIDs. Per-node values are then a cheap call each.
Names seen are returned in `seen` so `scripts/probe_ccms_os.py` can show
exactly what a system offers when a tile stays empty.

Every figure carries the host it came from. Across hosts, CPU and memory
report the WORST host -- a fleet average hides the one that is short.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from utils.logger import get_logger

log = get_logger(__name__)

XMI_INTERFACE = "XAL"
XMI_VERSION = "1.0"
XMI_COMPANY = "InfraBeatOps"
XMI_PRODUCT = "InfraBeatOps"
EXT_USER = "INFRABEATOPS"

# The tree walk is the expensive part (one call, hundreds of nodes) and the
# node set does not change; the values behind it do. Cache TIDs for a while,
# read values every poll.
_TREE_TTL_SECONDS = 600
_tree_cache: dict[str, tuple[float, dict[str, list[dict]]]] = {}
_lock = threading.Lock()


def reset_cache(system: str | None = None) -> None:
    with _lock:
        if system is None:
            _tree_cache.clear()
        else:
            _tree_cache.pop(system, None)


# Which MTE short names feed which figure. Lower-cased substring match, all
# words must appear. Order matters: first role that matches wins.
#
# The memory rows are wider than the CPU ones on purpose. Deriving memory
# needs BOTH a free figure and a total, and saposcol does not publish the two
# under the same names on every platform -- AIX, Linux and Windows all differ,
# and on some the total is a static attribute that never appears as an MTE at
# all. That is why SARLOHA PRD filled CPU and load from RZ20 but left memory
# empty: the free node matched and nothing matched as the denominator.
#
# mem_used_pct is checked before the free/total pair because where saposcol
# publishes a utilisation percentage directly, it needs no denominator and no
# arithmetic -- which is one fewer thing that can silently be wrong.
_ROLES: list[tuple[str, tuple[str, ...]]] = [
    ("load_1m",  ("1min", "load")),
    ("load_5m",  ("5min", "load")),
    ("load_15m", ("15min", "load")),
    ("cpu",      ("cpu", "utilization")),
    ("cpu_idle", ("cpu", "idle")),

    # Direct utilisation, no denominator needed.
    ("mem_used_pct", ("memory", "utilization")),
    ("mem_used_pct", ("mem", "used", "%")),
    ("mem_used_pct", ("memory", "used", "percent")),

    # Free INCLUDING reclaimable cache -- the honest free figure on Linux,
    # where the OS deliberately fills RAM with cache it hands back on demand.
    # Preferred over strict free for the percentage. Node names from a live
    # CENTOR_QAS RZ20 tree: "Free Including Fs Cache".
    ("mem_free_inc", ("free", "including", "cache")),
    ("mem_free_inc", ("free", "incl", "cache")),

    # Free / available (strict -- counts cache as used).
    ("mem_free", ("physical", "mem", "free")),
    ("mem_free", ("free", "memory")),
    ("mem_free", ("memory", "free")),
    ("mem_free", ("free", "(value)")),
    ("mem_free", ("real", "mem", "free")),
    ("mem_free", ("available", "memory")),
    ("mem_free", ("mem", "available")),

    # Total / configured. "Configured" is the usual saposcol wording, but
    # several platforms say "physical memory" or "total real memory".
    ("mem_total", ("physical", "mem", "configured")),
    ("mem_total", ("configured", "memory")),
    ("mem_total", ("memory", "configured")),
    ("mem_total", ("physical", "mem", "total")),
    ("mem_total", ("total", "physical", "memory")),
    ("mem_total", ("total", "real", "memory")),
    ("mem_total", ("real", "mem", "configured")),
    ("mem_total", ("installed", "memory")),
    ("mem_total", ("ram", "configured")),
    # Bare "Physical" (seen on CENTOR_QAS: "Physical  15987 MB"). Listed
    # LAST: anything like "Physical Mem Free" is caught by the mem_free
    # patterns above before this can claim it.
    ("mem_total", ("physical",)),

    ("swap_free", ("swap", "free")),
]


def _role_for(name: str) -> str | None:
    n = (name or "").lower().replace("_", " ").replace("-", " ")
    for role, words in _ROLES:
        if all(w in n for w in words):
            return role
    return None


def _num(raw) -> float | None:
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v


def _fmt_mem(value: float, unit: str | None) -> str:
    """
    A human free-memory string, using the unit saposcol declared.

    Deliberately does NOT guess the unit. If saposcol says KB, convert up to
    MB/GB for readability; if it says MB, likewise; if it declared nothing,
    show the bare number with a "?" so the ambiguity is visible rather than
    hidden behind a confident-looking "MB". Guessing the unit is exactly the
    class of mistake that produced the earlier bad readings.
    """
    if value is None:
        return "—"
    u = (unit or "").strip().upper()
    kb = None
    if u in ("KB", "KBYTE", "KILOBYTE"):
        kb = value
    elif u in ("MB", "MBYTE", "MEGABYTE"):
        kb = value * 1024
    elif u in ("GB", "GBYTE", "GIGABYTE"):
        kb = value * 1024 * 1024
    elif u in ("B", "BYTE", "BYTES"):
        kb = value / 1024
    if kb is None:
        # Unknown unit: show the raw number and flag it, do not invent MB.
        return f"{value:,.0f} {unit}" if unit else f"{value:,.0f} (unit?)"
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:,.1f} GB free"
    if kb >= 1024:
        return f"{kb / 1024:,.0f} MB free"
    return f"{kb:,.0f} KB free"


def _ret_error(result: dict | None) -> str | None:
    """BAPIRET2-style RETURN: E/A means failed."""
    if not result:
        return None
    ret = result.get("RETURN")
    rows = ret if isinstance(ret, list) else ([ret] if ret else [])
    for r in rows:
        if str(r.get("TYPE", "")).upper() in ("E", "A"):
            return f"{r.get('ID', '')}{r.get('NUMBER', '')}: {str(r.get('MESSAGE', '')).strip()}"
    return None


def _logon(session) -> str | None:
    """Returns an error string, or None on success."""
    got = session.call("BAPI_XMI_LOGON", EXTCOMPANY=XMI_COMPANY, EXTPRODUCT=XMI_PRODUCT,
                       INTERFACE=XMI_INTERFACE, VERSION=XMI_VERSION)
    if got is None:
        return ("BAPI_XMI_LOGON not callable -- RFC user lacks S_XMI_PROD, "
                "or the BAPI is not remote-enabled on this release")
    return _ret_error(got)


def _logoff(session) -> None:
    session.call("BAPI_XMI_LOGOFF", INTERFACE=XMI_INTERFACE)


def _os_monitor_name(session) -> tuple[str, str]:
    """(monitor set, monitor) for the Operating System monitor."""
    got = session.call("BAPI_SYSTEM_MON_GETLIST", EXTERNAL_USER_NAME=EXT_USER)
    names = (got or {}).get("MONITOR_NAMES") or []
    template = None
    for m in names:
        ms, mo = str(m.get("MS_NAME", "")), str(m.get("MONI_NAME", ""))
        if "operating system" in mo.lower():
            if "templates" in ms.lower():
                return ms, mo
            template = template or (ms, mo)
    return template or ("SAP CCMS Monitor Templates", "Operating System")


def _discover(session, system: str) -> tuple[dict[str, list[dict]], list[str], str | None]:
    """
    Walks the OS monitor once. Returns ({role: [node...]}, names seen, error).
    Each node keeps its TID (opaque structure the value call needs) and the
    context name, which for OS nodes is the host.
    """
    ms, mo = _os_monitor_name(session)
    got = session.call("BAPI_SYSTEM_MON_GETTREE", EXTERNAL_USER_NAME=EXT_USER,
                       MONITOR_NAME={"MS_NAME": ms, "MONI_NAME": mo})
    if got is None:
        return {}, [], f"BAPI_SYSTEM_MON_GETTREE failed for '{ms}' / '{mo}'"
    err = _ret_error(got)
    if err:
        return {}, [], f"BAPI_SYSTEM_MON_GETTREE: {err}"

    nodes = got.get("TREE_NODES") or []
    by_role: dict[str, list[dict]] = {}
    seen: list[str] = []
    for n in nodes:
        short = str(n.get("MTNAMESHRT", "")).strip()
        if short:
            seen.append(short)
        role = _role_for(short)
        if not role:
            continue
        tid = n.get("TID") or {}
        host = str(tid.get("MTMCNAME") or n.get("MTMCNAME") or "").strip() or "?"
        by_role.setdefault(role, []).append({"tid": tid, "host": host, "name": short})

    if not by_role:
        return {}, seen, (f"Operating System monitor '{mo}' has {len(nodes)} nodes "
                          "but none matched CPU / memory / load names")
    log.info(f"[{system}] CCMS OS monitor '{mo}': matched "
             + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_role.items())))
    return by_role, seen, None


def _value(session, node: dict) -> float | None:
    got = session.call("BAPI_SYSTEM_MTE_GETPERFCURVAL", EXTERNAL_USER_NAME=EXT_USER,
                       TID=node["tid"])
    cur = (got or {}).get("CURRENT_VALUE") or {}
    for key in ("LASTPERVAL", "ALRELEVVAL", "AVG01PVAL"):
        v = _num(cur.get(key))
        if v is not None:
            return v
    return None


def _perf_unit(session, node: dict) -> str | None:
    """
    The unit saposcol declared for a performance MTE (KB, MB, %, ...).

    The value BAPI returns a bare number, so free memory reads e.g. 267264
    with no way to know if that is KB or MB. The unit lives in the MTE's
    customising, read once per node here. Called only for the memory nodes
    and only when the tree is (re)discovered, so it rides the 600s tree
    cache -- not an extra call per poll.
    """
    got = session.call("BAPI_SYSTEM_MTE_GETPERFPROP", EXTERNAL_USER_NAME=EXT_USER,
                       TID=node["tid"])
    if not got:
        return None
    props = got.get("PROPERTIES") or got.get("PERF_PROPERTIES") or {}
    for key in ("MTUNIT", "UNIT", "MTNUMUNIT", "PERF_UNIT"):
        unit = str(props.get(key, "") or "").strip()
        if unit:
            return unit
    return None


def read_os(session, system: str) -> dict[str, Any]:
    """
    {cpu, memory, load_1m, load_5m, per_host, source, seen, error}.
    Never raises. Figures are None where nothing was measurable.
    """
    out: dict[str, Any] = {"cpu": None, "memory": None, "load_1m": None, "load_5m": None,
                           "per_host": {}, "source": "RFC · CCMS RZ20", "seen": [],
                           "error": None}
    if not getattr(session, "ok", False):
        out["error"] = "no session"
        return out

    err = _logon(session)
    if err:
        out["error"] = f"XMI logon: {err}"
        log.info(f"[{system}] CCMS OS read unavailable -- {out['error']}")
        return out

    try:
        with _lock:
            hit = _tree_cache.get(system)
        tree = hit[1] if hit and time.monotonic() - hit[0] <= _TREE_TTL_SECONDS else None
        if tree is None:
            tree, seen, err = _discover(session, system)
            out["seen"] = seen
            if err:
                out["error"] = err
                return out
            with _lock:
                _tree_cache[system] = (time.monotonic(), tree)

        per_host: dict[str, dict] = {}
        units: dict[str, dict] = {}
        for role, nodes in tree.items():
            for n in nodes:
                v = _value(session, n)
                if v is None:
                    continue
                h = per_host.setdefault(n["host"], {})
                # First node per role per host wins; a duplicate name under
                # the same host is the same counter.
                if role not in h:
                    h[role] = v
                    # Only the memory figures need a unit to be interpretable;
                    # CPU and load are % and a bare ratio. Read it once, here,
                    # while the tree is being (re)built.
                    if role in ("mem_free", "mem_free_inc", "mem_total", "swap_free"):
                        try:
                            u = _perf_unit(session, n)
                        except Exception:  # noqa: BLE001 -- unit is a nicety
                            u = None
                        if u:
                            units.setdefault(n["host"], {})[role] = u

        for host, vals in per_host.items():
            if vals.get("cpu") is None and vals.get("cpu_idle") is not None:
                vals["cpu"] = round(100.0 - vals["cpu_idle"], 1)

            # A published utilisation percentage beats anything derived.
            pct = vals.get("mem_used_pct")
            if pct is not None and 0 <= pct <= 100:
                vals["memory"] = round(pct, 1)
                continue

            free, total = vals.get("mem_free"), vals.get("mem_total")
            free_inc = vals.get("mem_free_inc")
            # Cache-inclusive free is the honest numerator where it exists:
            # 199 MB "free" next to 1819 MB "free including fs cache" is the
            # OS using RAM as reclaimable cache, not memory pressure.
            if free_inc is not None and total and 0 <= free_inc <= total:
                vals["memory"] = round(100.0 * (1 - free_inc / total), 1)
                vals["memory_basis"] = "excl. reclaimable cache"
            elif free is not None and total and 0 <= free <= total:
                vals["memory"] = round(100.0 * (1 - free / total), 1)
                vals["memory_basis"] = "incl. cache as used"
            elif free is not None and free > 0 and not total:
                # Free figure without a denominator: show the free figure.
                u = (units.get(host) or {}).get("mem_free")
                vals["mem_free_raw"] = free
                vals["mem_free_unit"] = u
                vals["mem_free_display"] = _fmt_mem(free, u)
                vals["memory_note"] = (
                    "free memory read but no configured/total node published "
                    "on this host -- percentage cannot be derived; showing free")
            elif free == 0 and not total:
                # A literal zero with no total and (usually) no unit is not a
                # measurement -- a host with 0 bytes free is a host that has
                # already crashed. This is what saposcol feeding CCMS nothing
                # looks like, and it rendered as "0 (unit?)" on the tile.
                # Report it as the diagnosis it is, not as a number.
                vals["memory_note"] = (
                    "CCMS memory nodes answered zero -- the OS collector "
                    "(saposcol) on this host is likely not feeding CCMS; "
                    "check ST06/OS07N")
                # The same dead feed produces CPU=0. A genuinely idle host
                # shows 0% CPU with real memory figures; 0% CPU next to
                # 0-bytes-free is one broken collector, not two measurements.
                # Showing a green "0%" there would be inventing data.
                if not vals.get("cpu"):
                    vals["cpu"] = None
        out["per_host"] = per_host

        def worst(key):
            vs = [v[key] for v in per_host.values() if v.get(key) is not None]
            return max(vs) if vs else None

        out["cpu"] = worst("cpu")
        out["memory"] = worst("memory")
        out["load_1m"] = worst("load_1m")
        out["load_5m"] = worst("load_5m")
        # Total RAM in MB, unit-converted, for callers that have a better
        # free figure than CCMS does (SMON's cache-inclusive free) but no
        # denominator. Largest host wins, matching worst().
        totals_mb = []
        for host, v in per_host.items():
            t = v.get("mem_total")
            if t is None:
                continue
            u = ((units.get(host) or {}).get("mem_total") or "").upper()
            if u in ("MB", "MBYTE", "MEGABYTE"):
                totals_mb.append(t)
            elif u in ("KB", "KBYTE", "KILOBYTE"):
                totals_mb.append(t / 1024)
            elif u in ("GB", "GBYTE", "GIGABYTE"):
                totals_mb.append(t * 1024)
            elif not u and t > 100_000:
                totals_mb.append(t / 1024)   # unlabelled but clearly KB
            elif not u:
                totals_mb.append(t)          # unlabelled, plausibly MB
        out["mem_total_mb"] = round(max(totals_mb)) if totals_mb else None
        if out["memory"] is None:
            note = next((v["memory_note"] for v in per_host.values()
                         if v.get("memory_note")), None)
            if note:
                out["memory_note"] = note
            # The free figure, from the host with the least free memory --
            # the same "worst host" logic the percentage uses.
            frees = [(v["mem_free_raw"], v.get("mem_free_display"))
                     for v in per_host.values() if v.get("mem_free_raw") is not None]
            if frees:
                worst_free = min(frees, key=lambda t: t[0])
                out["mem_free_display"] = worst_free[1]
        if not any(out[k] is not None for k in ("cpu", "memory", "load_1m", "load_5m")):
            out["error"] = "CCMS nodes found but every value read came back empty"
        return out
    finally:
        _logoff(session)
