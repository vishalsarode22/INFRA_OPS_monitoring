"""
Show systems collected by OTHER IBOPS dashboards (collector VMs) on this one.

Why this exists
---------------
Each VM runs its own dashboard and only shows the systems in its own
config/systems.yaml. A collector VM (for example the Ubuntu box that holds a
client VPN) therefore showed its systems nowhere else. This module lets one
dashboard -- normally the Windows one -- pull the other dashboards' live data
and show it next to its own systems, so a system added on any VM appears here
without being configured twice.

How it works
------------
config/remote_sources.yaml lists the other dashboards:

    sources:
      - name: ubuntu
        label: Ubuntu collector
        url: http://127.0.0.1:8001      # SSH tunnel to the Ubuntu dashboard

A background thread asks each source for GET /api/live every few seconds.
That endpoint only returns the source's cache, so polling it never reads SAP
and adds no load on the client's systems. get_live_all() then merges the
results into its own list.

Rules this module will not break
--------------------------------
* A source that stops answering is never shown as healthy. After
  IBO_REMOTE_STALE_SECONDS without a good answer, its systems are replaced by
  "unreachable" placeholders. The last good values are dropped, not replayed.
* A source that has never answered gets a visible placeholder card instead of
  nothing, because an empty wall would look the same as "no systems".
* A failure here must never take the local wall down; the caller wraps merge()
  in try/except and the poller swallows and logs its own errors.
* This module only reads. It never starts a read or a sweep on the remote.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover -- PyYAML is in requirements.txt
    yaml = None

from utils.logger import get_logger

log = get_logger(__name__, "application")

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "remote_sources.yaml"

# name -> {"at": last good fetch (epoch) | None, "systems": [...], "error": str | None}
_state: dict[str, dict] = {}
# displayed name -> (source name, the collector's own name for it). Filled by
# merge() on every wall request, so lookups by a renamed card ("PS4 (Ubuntu)")
# resolve without the caller having to pass the local list again.
_shown: dict[str, tuple[str, str]] = {}
_lock = threading.Lock()
_poller_started = False


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _poll_seconds() -> float:
    return max(5.0, _env_float("IBO_REMOTE_POLL_SECONDS", 15))


def _timeout_seconds() -> float:
    return max(1.0, _env_float("IBO_REMOTE_TIMEOUT_SECONDS", 4))


def _stale_seconds() -> float:
    return max(10.0, _env_float("IBO_REMOTE_STALE_SECONDS", 45))


def load_sources() -> list[dict]:
    """Read config/remote_sources.yaml. No file means the feature is off."""
    path = Path(os.getenv("IBO_REMOTE_SOURCES_FILE") or DEFAULT_CONFIG)
    if yaml is None or not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 -- a bad file must not break the wall
        log.warning(f"remote sources: cannot read {path.name}: {type(exc).__name__}")
        return []
    sources = []
    for item in (data.get("sources") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip().rstrip("/")
        if not name or not url.startswith(("http://", "https://")):
            log.warning(f"remote sources: skipping an entry without a name or http(s) url ({name or '?'})")
            continue
        sources.append({"name": name, "url": url,
                        "label": str(item.get("label") or name).strip(),
                        # Starts runs on the remote, and -- now that the
                        # dashboards have a login (core/auth.py) -- is also
                        # what lets this dashboard READ the remote's data.
                        "token": str(item.get("token") or "").strip(),
                        # Customer scope for the remote's cards: a scoped
                        # user sees them only if this customer is theirs.
                        "customer": str(item.get("customer") or "").strip(),
                        # Where the collector VM itself is, so a failure can
                        # be split into "VM down" vs "tunnel down" vs
                        # "dashboard on the VM not answering" (_diagnose).
                        "ssh_host": str(item.get("ssh_host") or "").strip(),
                        "ssh_port": int(item.get("ssh_port") or 22),
                        "ssh_user": str(item.get("ssh_user") or "").strip(),
                        "remote_port": int(item.get("remote_port") or 8000)})
    return sources


def _source_for(system_name: str) -> tuple[dict, str] | None:
    """(source, remote system name) that currently supplies `system_name`."""
    for item in _displayed(system_name):
        src = next((s for s in load_sources() if s["name"] == item.get("source")), None)
        if src:
            return src, str(item.get("remote_name") or system_name)
    return None


def _call(source: dict, method: str, path: str, token: str = "") -> tuple[int, dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Accept": "application/json"}
    if token:
        headers["X-IBO-Token"] = token
    req = urllib.request.Request(source["url"] + path, method=method, headers=headers)
    try:
        with opener.open(req, timeout=_timeout_seconds()) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:          # 401 / 404 / 409 from the remote
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        return exc.code, body if isinstance(body, dict) else {"detail": str(body)}


def start_remote_run(system_name: str) -> tuple[int, dict]:
    """
    Forward "Monitor this system" to the dashboard that collects it.

    Returns (http status, body) exactly as the remote answered, so the page
    can show the remote's own 409 "already running" or 401 "bad token"
    message. The sweep itself runs on the remote; nothing runs here.
    """
    found = _source_for(system_name)
    if found is None:
        return 404, {"detail": f"{system_name} is not supplied by a remote collector"}
    src, remote_name = found
    if not src.get("token"):
        return 401, {"detail": f"No token configured for collector '{src['label']}' "
                               "in config/remote_sources.yaml; cannot start a run there"}
    status, body = _call(src, "POST", "/api/run-now/" + urllib.parse.quote(remote_name), src["token"])
    if status < 300:
        body = dict(body); body.update(forwarded_to=src["label"], remote_system=remote_name)
        log.info(f"forwarded run of {remote_name} to collector '{src['label']}'")
    else:
        log.warning(f"collector '{src['label']}' refused run of {remote_name}: {status} {body.get('detail')}")
    return status, body


def remote_run_status(system_name: str) -> dict:
    """The collector's /api/run-status, tagged with which collector answered."""
    found = _source_for(system_name)
    if found is None:
        return {"running": False, "remote": False}
    src, _ = found
    status, body = _call(src, "GET", "/api/run-status", src.get("token", ""))
    if status >= 300:
        return {"running": False, "remote": True, "collector": src["label"],
                "error": body.get("detail") or f"HTTP {status}"}
    body = dict(body); body.update(remote=True, collector=src["label"])
    return body


