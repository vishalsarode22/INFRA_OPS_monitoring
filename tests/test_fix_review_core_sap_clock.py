"""
Review fix 7: core.sap_clock uses collectors.sap_clock only when it has
learned the system's offset; otherwise the stored offset (custom checks)
applies, then the VM clock.
"""
from datetime import datetime, timedelta

import pytest

from collectors import sap_clock as rfc_clock
from core import custom_alerts
from core import sap_clock


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    sap_clock.set_provider(None)
    rfc_clock.forget()
    yield
    rfc_clock.forget()
    sap_clock.set_provider(None)


def test_stored_offset_used_when_collector_clock_not_learned(monkeypatch):
    monkeypatch.setattr(custom_alerts, "_sap_clock", lambda system: -5 * 3600)
    got = sap_clock.sap_now("PRD")
    expected = datetime.now() - timedelta(hours=5)
    assert abs((got - expected).total_seconds()) < 5


def test_learned_collector_clock_wins(monkeypatch):
    monkeypatch.setattr(custom_alerts, "_sap_clock", lambda system: -5 * 3600)
    rfc_clock.learn_offset("PRD", 3 * 3600)
    got = sap_clock.sap_now("PRD")
    expected = datetime.now() + timedelta(hours=3)
    assert abs((got - expected).total_seconds()) < 5


def test_vm_clock_when_nothing_known(monkeypatch):
    monkeypatch.setattr(custom_alerts, "_sap_clock", lambda system: 0)
    assert abs((sap_clock.sap_now("PRD") - datetime.now()).total_seconds()) < 5
