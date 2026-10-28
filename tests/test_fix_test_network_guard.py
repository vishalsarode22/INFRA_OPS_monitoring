"""The test suite must never reach a real mail server, Gemini or SAP host."""
import socket

import pytest


def test_non_loopback_connections_are_blocked_in_tests():
    with pytest.raises(OSError, match="network is blocked in tests"):
        socket.create_connection(("smtp.gmail.com", 587), timeout=1)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="network is blocked in tests"):
            s.connect(("10.250.20.150", 3300))
    finally:
        s.close()


def test_loopback_still_works():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=2)
        c.close()
    finally:
        srv.close()


def test_smtp_send_in_a_test_cannot_leave_the_machine():
    import smtplib
    with pytest.raises(OSError):
        smtplib.SMTP("smtp.gmail.com", 587, timeout=2)
