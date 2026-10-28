"""One scheduler per machine; the sign-in form is not refused as cross-site."""
import os
import subprocess
import sys
import textwrap

import pytest


def test_second_process_cannot_take_the_scheduler_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_RUNTIME_DIR", str(tmp_path))
    from core import instance_lock as L
    assert L.acquire("scheduler") and L.acquire("scheduler")      # re-entrant in-process
    code = textwrap.dedent(f"""
        import os, sys
        os.environ["IBO_RUNTIME_DIR"] = {str(tmp_path)!r}
        sys.path.insert(0, {os.getcwd()!r})
        from core import instance_lock as L
        print("GOT" if L.acquire("scheduler") else "STANDBY")
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().endswith("STANDBY"), out.stderr
    L.release("scheduler")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().endswith("GOT"), out.stderr


class _Req:
    def __init__(self, **h):
        self.headers = {k.replace("_", "-"): v for k, v in h.items()}


def test_same_origin_rules():
    pytest.importorskip("fastapi")
    import dashboard.app as A
    host = "127.0.0.1:8000"
    assert A._same_origin(_Req(host=host, origin="http://127.0.0.1:8000"))
    # Old tab served with no-referrer: Origin null, but the browser says same-origin.
    assert A._same_origin(_Req(host=host, origin="null", sec_fetch_site="same-origin"))
    assert not A._same_origin(_Req(host=host, origin="null"))
    assert not A._same_origin(_Req(host=host, origin="https://evil.example"))
    assert not A._same_origin(_Req(host=host, origin="http://127.0.0.1:8000", sec_fetch_site="cross-site"))
