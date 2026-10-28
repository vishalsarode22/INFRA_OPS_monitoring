"""
READ-ONLY. Lists the remote-enabled function modules a system offers for
four possible one-click actions, so they can be built on what the system
really has instead of on guesses:

    cancel work process    TH_*      (work process / session control)
    delete spool request   RSPO_R_*  (remote spool functions)
    ICM restart            *ICM*
    IDoc restart           *IDOC* / EDI_*  (inbound / outbound processing)

It reads only the function directory (TFDIR, TFTIT) and each candidate's
interface. Nothing is called, nothing is changed in SAP.

    Windows:  .venv\\Scripts\\python.exe scripts\\find_action_fms.py PS4
    Ubuntu:   ~/ibops/.venv/bin/python scripts/find_action_fms.py SARLOHA_QAS

Writes logs/action_fms_<SYSTEM>.txt -- attach that file.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(".env")

from collectors.rfc_collector import SapSession  # noqa: E402
from core.config_loader import get_all_systems  # noqa: E402

GROUPS = [
    ("Work process / sessions", "FUNCNAME LIKE 'TH_%'", re.compile(r"CANCEL|KILL|DELETE|STOP|WP|MODE|RESET", re.I)),
    ("Spool", "FUNCNAME LIKE 'RSPO_R_%'", re.compile(r".", re.I)),
    ("Spool (other)", "FUNCNAME LIKE 'RSPO_%DELETE%'", re.compile(r".", re.I)),
    ("ICM", "FUNCNAME LIKE '%ICM%'", re.compile(r".", re.I)),
    ("IDoc", "FUNCNAME LIKE '%IDOC%'", re.compile(r"PROCESS|INBOUND|INPUT|START|RESTART|POST|SEND|OUTBOUND|STATUS", re.I)),
    ("EDI", "FUNCNAME LIKE 'EDI_%'", re.compile(r"PROCESS|START|RESTART|SEND|STATUS", re.I)),
]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    cfg = next((c for c in get_all_systems() if c.get("name") == name), None)
    if cfg is None:
        print(f"{name} is not in config/systems.yaml")
        return 2
    lines: list[str] = []

    def out(text=""):
        print(text)
        lines.append(text)
    with SapSession(name, cfg) as s:
        if not s.ok:
            print(f"RFC logon failed: {s.error}")
            return 1
        for title, where, keep in GROUPS:
            rows = s.read_table("TFDIR", ["FUNCNAME", "PNAME", "FMODE"], f"{where} AND FMODE = 'R'", rows=400)
            if rows is None:
                out(f"== {title}: TFDIR not readable ({s.last_error[:120]})")
                continue
            names = sorted({r[0].strip() for r in rows if r and r[0].strip() and keep.search(r[0])})
            texts = {}
            for i in range(0, len(names), 20):
                chunk = names[i:i + 20]
                cond = " OR ".join(f"FUNCNAME = '{n}'" for n in chunk)
                got = s.read_table("TFTIT", ["FUNCNAME", "STEXT"], f"SPRAS = 'E' AND ( {cond} )", rows=40) or []
                texts.update({r[0].strip(): r[1].strip() for r in got if r})
            out(f"== {title}: {len(names)} remote-enabled")
            for n in names[:60]:
                out(f"   {n:<34} {texts.get(n, '')}")
                try:
                    desc = s.conn.get_function_description(n)
                    params = ", ".join(f"{p.get('direction', '?').replace('RFC_', '')}:{p['name']}"
                                       f"{'' if p.get('optional') else '*'}" for p in desc.parameters)
                    out(f"      {params[:400]}")
                except Exception as exc:  # noqa: BLE001
                    out(f"      (interface not readable: {str(exc)[:80]})")
            if len(names) > 60:
                out(f"   ... and {len(names) - 60} more")
    os.makedirs("logs", exist_ok=True)
    path = os.path.join("logs", f"action_fms_{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nWritten to {path} -- attach it.  (* = required parameter)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