def _fetch(source: dict) -> list[dict]:
    """GET <url>/api/live and return its list of system payloads."""
    # An empty ProxyHandler bypasses any corporate proxy: the URL is normally a
    # local SSH tunnel and must not be sent to a proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Accept": "application/json"}
    if source.get("token"):
        headers["X-IBO-Token"] = source["token"]   # the remote requires login for reads
    req = urllib.request.Request(source["url"] + "/api/live", headers=headers)
    with opener.open(req, timeout=_timeout_seconds()) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    systems = body.get("systems") if isinstance(body, dict) else None
    if not isinstance(systems, list):
        raise ValueError("unexpected answer (no 'systems' list)")
    return [s for s in systems if isinstance(s, dict) and s.get("system")]


def _diagnose(src: dict, exc: Exception) -> str:
    """
    Turn a failed fetch into one of three plain statements (tagged as in
    core/reachability.py):
      [COLLECTOR UNREACHABLE]   the collector VM does not answer on SSH
      [TUNNEL DOWN]             the VM answers, nothing listens on the local
                                tunnel port
      [COLLECTOR NOT ANSWERING] VM and tunnel are up, its dashboard is not
    Without ssh_host in remote_sources.yaml the VM check is skipped and the
    message says so instead of guessing.
    """
    from core import reachability as R
    label = src.get("label") or src.get("name")
    u = urllib.parse.urlparse(src["url"])
    lhost = u.hostname or "127.0.0.1"
    lport = u.port or (443 if u.scheme == "https" else 80)
    ssh_host, ssh_port = src.get("ssh_host"), int(src.get("ssh_port") or 22)
    raw = f"{type(exc).__name__}: {exc}"[:160]

    # Only network-level failures are diagnosed. A collector that answered
    # with something unexpected (bad JSON, no 'systems' list) is reported as
    # it is: the chain works, the answer does not.
    if not isinstance(exc, (OSError, urllib.error.URLError)):
        return raw

    if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
        return (f"[COLLECTOR REFUSED] {label} answered but refused this dashboard's token "
                f"(HTTP {exc.code}). Put the collector's IBO_API_TOKEN in config/remote_sources.yaml.")

    vm_up = R.tcp_open(ssh_host, ssh_port, 3) if ssh_host else None
    if vm_up is False:
        return R.tag(R.COLLECTOR_UNREACHABLE,
                     f"{label} is not reachable: its VM {ssh_host} does not answer on SSH port "
                     f"{ssh_port}. The VM is off or the network to it is down; its systems cannot "
                     f"be shown. ({raw})")
    tunnel_open = R.tcp_open(lhost, lport, 2)
    local = lhost in ("127.0.0.1", "localhost", "::1")
    if not tunnel_open:
        if local:
            who = f"{src.get('ssh_user')}@{ssh_host}" if src.get("ssh_user") and ssh_host else \
                  (f"<user>@{ssh_host}" if ssh_host else "<user>@<collector VM>")
            vm_text = (f"The VM {ssh_host} is up. " if vm_up else
                       "(VM not checked: add ssh_host to remote_sources.yaml.) ")
            return R.tag(R.TUNNEL_DOWN,
                         f"SSH tunnel to {label} is down: nothing is listening on {lhost}:{lport}. "
                         f"{vm_text}Restart the tunnel: ssh -N -L "
                         f"{lport}:127.0.0.1:{src.get('remote_port') or 8000} {who} ({raw})")
        return R.tag(R.COLLECTOR_UNREACHABLE,
                     f"{label} dashboard at {lhost}:{lport} does not answer ({raw}).")
    return R.tag(R.COLLECTOR_HUNG,
                 f"{label}: " + ("the VM and " if vm_up else "") + f"the tunnel are up, but its "
                 f"dashboard does not answer ({raw}). Restart the dashboard on the collector VM.")


