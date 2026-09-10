"""
Regression tests for the September patch.

Both bugs covered here were silent: the dump one was swallowed by an
`except Exception` and showed as an empty panel under a correct count, and
the response one showed as a plausible-looking number that happened to be
nonsense. Neither would have been caught by "does it crash" testing, so
they get explicit tests.
"""

from collections import Counter

import pytest

from collectors import ccms_os
from collectors import rfc_perf as rp


# ---------------------------------------------------------------------------
# SNAP -> per-user dump breakdown
# ---------------------------------------------------------------------------

RFC_READ_TABLE_REPLY = {
    "FIELDS": [{"FIELDNAME": f} for f in
               ["DATUM", "UZEIT", "AHOST", "UNAME", "MANDT", "MODNO", "SEQNO"]],
    "DATA": [
        {"WA": "20260909|104312|srlprdap1|3249|500|001|000"},
        {"WA": "20260909|104515|srlprdap1|3249|500|002|000"},
        {"WA": "20260909|104515|srlprdap1|3249|500|002|001"},   # continuation row
        {"WA": "20260909|105001|srlprdap2|BATCHUSR|500|003|000"},
    ],
}


def _parse(reply):
    """The parsing _dumps() now does."""
    order = [str(f.get("FIELDNAME", "")).strip() for f in reply.get("FIELDS", [])]
    return [dict(zip(order, r["WA"].split("|"))) for r in reply.get("DATA", [])]


def test_old_parsing_could_not_work():
    """
    Guards the diagnosis, not the fix. The previous code produced a list of
    lists and every reader called .get() on it, so the breakdown could never
    have been populated on any system, on any release.
    """
    rows = [r["WA"].split("|") for r in RFC_READ_TABLE_REPLY["DATA"]]
    with pytest.raises(AttributeError):
        rows[0].get("UNAME")


def test_dump_rows_parse_into_fields():
    rows = _parse(RFC_READ_TABLE_REPLY)
    assert rows[0]["UNAME"] == "3249"
    assert rows[0]["AHOST"] == "srlprdap1"
    assert rows[3]["UNAME"] == "BATCHUSR"


def test_continuation_rows_are_not_counted_as_dumps():
    rows = _parse(RFC_READ_TABLE_REPLY)
    heads = [r for r in rows if r["SEQNO"].lstrip("0") == ""]
    assert len(heads) == 3


def test_per_user_and_per_host_breakdowns_populate():
    rows = _parse(RFC_READ_TABLE_REPLY)
    heads = [r for r in rows if r["SEQNO"].lstrip("0") == ""]
    assert Counter(r["UNAME"] for r in heads) == {"3249": 2, "BATCHUSR": 1}
    assert Counter(r["AHOST"] for r in heads) == {"srlprdap1": 2, "srlprdap2": 1}


def test_field_order_follows_the_reply_not_the_request():
    """RFC_READ_TABLE returns columns in DDIC order, which need not match
    the order they were asked for. Zipping against the request would put
    every value in the wrong column, silently."""
    reply = {
        "FIELDS": [{"FIELDNAME": f} for f in ["UNAME", "DATUM", "SEQNO"]],
        "DATA": [{"WA": "BATCHUSR|20260909|000"}],
    }
    assert _parse(reply)[0]["UNAME"] == "BATCHUSR"


# ---------------------------------------------------------------------------
# Dialog response statistic
# ---------------------------------------------------------------------------

def _step(resp_ms, user="3249", instance="srlprdap1_DB1_00"):
    return {"TASKTYPE": b"\x01", "RESPTI": resp_ms, "ACCOUNT": user,
            "REPORT": "SAPMSSY1", "TCODE": "", "_instance": instance,
            "DBREQTIME": 0, "DBPREQTIME": 0, "MAXBYTES": 0, "DSQLCNT": 0}


# Ten quick steps and three stuck behind a lock: the shape that produced
# "464549 ms" on a system with no users and 70 free work processes.
SKEWED = ([_step(v) for v in (42, 51, 38, 61, 47, 55, 39, 44, 58, 49)]
          + [_step(v, "BATCHUSR") for v in (1_800_000, 2_100_000, 1_950_000)])


def test_median_reports_the_typical_step():
    rs = rp.response_summary(SKEWED, "TEST")
    assert rs["avg_resp_ms"] < 200


def test_mean_is_still_reported_so_the_tail_is_not_hidden():
    rs = rp.response_summary(SKEWED, "TEST")
    assert rs["mean_resp_ms"] > 400_000
    assert rs["max_resp_ms"] == 2_100_000
    assert rs["p95_resp_ms"] > rs["avg_resp_ms"]


def test_skew_is_detectable_by_the_metric_builder():
    """build_metrics() annotates the detail line when mean >> median. That
    rule needs both figures present and correctly ordered."""
    rs = rp.response_summary(SKEWED, "TEST")
    assert rs["mean_resp_ms"] > rs["avg_resp_ms"] * 5


def test_slowest_steps_name_the_contributor():
    rs = rp.response_summary(SKEWED, "TEST")
    worst = rs["slowest_steps"][0]
    assert worst["resp_ms"] == 2_100_000
    assert worst["user"] == "BATCHUSR"


def test_a_genuinely_slow_system_is_not_flattened():
    """The median must not turn a real problem green. Every step slow means
    the median is slow too."""
    slow = [_step(v, "U1") for v in
            (3200, 3400, 3100, 3600, 3300, 3500, 3250, 3450, 3150, 3550, 3350, 3400)]
    rs = rp.response_summary(slow, "TEST")
    assert rs["avg_resp_ms"] > 3000


def test_per_user_rows_separate_the_slow_user_from_the_fast_one():
    rs = rp.response_summary(SKEWED, "TEST")
    users = {u["user"]: u for u in rs["top_users_by_total_ms"]}
    assert users["3249"]["steps"] == 10
    assert users["3249"]["median_ms"] < 100
    assert users["BATCHUSR"]["steps"] == 3
    assert users["BATCHUSR"]["median_ms"] > 1_000_000


def test_per_user_rows_are_ordered_by_median_not_by_total():
    """Sorting by summed response ranks the busiest user, not the slowest
    one, which is how a user running many fast steps outranked the one
    actually stuck."""
    rs = rp.response_summary(SKEWED, "TEST")
    assert rs["top_users_by_total_ms"][0]["user"] == "BATCHUSR"


# ---------------------------------------------------------------------------
# CCMS role matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("node,role", [
    ("Physical Mem Free", "mem_free"),
    ("Free Memory", "mem_free"),
    ("Available Memory", "mem_free"),
    ("Physical Mem Configured", "mem_total"),
    ("Total Physical Memory", "mem_total"),
    ("Installed Memory", "mem_total"),
    ("Memory Utilization", "mem_used_pct"),
    ("CPU_Utilization", "cpu"),
    ("5minLoadAverage", "load_5m"),
])
def test_ccms_node_names_map_to_roles(node, role):
    assert ccms_os._role_for(node) == role


def test_direct_percentage_wins_over_derivation():
    """Where saposcol publishes a utilisation percentage there is no
    denominator to get wrong, so it must take precedence."""
    assert ccms_os._role_for("Memory Utilization") == "mem_used_pct"
