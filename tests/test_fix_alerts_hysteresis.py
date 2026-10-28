"""
Finding 1: no hysteresis. SM58 "entries displayed" flapped WARNING / NORMAL /
CRITICAL / NORMAL and every transition mailed. Now: raise after N
consecutive breaching readings, clear after M readings below 80 % of the
limit, no recovery mail for an alert shorter than 30 min; grading YELLOW ->
ORANGE / RED. Settings per metric / system in the thresholds files.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_fix_alerts import fx  # noqa: E402,F401

KEY = "sap.sm58.entries_today"


def _on(fx, key=KEY, level="warning", system="PS4", **extra):
    fx.T.save_override(system, key, None, None, alert=level, channels=["email"])
    fx.T._cache.clear()


def sm58(fx, status, value):
    return fx.feed(status, value, key=KEY, warning=100, critical=250)


def test_flapping_queue_size_sends_nothing(fx):
    _on(fx)
    for status, v in [("WARNING", 120), ("NORMAL", 60), ("CRITICAL", 260), ("NORMAL", 70),
                      ("WARNING", 110), ("NORMAL", 90), ("CRITICAL", 255), ("NORMAL", 95)]:
        sm58(fx, status, v)
        fx.clock.tick(3)
    assert fx.sent == []                       # never 3 breaching readings in a row


def test_raise_after_three_then_only_one_mail(fx):
    _on(fx)
    sm58(fx, "WARNING", 120); fx.clock.tick(3)
    sm58(fx, "WARNING", 130); fx.clock.tick(3)
    assert fx.sent == []
    sm58(fx, "WARNING", 140)
    assert [(j["kind"], j["level"], j["grade"]) for j in fx.sent] == [("alert", "WARNING", "YELLOW")]
    fx.clock.tick(3); sm58(fx, "WARNING", 150)
    assert len(fx.sent) == 1


def test_band_between_clear_level_and_limit_keeps_alert_and_clears_after_three_low(fx):
    _on(fx)
    for v in (120, 130, 140):
        sm58(fx, "WARNING", v); fx.clock.tick(15)
    assert len(fx.sent) == 1
    # 81..99: below the warning limit but above 80 % of it -> still raised, silent
    for v in (95, 85, 99, 81):
        sm58(fx, "NORMAL", v); fx.clock.tick(15)
    assert len(fx.sent) == 1
    # three readings at or below 80 -> cleared; alert lasted > 30 min -> recovery mail
    for v in (80, 40, 30):
        sm58(fx, "NORMAL", v); fx.clock.tick(15)
    assert [j["kind"] for j in fx.sent] == ["alert", "recovery"]


def test_short_alert_gets_no_recovery_mail_but_a_history_row(fx):
    _on(fx)
    for v in (120, 130, 140):
        sm58(fx, "WARNING", v); fx.clock.tick(1)
    for v in (10, 10, 10):
        sm58(fx, "NORMAL", v); fx.clock.tick(1)
    assert [j["kind"] for j in fx.sent] == ["alert"]
    rows = fx.E.history("PS4")
    assert any(r["kind"] == "recovery" and "no recovery mail" in r["note"] for r in rows)


def test_second_breach_within_window_goes_to_the_manager(fx):
    _on(fx)
    for v in (120, 130, 140):
        sm58(fx, "WARNING", v); fx.clock.tick(1)
    for v in (10, 10, 10):
        sm58(fx, "NORMAL", v); fx.clock.tick(1)
    fx.clock.tick(30)                            # well inside the 2 h window
    for v in (120, 130, 140):
        sm58(fx, "WARNING", v); fx.clock.tick(1)
    assert [j["grade"] for j in fx.sent if j["kind"] == "alert"] == ["YELLOW", "ORANGE"]


def test_breach_after_window_starts_yellow_again(fx):
    _on(fx)
    for v in (120, 130, 140):
        sm58(fx, "WARNING", v); fx.clock.tick(1)
    for v in (10, 10, 10):
        sm58(fx, "NORMAL", v); fx.clock.tick(1)
    fx.clock.tick(180)
    for v in (120, 130, 140):
        sm58(fx, "WARNING", v); fx.clock.tick(1)
    assert [j["grade"] for j in fx.sent if j["kind"] == "alert"] == ["YELLOW", "YELLOW"]


def test_still_breached_after_repeat_time_is_escalated(fx):
    _on(fx, level="critical")
    for _ in range(3):
        sm58(fx, "CRITICAL", 300); fx.clock.tick(1)
    fx.clock.tick(61)                            # critical repeat is 60 min
    sm58(fx, "CRITICAL", 300)
    assert [(j["kind"], j["grade"]) for j in fx.sent] == [("alert", "YELLOW"), ("repeat", "RED")]


def test_per_metric_settings_in_the_thresholds_file_and_kept_by_a_page_save(fx):
    (fx.tmp / "thresholds_systems.yaml").write_text(
        "systems:\n  PS4:\n    sap.sm58.entries_today:\n      alert: warning\n      channels: [email]\n"
        "      raise_after: 1\n      clear_after: 2\n      clear_pct: 50\n"
        "      recovery_min_minutes: 0\n", encoding="utf-8")
    fx.T._cache.clear()
    h = fx.E.hysteresis("PS4", KEY)
    assert (h["raise_after"], h["clear_after"], h["clear_pct"], h["recovery_min_minutes"]) == (1, 2, 50, 0)
    sm58(fx, "WARNING", 120)
    assert len(fx.sent) == 1                     # raise_after 1
    sm58(fx, "NORMAL", 60)                       # 60 > 50 % of 100: not clear
    sm58(fx, "NORMAL", 40); sm58(fx, "NORMAL", 40)
    assert [j["kind"] for j in fx.sent] == ["alert", "recovery"]
    # the Thresholds page edits limits only: the hysteresis keys survive a save
    fx.T.save_override("PS4", KEY, 100, 260)
    fx.T._cache.clear()
    assert fx.E.hysteresis("PS4", KEY)["clear_pct"] == 50


def test_env_defaults_and_event_counts(fx, monkeypatch):
    monkeypatch.setenv("IBO_ALERT_RAISE_AFTER", "2")
    assert fx.E.hysteresis("PS4", "cpu")["raise_after"] == 2
    # a per-window event count and a sweep-only value alert on the first reading
    assert fx.E.hysteresis("PS4", "sap.st22.user_dumps_10min")["raise_after"] == 1
    assert fx.E.hysteresis("PS4", "sap.sm21.errors", source="sweep")["raise_after"] == 1
