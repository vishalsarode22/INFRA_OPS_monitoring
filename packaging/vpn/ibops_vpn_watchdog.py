#!/usr/bin/env python3
"""
ibops-vpn: keep one client VPN connected, without ever locking the account
or pushing another user's session out.

Two kinds of VPN, same rules, same status file, same commands:
    FortiGate SSL-VPN      openfortivpn      /etc/openfortivpn/<name>.conf
    FortiGate IPsec        strongSwan        /etc/ibops-vpn/<name>.conf
      (pre-shared key + XAuth user/password, IKEv1 -- what FortiClient
       calls "IPsec VPN"; e.g. Ceat). The connection itself is in
       /etc/ipsec.d/ibops-<name>.conf, the key and password root-only in
       /etc/ipsec.d/ibops-<name>.secrets. Installed by
       packaging/vpn/install_ipsec_vpn.sh.

Runs as root under systemd (ibops-vpn@<name>.service), standard library
only, so it does not depend on the dashboard's virtualenv.

    config   /etc/openfortivpn/<name>.conf      (root-only, mode 600)
    status   /run/ibops-vpn/<name>.json         (world-readable; the
                                                 dashboard reads it)
    memory   /var/lib/ibops-vpn/<name>.json     (survives restarts: a
                                                 "stopped" VPN stays stopped
                                                 until a person resets it)

RULES (decided by Infrabeat, Oct 2026)
  VPN drops, wrong password, certificate changed, network error:
      retry after 2 min, then 5 min, then 15 min; if all three fail,
      STOP and wait for a person (protects the account from lockout).
  Account in use by another session (gateway says so, or our tunnel is
  dropped within 5 minutes of coming up, twice in a row):
      do not fight -- retry after 30 min, 1 h, 2 h, then every 2 h.
      A wrong-password answer during this switches to the 3-try rule.
  The gateway certificate is pinned (trusted-cert in the config). An
  unknown certificate is never sent the password; it counts as an
  ordinary failure and the reason says "certificate changed".

The dashboard turns the status file into the card text and the e-mails
(core/reachability.py, check: openfortivpn:<name>).

Commands:
    ibops-vpn status <name>    show the status file
    ibops-vpn reset  <name>    clear a "stopped" state; the service retries now
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

STATUS_DIR = os.environ.get("IBOPS_VPN_STATUS_DIR", "/run/ibops-vpn")
STATE_DIR = os.environ.get("IBOPS_VPN_STATE_DIR", "/var/lib/ibops-vpn")
CONF_DIR = os.environ.get("IBOPS_VPN_CONF_DIR", "/etc/openfortivpn")
OPENFORTIVPN = os.environ.get("IBOPS_OPENFORTIVPN", "/usr/bin/openfortivpn")
IPSEC_CONF_DIR = os.environ.get("IBOPS_VPN_IPSEC_DIR", "/etc/ibops-vpn")
IPSEC = os.environ.get("IBOPS_IPSEC", "/usr/sbin/ipsec")
IPSEC_CONNECT_TIMEOUT = 120    # charon gives up after ~50 s of retransmits (strongswan.d/ibops-vpn.conf)
IPSEC_CHECK_INTERVAL = 10      # ask charon whether the tunnel is still installed this often

FAIL_DELAYS = [120, 300, 900]            # before retry 1, 2, 3
IN_USE_DELAYS = [1800, 3600, 7200]       # then every 7200
QUICK_DROP_SECONDS = 300
CONNECT_TIMEOUT = 90
PROBE_INTERVAL = 60            # test real connectivity this often while "up"
PROBE_FAILS_TO_DROP = 3        # ... and treat 3 failures in a row as a drop

UP = "up"
CONNECTING = "connecting"
RETRYING = "retrying"
IN_USE = "in_use"
STOPPED = "stopped"

_UP_MARK = re.compile(r"tunnel is up and running", re.I)
_AUTH = re.compile(r"could not authenticate|authentication failed|invalid credential|"
                   r"permission denied|login failed|bad (user|pass)", re.I)
_CERT = re.compile(r"certificate validation failed|not in the local whitelist|"
                   r"certificate.*(mismatch|untrusted|verify)", re.I)
_IN_USE = re.compile(r"already (logged|connected)|in use|maximum (number|login)|"
                     r"too many (users|sessions|logins)|concurrent|login limit|session limit", re.I)


def classify(output: str) -> str:
    """auth | cert | in_use | network, from openfortivpn's output."""
    # (strongSwan output: classify_ipsec)
    text = output or ""
    if _IN_USE.search(text):
        return "in_use"
    if _CERT.search(text):
        return "cert"
    if _AUTH.search(text):
        return "auth"
    return "network"


