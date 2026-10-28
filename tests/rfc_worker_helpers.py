"""Top-level functions run inside RFC worker processes by test_fix_rfc_workers_per_system.py
(spawned processes import them by module name, so they cannot live in the test file)."""
import os
import time


def add(a, b):
    return a + b


def pid():
    return os.getpid()


def crash():
    os._exit(3)                       # like a native crash: no reply, process gone


def crash_once(marker):
    if not os.path.exists(marker):
        open(marker, "w").close()
        os._exit(3)
    return "recovered"


def hang(seconds):
    time.sleep(seconds)
    return "late"


class OddError(Exception):
    """Like pyrfc's errors: needs extra constructor arguments, so it does not unpickle."""
    def __init__(self, code, key, message):
        super().__init__(message)
        self.code, self.key = code, key


def raise_odd():
    raise OddError(1, "RFC_ERROR_LOGON_FAILURE", "Name or password is incorrect")


def raise_value():
    raise ValueError("bad table")


def spawn_worker_then_die(runtime_dir, pidfile):
    """Child 'dashboard': start a worker stuck in a long read, write its pid, then die abruptly."""
    import os
    import threading
    os.environ["IBO_RUNTIME_DIR"] = runtime_dir
    from core import rfc_workers as W
    threading.Thread(target=lambda: W.run("HUNG", hang, (600,), timeout=900), daemon=True).start()
    import time as _t
    for _ in range(100):
        ws = W.status()
        if ws and ws[0]["pid"]:
            open(pidfile, "w").write(str(ws[0]["pid"]))
            break
        _t.sleep(0.1)
    _t.sleep(0.5)
    os._exit(9)
