"""
READ-ONLY RFC diagnosis for one system: logs on, makes a few small standard
calls one by one, times each, and prints the full error of any that fails.
Nothing is changed in SAP. Uses the system's settings from systems.yaml /
.env / the credential store, exactly like the dashboard.

    .venv\\Scripts\\python.exe scripts\\rfc_probe.py "CEAT S4P"
    .venv\\Scripts\\python.exe scripts\\rfc_probe.py "CEAT S4P" --timeout 120 --trace

--timeout N   seconds the RFC library waits for an answer (dashboard: 30)
--trace       also write an RFC trace (dev_rfc*.trc in logs\\rfc_trace) -- attach it

Writes logs\\rfc_probe_<SYSTEM>.txt -- attach that file.
"""
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(".env")

from collectors.rfc_collector import build_rfc_params  # noqa: E402
from core.config_loader import get_all_systems  # noqa: E402

CALLS = [
    ("RFC_PING", {}),
    ("RFC_SYSTEM_INFO", {}),
    ("TH_SERVER_LIST", {}),
    ("TH_WPINFO", {}),
    ("RFC_READ_TABLE", {"QUERY_TABLE": "T000", "DELIMITER": "|", "ROWCOUNT": 5,
                        "FIELDS": [{"FIELDNAME": "MANDT"}]}),
    ("RFC_READ_TABLE", {"QUERY_TABLE": "SNAP", "DELIMITER": "|", "ROWCOUNT": 5,
                        "FIELDS": [{"FIELDNAME": "DATUM"}],
                        "OPTIONS": [{"TEXT": f"DATUM = '{time.strftime('%Y%m%d')}'"}]}),
]


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print(__doc__)
        return 2
    name = args[0]
    timeout = "30"
    if "--timeout" in args:
        timeout = args[args.index("--timeout") + 1]
    trace = "--trace" in args
    cfg = next((c for c in get_all_systems() if c.get("name") == name), None)
    if cfg is None:
        print(f"{name} is not in config/systems.yaml")
        return 2
    lines = []

    def out(text=""):
        print(text)
        lines.append(text)

    params = build_rfc_params(cfg)
    if not params:
        out("No RFC settings (ashost, user or password missing) -- set the password on Settings first.")
        return 1
    params["NWRFC_COMM_TIMEOUT"] = str(timeout)
    if trace:
        tdir = os.path.join(ROOT, "logs", "rfc_trace")
        os.makedirs(tdir, exist_ok=True)
        os.environ["RFC_TRACE_DIR"] = tdir
        params["TRACE"] = "2"
    shown = {k: v for k, v in params.items() if k.lower() not in ("passwd", "password")}
    out(f"system {name}: {shown}")
    try:
        from pyrfc import Connection
    except Exception as exc:  # noqa: BLE001
        out(f"pyrfc not importable: {exc}")
        return 1
    t = time.time()
    try:
        conn = Connection(**params)
    except Exception as exc:  # noqa: BLE001
        out(f"LOGON FAILED after {time.time() - t:.1f}s: {type(exc).__name__}: {exc}")
        _save(name, lines)
        return 1
    out(f"logon OK in {time.time() - t:.1f}s")
    try:
        attrs = conn.get_connection_attributes()
        out("connection: " + ", ".join(f"{k}={attrs.get(k)}" for k in
                                       ("sysId", "client", "user", "partnerHost", "partnerRelease",
                                        "kernelRel", "rfcRole", "codepage", "partnerCodepage") if k in attrs))
    except Exception as exc:  # noqa: BLE001
        out(f"connection attributes not readable: {exc}")
    for fm, kw in CALLS:
        t = time.time()
        try:
            res = conn.call(fm, **kw)
            size = sum(len(v) if isinstance(v, list) else 1 for v in res.values())
            out(f"OK    {fm:<18} {time.time() - t:6.1f}s  ({size} values)")
        except Exception as exc:  # noqa: BLE001
            out(f"FAIL  {fm:<18} {time.time() - t:6.1f}s  {type(exc).__name__}: {exc}")
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                conn = Connection(**params)
            except Exception as exc2:  # noqa: BLE001
                out(f"      reconnect failed: {exc2}")
                break
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    _save(name, lines)
    return 0


def _save(name, lines):
    os.makedirs("logs", exist_ok=True)
    path = os.path.join("logs", f"rfc_probe_{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nWritten to {path}")


if __name__ == "__main__":
    sys.exit(main())