_vpn_cache: dict = {}


def remote_vpn_status(source_name: str, vpn: str, max_age: float = 30.0) -> dict | None:
    """
    The VPN watchdog status of a collector (GET /api/vpn-status/<vpn> on it),
    cached for `max_age` seconds. None when the collector cannot be asked;
    {"state": "unknown-collector"} when the source is not configured.
    """
    key = (source_name, vpn)
    hit = _vpn_cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    src = next((s for s in load_sources() if s["name"] == source_name), None)
    if src is None:
        return {"state": "unknown-collector"}
    try:
        status, body = _call(src, "GET", "/api/vpn-status/" + urllib.parse.quote(vpn), src.get("token", ""))
        result = body if status < 300 else None
    except Exception:  # noqa: BLE001 -- collector unreachable: caller decides
        result = None
    _vpn_cache[key] = (time.time(), result)
    return result


def remote_reach(source_name: str, host: str, port: int) -> bool | None:
    """Ask a collector whether IT can reach host:port now. None = could not ask."""
    src = next((s for s in load_sources() if s["name"] == source_name), None)
    if src is None:
        return None
    try:
        q = urllib.parse.urlencode({"host": host, "port": port})
        status, body = _call(src, "GET", "/api/reach-test?" + q, src.get("token", ""))
        return bool(body.get("reachable")) if status < 300 else None
    except Exception:  # noqa: BLE001
        return None


def refresh_remote(timeout: float = 90.0) -> list[str]:
    """
    Make every collector read its systems NOW, wait for it, then fetch.

    The collectors are on-demand too: their /api/live serves whatever they
    last read, which can be many minutes old. "Read live" on this wall used
    to show that stale answer. This POSTs /api/live/refresh to each
    collector (token required), polls its /api/live/refresh/status until the
    pass finishes (bounded by `timeout`), then runs poll_once() so the wall
    gets the fresh values. Returns one line per source for the log.
    Never raises.
    """
    notes = []
    sources = load_sources()
    for src in sources:
        token = src.get("token") or ""
        if not token:
            notes.append(f"{src['name']}: no token, collector not asked to re-read")
            continue
        try:
            status, body = _call(src, "POST", "/api/live/refresh", token)
            if status >= 300:
                notes.append(f"{src['name']}: refresh refused (HTTP {status} {body.get('detail', '')})")
                continue
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(2)
                st, b = _call(src, "GET", "/api/live/refresh/status", token)
                if st < 300 and not b.get("in_progress"):
                    break
            notes.append(f"{src['name']}: collector re-read its systems")
        except Exception as exc:  # noqa: BLE001 -- the fetch below reports it
            notes.append(f"{src['name']}: refresh not possible ({type(exc).__name__})")
    if sources:
        poll_once()
    return notes


def poll_once() -> None:
    """Fetch every source once and record the result. Never raises."""
    for src in load_sources():
        try:
            systems, err = _fetch(src), None
        except Exception as exc:  # noqa: BLE001 -- record it, keep polling
            systems = None
            try:
                err = _diagnose(src, exc)
            except Exception:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"[:160]
        try:
            from core import reachability as _R
            key = f"collector:{src['name']}"
            if err is None:
                _R.report_ok(key, {})
            else:
                _R.report(key, err, {})
        except Exception:  # noqa: BLE001
            pass
        with _lock:
            st = _state.setdefault(src["name"], {"at": None, "systems": [], "error": None})
            if err is None:
                st.update(at=time.time(), systems=systems, error=None)
            else:
                # Keep the last system list only so unreachable placeholders
                # can name them. "at" is not advanced, so the age keeps growing.
                st["error"] = err
        if err:
            log.warning(f"remote source '{src['name']}' not answering: {err}")


