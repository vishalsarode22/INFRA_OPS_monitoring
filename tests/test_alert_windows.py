"""28 Oct 2026: the time windows of timed alerts are set on the Thresholds page."""
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from core import config_editor, alert_windows, timeline
    monkeypatch.setattr(config_editor, "config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(config_editor, "backup", lambda name: None, raising=False)
    monkeypatch.setattr(timeline, "add", lambda *a, **k: None)
    alert_windows._cache["stamp"] = None
    return alert_windows


def test_system_beats_standard_beats_default(cfg):
    k = "sap.st22.user_dumps_10min"
    assert cfg.minutes("PS4", k) == 15
    cfg.save("", k, 20)
    assert cfg.minutes("PS4", k) == 20 and cfg.label("PS4", k) == "ABAP dumps by one user (20 min)"
    cfg.save("PS4", k, 30)
    assert cfg.minutes("PS4", k) == 30 and cfg.minutes("CEQ", k) == 20
    cfg.save("PS4", k, None)
    assert cfg.minutes("PS4", k) == 20
    with pytest.raises(ValueError):
        cfg.save("", k, 0)
    with pytest.raises(ValueError):
        cfg.save("", "sap.cpu", 5)


def test_cancelled_jobs_use_the_window(cfg, monkeypatch):
    from core import custom_alerts as ca
    now = datetime(2026, 10, 28, 10, 0)
    raw = {"sap_now": now.strftime("%Y-%m-%d %H:%M:%S"),
           "cancelled": {"rows": [{"user": "OM", "job": "Z1", "end": (now - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")},
                                  {"user": "OM", "job": "Z2", "end": (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")}]}}
    monkeypatch.setattr(ca, "_seen_last_read", lambda *a: None, raising=False)

    def summary():
        items = [i for i in ca.values("PS4", raw, now=now) if i["key"] == "sap.sm37.user_cancelled_15min"]
        return next(i for i in items if i.get("summary"))
    assert summary()["value"] == 1                     # 15 min: only Z2
    cfg.save("PS4", "sap.sm37.user_cancelled_15min", 30)
    s = summary()
    assert s["value"] == 2 and s["label"] == "Cancelled jobs by one user (30 min)" and "30 minutes" in s["detail"]
