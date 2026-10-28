"""
Which of the SAP GUI checks (ST03N, SMICM, ST02, ST04, ST05, STMS, STRUST)
can be read over RFC on this system, and what exactly comes back? READ-ONLY.

    .venv\\Scripts\\python.exe scripts\\probe_rfc_gui_checks.py "CEAT S4P"

For each check it
  1. calls the read-only function modules that could carry the figures and
     prints the shape of every table they return, with the first rows;
  2. checks which tables could hold them (field lists from DD03L, a few rows);
  3. lists remote-enabled function modules whose names match the check.
Writes logs\\probe_rfc_gui_checks_<system>.txt -- send that file.
Nothing is changed in SAP and nothing is mailed. Only well-known display /
read functions are called.
"""
from __future__ import annotations

import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PATTERNS = {
    "SMICM": ["ICM%"],
    "ST02": ["SAPTUNE%", "TH_BUFFER%", "SBUF%", "BUF_%"],
    "ST03N": ["SWNC_COLLECTOR%", "SWNC_GET%"],
    "ST04": ["/SDF/HDB%", "/SDF/DB%", "DB_%", "SDB%"],
    "ST05": ["ST05%", "/SDF/ST05%", "STRC%", "SQLTRACE%", "TH_TRACE%"],
    "STMS": ["TMS_MGR_READ%", "TMS_CI_%", "TMS_UI_GET%", "TMS_TP_CHECK%"],
    "STRUST": ["SSFR_%", "SSF_GET%", "SSFC_%", "/SDF/CERT%", "STRUST%", "SSF_KRN_%"],
}
# Read-only calls (display / statistics functions only).
CALLS = [
    ("SMICM", "ICM_GET_INFO2", {}),
    ("SMICM", "ICM_GET_INFO", {}),
    ("ST02", "SAPTUNE_GET_SUMMARY_STATISTIC", {}),
    ("ST04", "/SDF/HDB_GET_DB_INFO", {}),
]
TABLES = {
    "STMS": ["TMSCSYS", "TMSCDOM"],
    "STRUST": ["SSF_PSE_D", "SSF_PSE_H", "STRUSTCERT", "STRUSTCAB"],
    "ST05": ["ST05_TRC_STATE", "SST05STATE"],
    "ST04": ["M_SERVICE_MEMORY", "M_VOLUME_FILES", "M_DISKS", "M_HOST_RESOURCE_UTILIZATION"],
}


def _out(lines, text=""):
    print(text)
    lines.append(str(text))


