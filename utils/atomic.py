"""
Crash-safe file writes -- the ONE helper every config/state writer uses.

A plain open(path, "w") + write truncates the file first and fills it
afterwards. A crash, a Defender scan or a power cut in between leaves an
empty or half-written file -- and a reader that parses it at that moment
(the scheduler polls every 20 s) gets {} or a YAML error. Writing to a
sibling temp file, fsync-ing it and os.replace()-ing it is atomic on both
NTFS and POSIX: readers see either the old file or the new one, never a
partial one.

    atomic_write_text(path, text)
    atomic_write_yaml(path, data, header="# comment\\n")
    atomic_write_json_file(path, data)      # always a file
    atomic_write_json(path, data)           # run-time state: the store unless
                                            # IBO_STATE_BACKEND=files

WINDOWS: os.replace() raises PermissionError while another process (an
antivirus scan, the search indexer, Notepad, the other IBOPS process reading
the file) briefly holds the target open. That is transient, so the replace
is retried with a short back-off before giving up.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

_REPLACE_ATTEMPTS = 12        # ~3 s in total with the back-off below


def replace_with_retry(src: str, dst: str, attempts: int = _REPLACE_ATTEMPTS) -> None:
    """os.replace(), retried on PermissionError (Windows sharing violations)."""
    delay = 0.02
    for attempt in range(1, max(1, attempts) + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def replace_or_overwrite(src: str, dst: str, attempts: int = 20) -> None:
    """
    Put `src` in place of `dst`: an atomic os.replace() when Windows allows it,
    else overwrite `dst` in place.

    WINDOWS: os.replace() onto a file fails with "Access is denied" (WinError 5)
    for as long as ANY process has that file open -- Python's open() does not
    share delete access, and every IBOPS process (dashboard, RFC workers,
    sweeps) reads systems.yaml. Overwriting in place is allowed while others
    have it open for reading, which is what the old code did. A reader that
    catches the file half-written falls back to its last good parse.
    A read-only file is reported as such instead of being forced.
    """
    import shutil
    try:
        replace_with_retry(src, dst, attempts)
        return
    except PermissionError as exc:
        if not os.path.exists(dst):
            raise
        if not os.access(dst, os.W_OK):
            raise PermissionError(f"{dst} is read-only (file attribute); clear it to save changes") from exc
        shutil.copyfile(src, dst)          # in place: allowed while others read it
        try:
            os.unlink(src)
        except OSError:
            pass
        try:
            from utils.logger import get_logger
            get_logger(__name__).warning(f"{os.path.basename(dst)} was in use; written in place instead of swapped")
        except Exception:  # noqa: BLE001
            pass


def atomic_write_text(path: str, text: str, encoding: str = "utf-8", newline: str | None = None) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=directory)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Keep the existing file's permissions (mkstemp creates 0600).
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass
        replace_or_overwrite(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_yaml(path: str, data: Any, header: str = "", **dump_kwargs) -> None:
    """yaml.safe_dump into `path` atomically, optionally after a comment header."""
    import yaml
    opts = {"default_flow_style": False, "sort_keys": False, "allow_unicode": True}
    opts.update(dump_kwargs)
    atomic_write_text(path, (header or "") + yaml.safe_dump(data, **opts))


def atomic_write_json_file(path: str, data: Any, indent: int = 2, **kwargs) -> None:
    """JSON into a FILE atomically, whatever IBO_STATE_BACKEND says."""
    kwargs.setdefault("default", str)
    atomic_write_text(path, json.dumps(data, indent=indent, **kwargs))


def _store_backend() -> bool:
    """
    Run-time JSON state lives in the SQLite store (core/store.py) unless
    IBO_STATE_BACKEND=files. Escalation state, schedules, owner-mail
    de-duplication, snapshots and profile state all write through here, so
    this one switch moves them into the database with the rest.
    """
    return (os.getenv("IBO_STATE_BACKEND", "sqlite") or "sqlite").strip().lower() != "files"


def atomic_write_json(path: str, data: Any, indent: int = 2, **kwargs) -> None:
    if _store_backend():
        from core import store
        store.put_document(path, data)
        return
    atomic_write_text(path, json.dumps(data, indent=indent, **kwargs))


def read_json(path: str, default: Any = None) -> Any:
    """json.load that treats a missing OR corrupt file as `default`."""
    if _store_backend():
        from core import store
        return store.get_document(path, default)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default