# strongSwan (IPsec) output. Kept apart from classify(): charon prints words
# such as "in use" about its own internals, which must not read as "the
# account is in use by someone else".
_IPSEC_IN_USE = re.compile(r"login limit|too many (users|sessions|logins)|maximum (number|login)|"
                           r"already (logged|connected)|concurrent (login|session)", re.I)
_IPSEC_AUTH = re.compile(r"xauth authentication of .* failed|xauth.*(failed|rejected)|"
                         r"received authentication_failed|authentication of .* failed|"
                         r"invalid hash|invalid_hash_information|hash.*mismatch|"
                         r"decryption failed|no shared key found|no xauth password found", re.I)
_IPSEC_PROPOSAL = re.compile(r"no_proposal_chosen|no proposal chosen|no matching proposal|"
                             r"invalid_id_information|ts_unacceptable|attributes_not_supported|"
                             r"no virtual ip|virtual ip.*(failed|not)|unable to install", re.I)
_IPSEC_NOT_RUNNING = re.compile(r"charon.*not running|connect\(\) failed|no such file|"
                                r"unable to connect to", re.I)


def classify_ipsec(output: str) -> str:
    """auth | proposal | in_use | network, from `ipsec up` output."""
    text = output or ""
    if _IPSEC_IN_USE.search(text):
        return "in_use"
    if _IPSEC_AUTH.search(text):
        return "auth"
    if _IPSEC_PROPOSAL.search(text):
        return "proposal"
    return "network"


REASON_TEXT = {
    "auth": "the gateway rejected the user/password (or, for IPsec, the pre-shared key)",
    "proposal": "the gateway did not accept our IPsec settings (mode, proposals or remote network) -- "
                "compare /etc/ipsec.d/ibops-<name>.conf with FortiClient's Advanced Settings",
    "cert": "the gateway certificate changed (not the pinned one), password not sent",
    "network": "the gateway did not answer (network)",
    "in_use": "the VPN account is in use by another session",
    "quick_drop": "the tunnel was dropped twice within 5 minutes of connecting "
                  "(account most likely in use by another session)",
    "drop": "the tunnel dropped",
}


