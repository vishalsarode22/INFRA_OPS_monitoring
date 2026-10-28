"""The plain sheet wording (reporting/plain_words.py, on by default) must keep
every fact the base wording had -- found by the TADA pass: on-hold work
processes, an unread value, a running backup and a WARNING explanation were
being dropped."""
from reporting import plain_words as P


def test_sm66_counts_on_hold_processes_as_busy():
    d = {"running_processes": 6, "on_hold_processes": 11, "active_processes": 17,
         "waiting_processes": 40, "priv_mode_processes": 1}
    t = P.actual("SM66", d, "WARNING", "")
    assert t.startswith("17 busy (6 running, 11 on hold), 40 free") and "1 in PRIV mode" in t


def test_sm66_says_when_free_processes_were_not_read():
    t = P.actual("SM66", {"running_processes": 6, "waiting_processes": None}, "OK", "")
    assert "free processes not read" in t


def test_db12_keeps_the_running_backup():
    d = {"latest_backup": {"end_time": "21.09.2026 14:47:58", "status": "successful"},
         "running_backup": {"start_time": "22.09.2026 13:46:19"}, "finished_at": "2026-09-22T13:50:00"}
    t = P.actual("DB12", d, "OK", "")
    assert "21.09.2026 14:47" in t and "running since 22.09.2026 13:46" in t


def test_warning_row_never_has_an_empty_explanation():
    base = "Outbound mail cannot leave the system: no SMTP node is active."
    assert P.note("SCOT", {"smtp_nodes_configured": 2}, "WARNING", base) == base
    assert P.note("SCOT", {"smtp_nodes_configured": 2}, "OK", "All within limits") == ""


def test_single_sm21_error_keeps_time_and_user():
    d = {"error_entries": [{"id": "R49", "time": "16:30:21", "user": "65142",
                            "text": "Communication error, CPIC-RC=CM_PARAMETER_ERROR(19)"}]}
    t = P.note("SM21", d, "WARNING", "")
    assert "R49" in t and "16:30:21" in t and "65142" in t and "Communication error" in t


def test_whole_number_milliseconds_have_no_decimal():
    t = P.actual("SMLG", {"instances": [{"instance": "ci_00", "response_time_ms": 893.0}]}, "OK", "")
    assert t == "ci_00 893 ms"
