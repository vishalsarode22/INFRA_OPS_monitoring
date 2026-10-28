"""
The Windows VM and the Ubuntu VM watch each other.

SENDER (the Windows dashboard)
Every minute it posts a heartbeat to each dashboard in
config/remote_sources.yaml that has a token (the Ubuntu one, through the
SSH tunnel): POST <url>/api/peer/heartbeat with its host name, whether a
sweep is running, the last sweep, and whether the tunnel looks healthy.
Turn off with IBO_PEER_PUSH=0.

RECEIVER (the Ubuntu dashboard)
Remembers every peer that has ever sent one (heartbeats table, service
"peer:<name>"). When a peer has been silent for IBO_PEER_SILENT_MINUTES
(default 5), Basis and the manager get "[WINDOWS VM SILENT]", repeated
every 30 minutes while it lasts, and "[RECOVERED]" when it is back.

Silent can mean the VM is off or hung, the dashboard on it stopped, or the
tunnel between the two is down -- the heartbeat comes through the tunnel.
To tell those apart, give Ubuntu an address on the VM to probe:
    IBO_PEER_PROBE_<NAME>=10.1.0.50:3389    (RDP, or any port the VM answers)
Then the mail says "the VM answers on 3389, so it is up: the dashboard or
the tunnel stopped" or "the VM does not answer at all".

A peer that is retired on purpose: "Forget" on the Health page.
"""
from __future__ import annotations

import json
import os
import re
import socket
from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__)

PREFIX = "peer:"


def peer_name() -> str:
    return (os.getenv("IBO_PEER_NAME") or socket.gethostname()).strip()[:60]


def silent_after_s() -> float:
    try:
        return float(os.getenv("IBO_PEER_SILENT_MINUTES", "5") or 5) * 60
    except ValueError:
        return 300.0


# ------------------------------------------------------------------ sender

def _status_detail() -> dict:
    detail = {"host": socket.gethostname(), "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        from dashboard.app import _run_state
        detail["sweeping"] = _run_state.get("current_system")
    except Exception:
        pass
    try:
        from core import reachability
        lines = []
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config",
                            "tunnel_forwards.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split("#", 1)[0].strip().split(":")
                    if len(parts) == 4:
                        lines.append(reachability.tcp_open(parts[0], int(parts[1]), timeout=1))
        if lines:
            detail["tunnel_ports_listening"] = f"{sum(lines)}/{len(lines)}"
    except Exception:
        pass
    return detail


def push_heartbeats() -> int:
    """Post our heartbeat to every remote source with a token. Returns how many answered."""
    if os.getenv("IBO_PEER_PUSH", "1").strip().lower() in ("0", "false", "no", "off"):
        return 0
    try:
        from dashboard.remote_sources import load_sources
        sources = [s for s in load_sources() if s.get("token")]
    except Exception:
        return 0
    if not sources:
        return 0
    import urllib.request
    body = json.dumps({"name": peer_name(), "detail": _status_detail()}).encode("utf-8")
    ok = 0
    for src in sources:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(src["url"] + "/api/peer/heartbeat", data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "X-IBO-Token": src["token"]})
            with opener.open(req, timeout=10) as resp:
                if resp.status < 300:
                    ok += 1
        except Exception as exc:  # noqa: BLE001
            log.debug(f"peer heartbeat to {src.get('name')} failed: {exc}")
    return ok


# ---------------------------------------------------------------- receiver

def receive(name: str, detail: dict | None) -> None:
    from core import store
    name = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(name or "peer"))[:60]
    detail = dict(detail or {})
    text = json.dumps(detail, default=str)
    while len(text) > 190 and detail:
        detail.pop(sorted(detail, key=lambda k: len(str(detail[k])))[-1])
        text = json.dumps(detail, default=str)
    store.beat(PREFIX + name, text[:190])


def peers() -> list[dict]:
    from core import store
    out = []
    for h in store.heartbeats():
        if not h["service"].startswith(PREFIX):
            continue
        name = h["service"][len(PREFIX):]
        try:
            detail = json.loads(h.get("detail") or "{}")
        except ValueError:
            detail = {}
        age = h.get("age_seconds")
        out.append({"name": name, "age_seconds": age, "beat_at": h["beat_at"], "detail": detail,
                    "silent": age is None or age > silent_after_s(),
                    "probe": os.getenv(f"IBO_PEER_PROBE_{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}", "")})
    return out


def forget(name: str) -> bool:
    from core import store
    return bool(store.conn().execute("DELETE FROM heartbeats WHERE service=?", (PREFIX + name,)).rowcount)


def _probe(spec: str) -> bool | None:
    if not spec or ":" not in spec:
        return None
    host, _, port = spec.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            return True
    except (OSError, ValueError):
        return False


def check_peers() -> int:
    """Mail about silent peers and their recovery. Returns how many are silent."""
    from core import alert_ledger, recipients, timeline
    from core.config_loader import get_smtp_config
    from notifications.mailer import send_html_now
    silent = 0
    to, cc = recipients.basis_and_manager(None)
    for p in peers():
        name = p["name"]
        if p["silent"]:
            silent += 1
            mins = "never" if p["age_seconds"] is None else f"{p['age_seconds'] // 60} min"
            probe = _probe(p["probe"])
            if probe is True:
                cause = (f"The VM answers on {p['probe']}, so it is running: the IBOPS dashboard on it has "
                         f"stopped, or the SSH tunnel between the VMs is down.")
            elif probe is False:
                cause = (f"The VM does not answer on {p['probe']} either: it is off, hung, or its network is "
                         f"down. Check it in the hypervisor / RDP.")
            else:
                cause = ("It may be off or hung, the dashboard on it may have stopped, or the SSH tunnel "
                         "between the VMs may be down (the heartbeat travels through it). Set "
                         f"IBO_PEER_PROBE_{name.upper()}=<vm ip>:3389 to tell these apart.")
            sent = send_html_now(
                subject=f"[WINDOWS VM SILENT] InfraBeatOps -- {name} has not reported for {mins}",
                html=(f"<p><b>{name} has stopped reporting</b> (last heartbeat {p['beat_at']}).</p>"
                      f"<p>{cause}</p><p>While it is silent, SAP GUI screenshot sweeps and the systems "
                      f"monitored from it are not checked. This mail repeats every 30 minutes; a recovery "
                      f"mail follows.</p>"),
                to=to, cc=cc, smtp_config=get_smtp_config(None, require_recipients=False), system="IBOPS",
                kind="peer_silent", key=name, min_interval_s=1800, severity="CRITICAL")
            if sent:
                timeline.add("IBOPS", "state", f"{name} silent ({mins})", cause, severity="critical")
        else:
            last = [a for a in alert_ledger.recent(50, "IBOPS") if a["key"] == name and not a["suppressed"]
                    and a["kind"] in ("peer_silent", "peer_recovered")]
            if last and last[0]["kind"] == "peer_silent":
                send_html_now(
                    subject=f"[RECOVERED] InfraBeatOps -- {name} is reporting again",
                    html=f"<p>{name} sent a heartbeat at {p['beat_at']}.</p>",
                    to=to, cc=cc, smtp_config=get_smtp_config(None, require_recipients=False), system="IBOPS",
                    kind="peer_recovered", key=name, min_interval_s=60, severity="INFO")
                timeline.add("IBOPS", "state", f"{name} reporting again", severity="ok")
    return silent