class Policy:
    """
    The retry rules, separate from processes and clocks so they can be
    tested. Call on_connected(), on_drop(uptime) or on_failure(kind); each
    returns (state, delay_seconds or None for STOPPED, reason).
    """

    def __init__(self, remembered: dict | None = None):
        r = remembered or {}
        self.mode = r.get("mode", "normal")          # normal | in_use
        self.failures = int(r.get("failures", 0))    # scheduled retries used (normal)
        self.in_use_tries = int(r.get("in_use_tries", 0))
        self.quick_drops = int(r.get("quick_drops", 0))
        self.stopped = bool(r.get("stopped", False))
        self.reason = r.get("reason", "")

    def snapshot(self) -> dict:
        return {"mode": self.mode, "failures": self.failures, "in_use_tries": self.in_use_tries,
                "quick_drops": self.quick_drops, "stopped": self.stopped, "reason": self.reason}

    def _normal_retry(self, reason_key: str):
        self.mode = "normal"
        if self.failures >= len(FAIL_DELAYS):
            self.stopped = True
            self.reason = REASON_TEXT[reason_key]
            return STOPPED, None, self.reason
        delay = FAIL_DELAYS[self.failures]
        self.failures += 1
        self.reason = REASON_TEXT[reason_key]
        return RETRYING, delay, self.reason

    def _in_use_retry(self, reason_key: str):
        self.mode = "in_use"
        delay = IN_USE_DELAYS[min(self.in_use_tries, len(IN_USE_DELAYS) - 1)]
        self.in_use_tries += 1
        self.reason = REASON_TEXT[reason_key]
        return IN_USE, delay, self.reason

    def on_connected(self):
        # Counters reset only once the tunnel has HELD (see on_drop): a
        # tunnel that is kicked within seconds must still count.
        return UP, None, ""

    def on_drop(self, uptime: float):
        if uptime < QUICK_DROP_SECONDS:
            self.quick_drops += 1
        else:
            self.quick_drops = 0
            self.failures = 0
            self.in_use_tries = 0
            self.mode = "normal"
        if self.quick_drops >= 2:
            return self._in_use_retry("quick_drop")
        if self.mode == "in_use":
            return self._in_use_retry("in_use")
        return self._normal_retry("drop")

    def on_held(self):
        """Tunnel stayed up past QUICK_DROP_SECONDS: everything is fine again."""
        self.failures = self.in_use_tries = self.quick_drops = 0
        self.mode, self.reason = "normal", ""

    def on_failure(self, kind: str):
        if kind == "in_use":
            return self._in_use_retry("in_use")
        if kind == "auth" and self.mode == "in_use":
            # A password problem must never ride the every-2-hours loop.
            self.failures = 0
        return self._normal_retry(kind)

    def reset(self):
        self.__init__({})


# ------------------------------------------------------------------ files

