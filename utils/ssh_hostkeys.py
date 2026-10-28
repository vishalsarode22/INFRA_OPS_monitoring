"""
SSH with pinned host keys ("trust on first use").

paramiko.AutoAddPolicy accepted whatever key a host presented, every time,
so anyone who could sit between the monitoring host and the SAP server
could present their own key and collect the SSH password. Now:

  * known keys live in config/ssh_known_hosts (OpenSSH format, so
    `ssh-keyscan -p 22 host >> config/ssh_known_hosts` pre-seeds it);
  * a host seen for the FIRST time is recorded and logged -- the only
    unverified moment, which is why pre-seeding is better;
  * a host whose key CHANGES is refused with a clear error. If the change is
    legitimate (server rebuilt), delete its line from the file.
"""

from __future__ import annotations

import os
import threading

import paramiko

from utils.logger import get_logger

log = get_logger(__name__, "monitoring")

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
KNOWN_HOSTS = os.path.join(_ROOT, "config", "ssh_known_hosts")
_lock = threading.Lock()


class _RecordFirstKey(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        log.warning(f"SSH: first contact with {hostname}; recording its "
                    f"{key.get_name()} key ({key.get_base64()[:16]}...) in "
                    f"{KNOWN_HOSTS}. Pre-seed with ssh-keyscan to avoid this.")
        client.get_host_keys().add(hostname, key.get_name(), key)
        with _lock:
            os.makedirs(os.path.dirname(KNOWN_HOSTS), exist_ok=True)
            client.save_host_keys(KNOWN_HOSTS)


def connect_pinned(host: str, port: int, username: str, password: str,
                   timeout: int = 10) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    if os.path.isfile(KNOWN_HOSTS):
        client.load_host_keys(KNOWN_HOSTS)
    client.set_missing_host_key_policy(_RecordFirstKey())
    try:
        client.connect(hostname=host, port=port, username=username,
                       password=password, timeout=timeout,
                       allow_agent=False, look_for_keys=False)
    except paramiko.BadHostKeyException as exc:
        raise RuntimeError(
            f"SSH host key for {host} does not match {KNOWN_HOSTS}. "
            f"Refusing to send the password. If the server was legitimately "
            f"rebuilt, remove its line from that file.") from exc
    return client
