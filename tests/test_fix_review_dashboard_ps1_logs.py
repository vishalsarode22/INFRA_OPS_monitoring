"""Review fix 8d: start_dashboard_hidden.ps1 keeps the previous run's logs (rotated to .1), CRLF kept."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _raw():
    with open(os.path.join(ROOT, "start_dashboard_hidden.ps1"), "rb") as fh:
        return fh.read()


def test_logs_rotated_before_start_process():
    text = _raw().decode("utf-8")
    rot = text.index('Move-Item -Force -Path $f -Destination "$f.1"')
    start = text.index("Start-Process -WindowStyle Hidden")
    assert rot < start
    assert '"dashboard_stdout.log", "dashboard_stderr.log"' in text


def test_crlf_line_endings():
    raw = _raw()
    assert raw.count(b"\n") == raw.count(b"\r\n") > 0