def _dump(lines, label, value, rows=3):
    if isinstance(value, dict):
        tables = {k: v for k, v in value.items() if isinstance(v, list)}
        scalars = {k: v for k, v in value.items() if not isinstance(v, (list, dict))}
        structs = {k: v for k, v in value.items() if isinstance(v, dict)}
        if scalars:
            _out(lines, f"   {label} scalars: " + ", ".join(f"{k}={str(v)[:30]!r}" for k, v in list(scalars.items())[:30]))
        for k, v in structs.items():
            _out(lines, f"   {label}.{k} (structure): " + ", ".join(f"{a}={str(b)[:30]!r}" for a, b in list(v.items())[:30]))
        for k, v in tables.items():
            _out(lines, f"   {label}.{k}: {len(v)} rows")
            for r in v[:rows]:
                if isinstance(r, dict):
                    _out(lines, "      " + ", ".join(f"{a}={str(b)[:28]!r}" for a, b in list(r.items())[:30]))
                else:
                    _out(lines, f"      {str(r)[:200]}")
    else:
        _out(lines, f"   {label}: {str(value)[:300]}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    from dotenv import load_dotenv
    load_dotenv(".env")
    from core.config_loader import get_all_systems
    from collectors.rfc_collector import SapSession
    cfg = next((c for c in get_all_systems() if c.get("name") == name), None)
    if cfg is None:
        print(f"unknown system {name}")
        return 2
    lines: list[str] = []
    today = date.today().strftime("%Y%m%d")
    with SapSession(name, cfg) as s:
        if not s.ok:
            print(f"RFC logon failed: {getattr(s, 'error', '')}")
            return 1
        _out(lines, f"== {name}  (read-only probe, {today})")

        _out(lines, "\n== 1. Function module calls")
        for check, fm, kw in CALLS:
            res = s.call(fm, **kw)
            _out(lines, f"\n-- [{check}] {fm}: " + ("NOT AVAILABLE: " + str(getattr(s, 'last_error', ''))[:200]
                                                   if res is None else "OK"))
            if res is not None:
                _dump(lines, fm, res)

        # ST03N: today's aggregate, all task types, TOTAL and per instance
        _out(lines, "\n-- [ST03N] SWNC_COLLECTOR_GET_AGGREGATES (today, TOTAL)")
        sid = str(cfg.get("sap_system_id") or "").strip()
        for comp in ("TOTAL",):
            res = s.call("SWNC_COLLECTOR_GET_AGGREGATES", COMPONENT=comp, ASSIGNDSYS=sid,
                         PERIODTYPE="D", PERIODSTRT=today)
            if res is None:
                _out(lines, "   NOT AVAILABLE: " + str(getattr(s, 'last_error', ''))[:200])
                continue
            tt = res.get("TASKTIMES") or []
            _out(lines, f"   TASKTIMES rows: {len(tt)}; fields: {sorted(tt[0].keys()) if tt else '-'}")
            agg: dict = {}
            for r in tt:
                k = str(r.get("TASKTYPE"))
                a = agg.setdefault(k, [0, 0.0])
                try:
                    a[0] += float(r.get("COUNT") or 0)
                    a[1] += float(r.get("RESPTI") or 0)
                except (TypeError, ValueError):
                    pass
            for k, (c, t) in sorted(agg.items()):
                _out(lines, f"   TASKTYPE {k!r}: steps {c:,.0f}, total resp {t:,.0f} -> avg {t / c if c else 0:,.1f}")

        _out(lines, "\n== 2. Tables (fields from DD03L, then up to 5 rows)")
        for check, tabs in TABLES.items():
            for t in tabs:
                f = s.read_table("DD03L", ["FIELDNAME"], f"TABNAME = '{t}' AND AS4LOCAL = 'A'", 120)
                fields = [r[0].strip() for r in (f or []) if r and r[0].strip() and not r[0].startswith(".")]
                if not fields:
                    _out(lines, f"\n-- [{check}] {t}: no such table / not readable")
                    continue
                _out(lines, f"\n-- [{check}] {t}: fields {', '.join(fields[:40])}")
                rows = s.read_table(t, fields[:12], "", 5)
                if rows is None:
                    _out(lines, f"   rows not readable: {str(getattr(s, 'last_error', ''))[:200]}")
                else:
                    _out(lines, f"   {len(rows)} row(s) (first 5, first 12 fields):")
                    for r in rows:
                        _out(lines, "      " + " | ".join(str(v).strip()[:24] for v in r))

        _out(lines, "\n== 3. Remote-enabled function modules per check (TFDIR, FMODE='R')")
        for check, pats in PATTERNS.items():
            found = []
            for p in pats:
                rows = s.read_table("TFDIR", ["FUNCNAME", "FMODE"], f"FUNCNAME LIKE '{p}' AND FMODE = 'R'", 200)
                found += [r[0].strip() for r in (rows or []) if r and r[0].strip()]
            found = sorted(set(found))
            _out(lines, f"\n-- {check}: {len(found)} remote-enabled")
            texts = {}
            for i in range(0, len(found), 8):
                cond = " OR ".join(f"FUNCNAME = '{n}'" for n in found[i:i + 8])
                got = s.read_table("TFTIT", ["FUNCNAME", "STEXT"], f"SPRAS = 'E' AND ( {cond} )", 40) or []
                texts.update({r[0].strip(): r[1].strip() for r in got if len(r) > 1})
            for fm in found[:60]:
                _out(lines, f"   {fm:<40} {texts.get(fm, '')}")
    path = os.path.join("logs", f"probe_rfc_gui_checks_{name.replace(' ', '_')}.txt")
    os.makedirs("logs", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\nWritten to {path} -- send this file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
