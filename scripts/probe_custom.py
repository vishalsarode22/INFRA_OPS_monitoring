"""
Check the custom alerts on one system, and show what SM21's reader offers.

    python scripts/probe_custom.py "CEAT S4P"

Prints:
  1. every custom value as it would be graded now (nothing is sent), with
     the reason for any value that cannot be read on this system;
  2. the interface of RSLG_READ_SLG (for the SM21 red-errors alert) and
     whether it is remote-enabled, so the reader can be written for the
     real parameters instead of guessed ones;
  3. the fields of TRFCQIN (SMQ2) on this release.
Read-only: it changes nothing in SAP and sends no alert.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(".env")

from collectors import custom_reads, extra_checks  # noqa: E402
from collectors.rfc_collector import SapSession  # noqa: E402
from core import custom_alerts  # noqa: E402
from core.config_loader import get_all_systems  # noqa: E402


def _type_fields(p) -> str:
    td = p.get("type_description")
    fields = getattr(td, "fields", None) or []
    names = [f.get("name") if isinstance(f, dict) else getattr(f, "name", "") for f in fields]
    return (f" {getattr(td, 'name', '')}: " + ", ".join(n for n in names if n)) if names else ""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    cfg = next((c for c in get_all_systems() if c.get("name") == name), None)
    if cfg is None:
        print(f"unknown system {name}; known: {', '.join(c.get('name') for c in get_all_systems())}")
        return 2
    with SapSession(name, cfg) as s:
        if not s.ok:
            print(f"RFC logon failed: {s.error}")
            return 1
        raw = custom_reads.read_all(s, name, cfg)
        print(f"== {name}: custom values (SAP time {raw.get('sap_now')}); nothing is sent")
        items = custom_alerts.grade(name, custom_alerts.values(name, raw))
        for it in items:
            inst = f" [{it['instance']}]" if it.get("instance") else ""
            print(f"  {it['status']:8} {it['label']}{inst}: {it.get('value')} {it.get('unit') or ''}"
                  f" (warning {it.get('warning')}, critical {it.get('critical')})"
                  f"{' -> ' + ', '.join(it['teams']) if it.get('teams') and it.get('instance') else ''}")
            if it.get("detail"):
                print(f"           {it['detail']}")

        print("\n== RSLG_READ_SLG (SM21)")
        try:
            desc = s.conn.get_function_description("RSLG_READ_SLG")
            for p in desc.parameters:
                print(f"  {p.get('direction', '?'):18} {p.get('name'):30} {p.get('parameter_type', '')}"
                      f"{' optional' if p.get('optional') else ''}{_type_fields(p)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  not readable over RFC: {type(exc).__name__}: {exc}")
        attrs = extra_checks._rows(s, "TFDIR", ["FUNCNAME", "FMODE"], "FUNCNAME = 'RSLG_READ_SLG'", rows=1)
        if attrs:
            print(f"  remote-enabled: {'yes' if (attrs[0].get('FMODE') or '').strip() == 'R' else 'NO'}"
                  f" (TFDIR FMODE = {attrs[0].get('FMODE')!r})")
        else:
            print(f"  TFDIR not readable: {s.last_error[:120]}")

        print("\n== TRFCQIN fields (SMQ2)")
        print("  " + ", ".join(sorted(extra_checks.table_fields(s, "TRFCQIN") or [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
