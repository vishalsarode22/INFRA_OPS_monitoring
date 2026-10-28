"""Alert history and the Monitoring log show times in IST, labelled, whatever zone the VM is in."""
from datetime import datetime, timedelta, timezone

import pytest

from utils import display_time as D


def test_vm_local_stamp_is_shown_in_ist(monkeypatch):
    # Pretend the VM clock is UTC: 10:55:24 UTC is 04:25:24 PM IST.
    utc = timezone.utc
    real = datetime.astimezone

    class FakeDT(datetime):
        def astimezone(self, tz=None):
            if tz is None and self.tzinfo is None:
                return self.replace(tzinfo=utc)
            return real(self, tz)

    monkeypatch.setattr(D, "parse_stamp", lambda s: FakeDT(2026, 10, 23, 10, 55, 24))
    assert D.to_display("2026-10-23 10:55:24") == "23 Oct 2026, 04:25:24 PM IST"


def test_ist_vm_clock_is_unchanged_just_labelled():
    ist = timezone(timedelta(hours=5, minutes=30))
    aware = datetime(2026, 10, 23, 16, 25, 24, tzinfo=ist)
    assert D.to_display(aware) == "23 Oct 2026, 04:25:24 PM IST"
    assert D.to_display(aware, with_date=False) == "04:25:24 PM IST"


def test_empty_and_unparsable():
    assert D.to_display("") == "" and D.to_display(None) == ""
    assert D.to_display("yesterday") == "yesterday"


def test_other_offset_can_be_configured(monkeypatch):
    monkeypatch.setenv("IBO_DISPLAY_TZ_OFFSET", "+04:00")
    monkeypatch.setenv("IBO_DISPLAY_TZ_NAME", "GST")
    aware = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
    assert D.to_display(aware) == "23 Oct 2026, 04:00:00 PM GST"


def test_alert_history_api_adds_ist_time(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import dashboard.app as A
    from core import alert_engine
    monkeypatch.setattr(alert_engine, "history",
                        lambda *a, **k: [{"at": "2026-10-23 16:25:24", "system": "CEAT S4P", "recipients": "x"}])
    r = TestClient(A.app).get("/api/ops/alerts?days=1")
    assert r.status_code == 200
    row = r.json()["rows"][0]
    assert row["at"] == "2026-10-23 16:25:24" and row["at_display"].endswith(" IST")
