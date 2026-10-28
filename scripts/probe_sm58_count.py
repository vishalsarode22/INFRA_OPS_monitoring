"""
Why does the dashboard's "tRFC entries today" differ from SM58's
"Number of entries displayed"? READ-ONLY: reads ARFCSSTATE (and TRFCQOUT to
spot qRFC LUWs) for today and prints the split by every field that could
explain the difference. Nothing is changed in SAP.

    .venv\\Scripts\\python.exe scripts\\probe_sm58_count.py "CEAT S4P"

Writes logs\\probe_sm58_<system>.txt -- send that file.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

LIMIT = 50000


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
    today = date.today().strftime("%Y%m%d")
    out: list[str] = []

    def say(t=""):
        print(t)
        out.append(str(t))

    with SapSession(name, cfg) as s:
        if not s.ok:
            print(f"RFC logon failed: {getattr(s, 'error', '')}")
            return 1
        say(f"== {name}  RFC client {cfg.get('rfc', {}).get('client') or cfg.get('client')}  date {today}")
        fields = s.read_table("DD03L", ["FIELDNAME"], "TABNAME = 'ARFCSSTATE' AND AS4LOCAL = 'A'", 200) or []
        fields = [r[0].strip() for r in fields if r and r[0].strip() and not r[0].startswith(".")]
        say(f"ARFCSSTATE fields: {', '.join(fields)}")
        want = [f for f in ("ARFCSTATE", "ARFCDEST", "ARFCUSER", "ARFCRESERV", "MANDT", "ARFCCLIENT",
                            "ARFCFNAM", "ARFCTCODE") if f in fields]
        key = [f for f in ("ARFCIPID", "ARFCPID", "ARFCTIME", "ARFCTIDCNT") if f in fields]
        rows = s.read_table("ARFCSSTATE", want + key, f"ARFCDATUM = '{today}'", LIMIT)
        if rows is None:
            say(f"ARFCSSTATE not readable: {getattr(s, 'last_error', '')}")
            return 1
        say(f"\nARFCSSTATE rows dated today: {len(rows)}" + (" (read limit reached)" if len(rows) >= LIMIT else ""))
        recs = [{c: (str(r[i]).strip() if i < len(r) else "") for i, c in enumerate(want + key)} for r in rows]
        for f in want:
            c = Counter(x[f] or "(blank)" for x in recs)
            say(f"\n-- by {f} ({len(c)} values):")
            for v, n in c.most_common(15):
                say(f"   {v[:60]:<60} {n:>7}")
        if "ARFCSTATE" in want and "ARFCDEST" in want:
            c = Counter((x["ARFCSTATE"] or "(blank)", x["ARFCDEST"] or "(blank)") for x in recs)
            say("\n-- by state + destination:")
            for (st, de), n in c.most_common(15):
                say(f"   {st:<12} {de[:40]:<40} {n:>7}")
        # qRFC: LUWs that sit in an outbound queue (SMQ1), not plain tRFC (SM58)
        if key:
            q = s.read_table("TRFCQOUT", ["QNAME"] + key, "", LIMIT)
            if q is not None:
                say(f"\nTRFCQOUT (all qRFC outbound LUWs): {len(q)} rows")
            if q is None:
                say(f"\nTRFCQOUT not readable: {getattr(s, 'last_error', '')}")
            else:
                qkeys = {tuple(str(v).strip() for v in r[1:]) for r in q}
                inq = [x for x in recs if tuple(x[k] for k in key) in qkeys]
                say(f"\nOf today's ARFCSSTATE rows, {len(inq)} belong to qRFC queues (TRFCQOUT, SMQ1) "
                    f"and {len(recs) - len(inq)} are plain tRFC.")
                if "ARFCSTATE" in want:
                    c = Counter(x["ARFCSTATE"] for x in recs if tuple(x[k] for k in key) not in qkeys)
                    say("   plain tRFC by state: " + ", ".join(f"{k or '(blank)'} {v}" for k, v in c.most_common()))
        # Which rule gives SM58's "entries displayed / failed / in execution"?
        if "ARFCSTATE" in want and "ARFCDEST" in want:
            try:
                qk = qkeys
            except NameError:
                qk = set()
            fail = {"SYSFAIL", "CPICERR"}

            def show(label, sel):
                c = Counter(x["ARFCSTATE"] for x in sel)
                say(f"   {label:<58} total {len(sel):>6}  failed {sum(c[k] for k in fail):>4}  "
                    f"sended {c.get('SENDED', 0):>4}  recorded {c.get('RECORDED', 0):>6}  "
                    f"executed {c.get('EXECUTED', 0):>4}")
            say("\n-- candidate rules (compare with SM58: displayed / failed / in execution):")
            show("A all today", recs)
            show("B not EXECUTED", [x for x in recs if x["ARFCSTATE"] != "EXECUTED"])
            show("C not in a qRFC queue", [x for x in recs if tuple(x[k] for k in key) not in qk])
            show("D not (RECORDED and dest NONE)",
                 [x for x in recs if not (x["ARFCSTATE"] == "RECORDED" and x["ARFCDEST"] == "NONE")])
            if "ARFCRESERV" in want:
                show("E ARFCRESERV blank", [x for x in recs if not x["ARFCRESERV"]])
                c = Counter((x["ARFCRESERV"][:8] or "(blank)") for x in recs)
                say("   ARFCRESERV first 8 chars: " + ", ".join(f"{k} {v}" for k, v in c.most_common(6)))
            if "ARFCTCODE" in want:
                c = Counter((x["ARFCTCODE"] or "(blank)") for x in recs)
                say("   ARFCTCODE: " + ", ".join(f"{k} {v}" for k, v in c.most_common(6)))
            if "ARFCFNAM" in want:
                c = Counter((x["ARFCSTATE"], x["ARFCFNAM"] or "(blank)") for x in recs)
                say("   state + function: " + ", ".join(f"{a}/{b} {v}" for (a, b), v in c.most_common(10)))
    path = os.path.join("logs", f"probe_sm58_{name.replace(' ', '_')}.txt")
    os.makedirs("logs", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print(f"\nWritten to {path} -- send this file, and run SM58 (today, user *) at the same minute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