def _poll_forever() -> None:
    while True:
        try:
            poll_once()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"remote poller error: {type(exc).__name__}: {exc}")
        time.sleep(_poll_seconds())


def ensure_poller() -> None:
    """Start the background poller once. Not started under pytest."""
    global _poller_started
    if _poller_started or "pytest" in sys.modules:
        return
    _poller_started = True
    threading.Thread(target=_poll_forever, name="remote-sources", daemon=True).start()


def _unreachable(base: dict, name: str, src: dict, reason: str) -> dict:
    """Placeholder card. Same keys the wall's card() reads unguarded."""
    label = src["label"]
    return {
        "system": name,
        "sid": base.get("sid") or name,
        "client": f"{base.get('client', '')} · via {label}",
        "connected": False, "error": reason, "cooldown": 0,
        "cpu": None, "memory": None, "load_1m": None,
        "smon": {}, "checks": [], "instances": [], "work_processes": {},
        "dispatcher": {"label": "Unknown", "status": "UNKNOWN", "source": ""},
        "icm": {"label": "Unknown", "status": "UNKNOWN", "source": ""},
        "source": src["name"], "source_label": label,
        "customer": base.get("customer") or src.get("customer") or "",
        "remote": True, "source_unreachable": True,
    }


def merge(local: list[dict]) -> list[dict]:
    """Return `local` plus every remote system, tagged with where it came from."""
    sources = load_sources()
    if not sources:
        return local
    ensure_poller()
    now = time.time()
    taken = {str(p.get("system")) for p in local}
    out = list(local)
    shown: dict[str, tuple[str, str]] = {}

    for src in sources:
        with _lock:
            st = dict(_state.get(src["name"]) or {})
        label = src["label"]
        at = st.get("at")
        age = (now - at) if at else None
        systems = st.get("systems") or []

        if at is None:
            err = st.get("error") or ""
            reason = (err if err.startswith("[") else
                      f"collector '{label}' has not answered yet" + (f" ({err})" if err else ""))
            out.append(_unreachable({"sid": label}, f"{label} (collector)", src, reason))
            continue

        stale = age > _stale_seconds()
        for payload in systems:
            name = str(payload["system"])
            if name in taken:
                name = f"{name} ({label})"      # a local system keeps its own name
            taken.add(name)
            shown[name] = (src["name"], str(payload["system"]))
            if stale:
                err = st.get("error") or ""
                reason = ((err if err.startswith("[") else
                           f"collector '{label}' unreachable" + (f" ({err})" if err else ""))
                          + f"; last data {int(age)}s old, not shown as live")
                out.append(_unreachable(payload, name, src, reason))
                continue
            item = dict(payload)
            item.update(
                customer=payload.get("customer") or src.get("customer") or "",
                system=name, remote_name=str(payload["system"]),
                source=src["name"], source_label=label, remote=True,
                remote_age_seconds=int(age + (payload.get("cache_age_seconds") or 0)),
                client=f"{payload.get('client', '')} · via {label}",
            )
            out.append(item)
    with _lock:
        _shown.clear(); _shown.update(shown)
    return _scope_filter(out)


def _scope_filter(items: list[dict]) -> list[dict]:
    """
    Drop remote cards outside the logged-in user's customer scope. Local
    systems were already filtered by core.config_loader.get_systems().
    A remote card's customer comes from the remote payload, else from the
    source's `customer:` in remote_sources.yaml, else "Infrabeat".
    """
    try:
        from core.auth import current_user, DEFAULT_CUSTOMER
    except Exception:
        return items
    user = current_user.get()
    if user is None or user.get("customers") is None:
        return items
    keep = []
    for it in items:
        if not it.get("remote") and not it.get("source"):
            keep.append(it)
            continue
        cust = str(it.get("customer") or DEFAULT_CUSTOMER)
        if cust in user["customers"]:
            keep.append(it)
    return keep


def _displayed(system_name: str) -> list[dict]:
    """Merged remote items, using the names the wall last showed."""
    with _lock:
        hit = _shown.get(system_name)
    if hit is None:
        return [i for i in merge([]) if i.get("system") == system_name and i.get("remote")]
    src_name, remote_name = hit
    return [dict(i, system=system_name)
            for i in merge([])
            if i.get("remote") and i.get("source") == src_name
            and i.get("remote_name") == remote_name]


def find(system_name: str) -> dict | None:
    """The merged payload for one remote system, for the detail page."""
    items = _displayed(system_name)
    return items[0] if items else None