def _write_json(path: str, data: dict, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def status_path(name: str) -> str:
    return os.path.join(STATUS_DIR, f"{name}.json")


def memory_path(name: str) -> str:
    return os.path.join(STATE_DIR, f"{name}.json")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_status(name: str, state: str, reason: str = "", next_at: float | None = None,
                 attempt: str = "", detail: str = "", since: str | None = None) -> None:
    prev = _read_json(status_path(name))
    _write_json(status_path(name), {
        "name": name, "state": state, "reason": reason, "attempt": attempt,
        "detail": detail[-400:],
        "since": since or (prev.get("since") if prev.get("state") == state else _now()),
        "updated": _now(),
        "next_attempt": (datetime.fromtimestamp(next_at).strftime("%Y-%m-%d %H:%M:%S")
                         if next_at else None),
    })


# ------------------------------------------------------------------ probe
#
# "Up" must mean traffic gets through, not just that openfortivpn is still
# running: a tunnel can die inside a living process, and then the dashboard
# was told "VPN up" and blamed the SAP system instead. Probe targets come
# from the config file, as comment lines openfortivpn ignores:
#     # ibops-probe = 192.0.2.127:3300
#     # ibops-probe = 192.0.2.123:3300
# One answering target is enough. Three failed rounds in a row (3 minutes)
# count as a drop: openfortivpn is stopped and the normal retry rules apply.

def backend(name: str) -> str:
    """'ipsec' when /etc/ibops-vpn/<name>.conf exists, else 'openfortivpn'."""
    return "ipsec" if os.path.isfile(os.path.join(IPSEC_CONF_DIR, f"{name}.conf")) else "openfortivpn"


def conf_file(name: str) -> str:
    return os.path.join(IPSEC_CONF_DIR if backend(name) == "ipsec" else CONF_DIR, f"{name}.conf")


def ipsec_settings(name: str) -> dict:
    """key = value lines of /etc/ibops-vpn/<name>.conf (no secrets in that file)."""
    out = {}
    try:
        with open(os.path.join(IPSEC_CONF_DIR, f"{name}.conf"), encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"\s*([A-Za-z_]+)\s*=\s*(.*?)\s*$", line)
                if m and not line.lstrip().startswith("#"):
                    out[m.group(1).lower()] = m.group(2)
    except OSError:
        pass
    out.setdefault("conn", f"ibops-{name}")
    return out


def probe_targets(name: str) -> list[tuple[str, int]]:
    out = []
    try:
        with open(conf_file(name), encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"\s*#\s*ibops-probe\s*=\s*([\w.\-]+):(\d+)", line)
                if m:
                    out.append((m.group(1), int(m.group(2))))
    except OSError:
        pass
    return out


def probe_ok(targets: list[tuple[str, int]], timeout: float = 5.0) -> bool | None:
    """True/False, or None when no targets are configured (no opinion)."""
    if not targets:
        return None
    import socket
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


# ------------------------------------------------------------------- run

_child: subprocess.Popen | None = None


def _wait(name: str, seconds: float) -> bool:
    """
    Sleep up to `seconds`, but wake early when `ibops-vpn reset` is run.
    Returns True when a reset was requested. Without this, a reset during a
    30-minute "in use" wait (or a 15-minute retry wait) did nothing until
    the wait ran out.
    """
    started = time.time()
    while time.time() - started < seconds:
        time.sleep(min(5.0, max(0.0, seconds - (time.time() - started))))
        if float(_read_json(memory_path(name)).get("reset_at") or 0) > started:
            return True
    return False


_running_name = {"name": ""}


class FortiLink:
    """A running openfortivpn process."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def alive(self) -> bool:
        try:
            self.proc.stdout.readline()           # keep the pipe drained
        except Exception:
            pass
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except Exception:
                self.proc.kill()


def _ipsec(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        r = subprocess.run([IPSEC] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out + "\ntimed out waiting for the tunnel"
    except OSError as exc:
        return 127, f"{IPSEC}: {exc}"


def ipsec_installed(conn: str) -> bool:
    """True while charon has an INSTALLED child SA for the connection."""
    rc, out = _ipsec(["status", conn], 15)
    return bool(re.search(r"^\s*" + re.escape(conn) + r"\{\d+\}:\s+INSTALLED", out, re.M))


def ipsec_ready() -> tuple[bool, str]:
    """charon must be running; start strongswan-starter once when it is not."""
    rc, out = _ipsec(["status"], 15)
    if rc == 0 and not _IPSEC_NOT_RUNNING.search(out):
        return True, ""
    for unit in ("strongswan-starter", "strongswan"):
        try:
            r = subprocess.run(["systemctl", "start", unit], capture_output=True, text=True, timeout=30)
        except Exception:  # noqa: BLE001
            continue
        if r.returncode == 0:
            break
    time.sleep(3)
    rc, out = _ipsec(["status"], 15)
    if rc == 0 and not _IPSEC_NOT_RUNNING.search(out):
        return True, ""
    return False, "strongSwan (charon) is not running and could not be started: " + out.strip()[-200:]


class IpsecLink:
    """An IPsec tunnel held by charon; checked every IPSEC_CHECK_INTERVAL seconds."""

    def __init__(self, conn: str):
        self.conn = conn
        self._checked = time.time()
        self._ok = True

    def alive(self) -> bool:
        if time.time() - self._checked >= IPSEC_CHECK_INTERVAL:
            self._checked = time.time()
            self._ok = ipsec_installed(self.conn)
        return self._ok

    def stop(self) -> None:
        _ipsec(["down", self.conn], 30)
        self._ok = False


_link = {"link": None}


def _stop_child(*_a):
    if _child and _child.poll() is None:
        _child.terminate()
        try:
            _child.wait(10)
        except Exception:
            _child.kill()
    if isinstance(_link["link"], IpsecLink):
        try:
            _link["link"].stop()
        except Exception:
            pass
    # Stopped by systemd (Disconnect on the dashboard's Settings page, or
    # systemctl stop): say so, so nobody reads the last "up" as current.
    if _running_name["name"]:
        try:
            write_status(_running_name["name"], "off", "the VPN service was stopped")
        except Exception:
            pass
    sys.exit(0)


def connect_once_ipsec(name: str):
    """`ipsec up` the connection. (True, IpsecLink, output) or (False, None, output)."""
    conn = ipsec_settings(name)["conn"]
    ok, why = ipsec_ready()
    if not ok:
        return False, None, why
    _ipsec(["down", conn], 30)                    # a half-open leftover would block the new one
    rc, out = _ipsec(["up", conn], IPSEC_CONNECT_TIMEOUT)
    if ipsec_installed(conn):
        link = IpsecLink(conn)
        _link["link"] = link
        return True, link, out
    _ipsec(["down", conn], 30)                    # never leave charon retrying behind our back
    return False, None, out


def connect(name: str):
    """(True, link, output) when up; (False, None, output) on failure. kind = classify function."""
    if backend(name) == "ipsec":
        return connect_once_ipsec(name)
    ok, proc, out = connect_once(name)
    return ok, (FortiLink(proc) if ok else None), out


def classify_for(name: str, output: str) -> str:
    return classify_ipsec(output) if backend(name) == "ipsec" else classify(output)


def connect_once(name: str):
    """
    Start openfortivpn and wait until the tunnel is up or it fails.
    Returns (True, proc, output) when up; (False, None, output) on failure.
    """
    global _child
    conf = os.path.join(CONF_DIR, f"{name}.conf")
    _child = subprocess.Popen([OPENFORTIVPN, "-c", conf], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines: list[str] = []
    deadline = time.time() + CONNECT_TIMEOUT
    os.set_blocking(_child.stdout.fileno(), False)
    while time.time() < deadline:
        line = _child.stdout.readline()
        if line:
            lines.append(line.rstrip())
            if _UP_MARK.search(line):
                return True, _child, "\n".join(lines)
            continue
        if _child.poll() is not None:
            rest = _child.stdout.read() or ""
            lines.extend(rest.splitlines())
            return False, None, "\n".join(lines)
        time.sleep(0.2)
    _child.terminate()
    try:
        _child.wait(10)
    except Exception:
        _child.kill()
    return False, None, "\n".join(lines + ["timed out waiting for the tunnel"])


def run(name: str) -> None:
    _running_name["name"] = name
    signal.signal(signal.SIGTERM, _stop_child)
    signal.signal(signal.SIGINT, _stop_child)
    pol = Policy(_read_json(memory_path(name)))

    def remember():
        _write_json(memory_path(name), pol.snapshot(), 0o600)

    while True:
        if pol.stopped:
            write_status(name, STOPPED, pol.reason,
                         detail="automatic reconnect stopped; run: sudo ibops-vpn reset " + name)
            while _read_json(memory_path(name)).get("stopped", True):
                time.sleep(15)                         # wait for `ibops-vpn reset`
            pol = Policy(_read_json(memory_path(name)))
            continue

        write_status(name, CONNECTING, pol.reason)
        ok, link, output = connect(name)
        if not ok:
            kind = classify_for(name, output)
            state, delay, reason = pol.on_failure(kind)
            remember()
            if state == STOPPED:
                continue
            write_status(name, state, reason, time.time() + delay,
                         attempt=(f"{pol.failures} of {len(FAIL_DELAYS)}" if state == RETRYING
                                  else f"in-use wait {pol.in_use_tries}"), detail=output)
            if _wait(name, delay):
                pol = Policy(_read_json(memory_path(name)))
            continue

        up_at = time.time()
        write_status(name, UP, "", detail="", since=_now())
        state, _, _ = pol.on_connected()
        held = False
        targets = probe_targets(name)
        last_probe, probe_fails, probe_killed = time.time(), 0, False
        while link.alive():
            if not held and time.time() - up_at >= QUICK_DROP_SECONDS:
                pol.on_held(); remember(); held = True
            if targets and time.time() - last_probe >= PROBE_INTERVAL:
                last_probe = time.time()
                if probe_ok(targets):
                    probe_fails = 0
                else:
                    probe_fails += 1
                    write_status(name, UP, f"connectivity test failed {probe_fails} of "
                                 f"{PROBE_FAILS_TO_DROP} (tunnel may be dead)", since=None)
                    if probe_fails >= PROBE_FAILS_TO_DROP:
                        probe_killed = True
                        link.stop()
                        break
            time.sleep(1)
        if isinstance(link, IpsecLink):
            link.stop()                           # clear what is left (no charon re-dial)
        _link["link"] = None
        state, delay, reason = pol.on_drop(time.time() - up_at)
        if probe_killed:
            reason = ("the tunnel stopped passing traffic (connectivity test failed "
                      f"{PROBE_FAILS_TO_DROP} times); VPN restarted")
            pol.reason = reason
        remember()
        if state == STOPPED:
            continue
        write_status(name, state, reason, time.time() + delay,
                     attempt=(f"{pol.failures} of {len(FAIL_DELAYS)}" if state == RETRYING
                              else f"in-use wait {pol.in_use_tries}"))
        if _wait(name, delay):
            pol = Policy(_read_json(memory_path(name)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ibops-vpn")
    ap.add_argument("command", choices=["run", "status", "reset", "try"])
    ap.add_argument("name")
    a = ap.parse_args(argv)
    known = sorted({f[:-5] for d in (CONF_DIR, IPSEC_CONF_DIR)
                    if os.path.isdir(d) and os.access(d, os.R_OK)
                    for f in os.listdir(d) if f.endswith(".conf")})
    known = known or sorted(f[:-5] for f in os.listdir(STATUS_DIR)
                            if f.endswith(".json")) if os.path.isdir(STATUS_DIR) else known
    if a.command == "run":
        # Only systemd starts the loop. A second copy started by hand would
        # open a second tunnel with the same account -- the two would push
        # each other off.
        if not os.environ.get("INVOCATION_ID") and os.environ.get("IBOPS_VPN_ALLOW_MANUAL_RUN") != "1":
            print(f"'run' is started by the service, not by hand. Use:\n"
                  f"  sudo systemctl restart ibops-vpn@{a.name}\n"
                  f"  ibops-vpn status {a.name}")
            return 2
        run(a.name)
        return 0
    if a.command == "try":
        # One connection attempt and straight back down -- used by the IPsec
        # installer to find the gateway's settings. Refused while the service
        # runs: two tunnels with one account would push each other off.
        if backend(a.name) != "ipsec":
            print(f"'try' is for IPsec VPNs; {a.name} has no /etc/ibops-vpn/{a.name}.conf")
            return 2
        r = subprocess.run(["systemctl", "is-active", f"ibops-vpn@{a.name}"], capture_output=True, text=True)
        if r.stdout.strip() in ("active", "activating"):
            print(f"ibops-vpn@{a.name} is running; stop it first: sudo systemctl stop ibops-vpn@{a.name}")
            return 2
        ok, link, out = connect_once_ipsec(a.name)
        if ok:
            link.stop()
            print("RESULT=up")
            return 0
        print(out.strip()[-1500:])
        print(f"RESULT={classify_ipsec(out)}")
        return 1
    if a.command == "status":
        st = _read_json(status_path(a.name))
        if not st:
            print(json.dumps({"state": "unknown",
                              "hint": f"no VPN named '{a.name}'. Known: {', '.join(known) or 'none'}"},
                             indent=1))
            return 1
        print(json.dumps(st, indent=1))
        return 0
    if known and a.name not in known:
        print(f"No VPN named '{a.name}'. Known: {', '.join(known)}")
        return 1
    snap = Policy().snapshot()
    snap["reset_at"] = time.time()        # wakes a waiting service (see _wait)
    _write_json(memory_path(a.name), snap, 0o600)
    print(f"{a.name}: reset; the service will try to connect within 15 seconds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
