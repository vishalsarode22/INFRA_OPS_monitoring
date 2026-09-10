"""
Memory-dump attribution: every rung of the ladder, the rule firing through
the correlation engine, and the notifier routing to the right mailbox.

The scenarios are the ones from the design discussion: one Z report hogging,
a Z report in PRIV with dumps landing elsewhere, genuine starvation, standard
code, and the ambiguous case that must say TRIAGE rather than guess.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import dump_attribution as da
from core.models import MetricResult, Status


def _m(name, value=1.0, extra=None, status=Status.NORMAL, display=None):
    return MetricResult(name=name, value=value, display_value=display or str(value), status=status,
                        source="test", extra_data=extra or {})


def _dump(err, program, user="U1", host="app1"):
    return {"runtime_error": err, "program": program, "user": user, "host": host}


def _st22(dumps):
    return _m("screenshot_ST22", len(dumps), {"dumps": dumps})


MEM = "TSV_TNEW_PAGE_ALLOC_FAILED"


# --- ladder ------------------------------------------------------------------

def test_below_threshold_is_none():
    metrics = {"screenshot_ST22": _st22([_dump(MEM, "ZX"), _dump(MEM, "ZX")])}
    assert da.attribute(metrics) is None


def test_non_memory_dumps_do_not_count():
    dumps = [_dump("GETWA_NOT_ASSIGNED", "ZX")] * 5 + [_dump(MEM, "ZX")]
    assert da.attribute({"screenshot_ST22": _st22(dumps)}) is None


def test_rfc_only_path_has_no_classes_and_returns_none():
    metrics = {"sap.st22.dumps": _m("sap.st22.dumps", 12.0)}
    assert da.attribute(metrics) is None


def test_rung1_single_custom_hog():
    dumps = [_dump(MEM, "ZMM_REPORT", "JSMITH")] * 4 + [_dump(MEM, "SAPLMEPO", "AKUMAR")]
    v = da.attribute({"screenshot_ST22": _st22(dumps)})
    assert v.team == "ABAP" and v.verdict_type == "single_hog"
    assert v.culprit_program == "ZMM_REPORT" and v.culprit_users == ["JSMITH"]
    assert v.memory_dumps == 5 and v.distinct_programs == 2
    assert "80%" in v.evidence[1]


def test_rung2_priv_report_with_collateral():
    # Dumps land on three innocent standard programs; ZMM_BIG is in PRIV on a Z report.
    dumps = [_dump(MEM, "SAPLMEPO", "U1"), _dump(MEM, "SAPLV45A", "U2"), _dump(MEM, "SAPMF05A", "U3")]
    metrics = {
        "screenshot_ST22": _st22(dumps),
        "sap.sm50.priv_mode_wp": _m("sap.sm50.priv_mode_wp", 1.0,
                                    {"priv": [{"user": "JSMITH", "report": "ZMM_BIG", "elapsed_s": 1400,
                                               "instance": "app1_PRD_00"}]}),
    }
    v = da.attribute(metrics)
    assert v.team == "ABAP" and v.verdict_type == "hog_with_collateral"
    assert v.culprit_program == "ZMM_BIG" and v.culprit_users == ["JSMITH"]
    assert v.collateral_programs == ["SAPLMEPO", "SAPLV45A", "SAPMF05A"]
    assert "PRIV now" in " ".join(v.evidence)


def test_rung2_memory_ranking_hog_without_priv():
    dumps = [_dump(MEM, "SAPLMEPO"), _dump(MEM, "SAPLV45A"), _dump(MEM, "SAPMF05A")]
    metrics = {
        "screenshot_ST22": _st22(dumps),
        "sap.st03.dialog_resp_ms": _m("sap.st03.dialog_resp_ms", 900.0, {
            "top_reports_by_memory": [{"report": "ZFI_EXTRACT", "max_mb": 2300.0, "users": ["BJONES"]},
                                      {"report": "SAPLMEPO", "max_mb": 120.0, "users": ["U1"]}]}),
    }
    v = da.attribute(metrics)
    assert v.verdict_type == "hog_with_collateral"
    assert v.culprit_program == "ZFI_EXTRACT" and v.culprit_users == ["BJONES"]
    assert "2300.0MB" in " ".join(v.evidence)


def test_rung2_standard_report_in_priv_does_not_qualify():
    # Standard code in PRIV is not a Z hog; with three programs spread it is starvation.
    dumps = [_dump(MEM, "SAPLMEPO"), _dump(MEM, "SAPLV45A"), _dump(MEM, "SAPMF05A")]
    metrics = {"screenshot_ST22": _st22(dumps),
               "sap.sm50.priv_mode_wp": _m("sap.sm50.priv_mode_wp", 1.0,
                                           {"priv": [{"user": "U9", "report": "SAPLSTRD", "elapsed_s": 50}]})}
    assert da.attribute(metrics).verdict_type == "system_starvation"


def test_rung3_starvation():
    dumps = [_dump(MEM, p) for p in ("SAPLMEPO", "SAPLV45A", "SAPMF05A", "RSNAST00")]
    v = da.attribute({"screenshot_ST22": _st22(dumps),
                      "memory": _m("memory", 97.0, status=Status.CRITICAL, display="97%")})
    assert v.team == "BASIS" and v.verdict_type == "system_starvation"
    assert v.culprit_program == "" and "host memory=97%" in " ".join(v.evidence)


def test_rung4_standard_code_dominates():
    dumps = [_dump(MEM, "SAPLMEPO", "U1")] * 3 + [_dump(MEM, "SAPLV45A", "U2")]
    v = da.attribute({"screenshot_ST22": _st22(dumps)})
    assert v.team == "FUNCTIONAL" and v.verdict_type == "standard_code"
    assert v.culprit_program == "SAPLMEPO"


def test_rung5_triage_when_ambiguous():
    # Two programs, 50/50, both standard, nothing holding memory: two programs is
    # below the spread threshold and neither dominates.
    dumps = [_dump(MEM, "SAPLMEPO"), _dump(MEM, "SAPLMEPO"), _dump(MEM, "SAPLV45A"), _dump(MEM, "SAPLV45A")]
    v = da.attribute({"screenshot_ST22": _st22(dumps)}, {"thresholds": {"dominant_share": 0.6}})
    assert v.team == "TRIAGE" and v.verdict_type == "unclear"


def test_custom_namespace_detection():
    assert da.is_custom("ZMM_X") and da.is_custom("YFI_Y") and da.is_custom("/ACME/REPORT")
    assert not da.is_custom("SAPLMEPO") and not da.is_custom("/1BCDWB/X") and not da.is_custom("/SDF/SMON")


def test_header_round_trip():
    v = da.Verdict(team="ABAP", verdict_type="single_hog", confidence=0.9, culprit_program="ZX",
                   memory_dumps=5, distinct_programs=2)
    h = v.header()
    parsed = da.parse_header(h)
    assert parsed["owner_team"] == "ABAP" and parsed["culprit"] == "ZX"
    assert parsed["memory_dumps"] == "5" and parsed["confidence"] == "0.90"


# --- through the correlation engine ----------------------------------------

def test_rule_fires_through_engine_and_first_evidence_line_is_the_header():
    from core.correlation import get_correlation_rules
    rules = {r.rule_id: r for r in get_correlation_rules()}
    assert "SAP_MEMORY_DUMP_ATTRIBUTION" in rules, "rule not registered"
    rule = rules["SAP_MEMORY_DUMP_ATTRIBUTION"]
    dumps = [_dump(MEM, "ZMM_REPORT", "JSMITH")] * 4
    metrics = {"screenshot_ST22": _st22(dumps)}
    assert rule.matcher(metrics, [], rule.config)
    ev = rule.evidence_builder(metrics, [], rule.config)
    assert ev[0].startswith("owner_team=ABAP verdict=single_hog")
    assert any("ZMM_REPORT" in line for line in ev)


def test_rule_does_not_fire_on_rfc_only_counts():
    from core.correlation import get_correlation_rules
    rule = {r.rule_id: r for r in get_correlation_rules()}["SAP_MEMORY_DUMP_ATTRIBUTION"]
    assert not rule.matcher({"sap.st22.dumps": _m("sap.st22.dumps", 40.0)}, [], rule.config)


def test_rule_is_not_listed_as_unmatched():
    from core.correlation import unmatched_rules
    assert "SAP_MEMORY_DUMP_ATTRIBUTION" not in unmatched_rules()


# --- notifier routing -------------------------------------------------------

class _Incident:
    def __init__(self, evidence, rule_id="SAP_MEMORY_DUMP_ATTRIBUTION"):
        self.rule_id = rule_id
        self.evidence = evidence
        self.title = "Memory dump spike -- owner attributed"
        self.first_seen = datetime(2026, 9, 7, 14, 0)
        self.incident_id = "INC-TEST"


def _decoded(raw: str) -> str:
    """Headers plus every MIME part decoded, so assertions can read the HTML."""
    import email
    msg = email.message_from_string(raw)
    parts = [str(msg["Subject"])]
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            parts.append(part.get_payload(decode=True).decode("utf-8", "replace"))
    return "\n".join(parts)


def _capture(monkeypatch, module):
    sent = []
    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, frm, to, body): sent.append((to, _decoded(body)))
    monkeypatch.setattr(module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(module, "_already_sent", lambda key: False)
    monkeypatch.setattr(module, "_mark_sent", lambda key: None)
    return sent


SMTP = {"host": "h", "port": 25, "username": "u", "password": "p",
        "from_email": "ibo@company.com", "to_emails": ["basis@company.com"]}


def test_notifier_off_by_default(monkeypatch):
    from notifications import dump_owner as do
    monkeypatch.delenv("NOTIFY_DUMP_OWNERS", raising=False)
    inc = _Incident(["owner_team=ABAP verdict=single_hog confidence=0.90 culprit=ZX memory_dumps=5 programs=1"])
    assert do.notify_dump_owner("PRD", [inc], SMTP)["sent"] == 0


def test_notifier_routes_abap_and_copies_basis(monkeypatch):
    from notifications import dump_owner as do
    monkeypatch.setenv("NOTIFY_DUMP_OWNERS", "true")
    monkeypatch.setenv("DUMP_TEAM_EMAIL_ABAP", "abap@company.com, lead@company.com")
    sent = _capture(monkeypatch, do)
    inc = _Incident(["owner_team=ABAP verdict=single_hog confidence=0.90 culprit=ZMM_REPORT memory_dumps=5 programs=1",
                     "  ZMM_REPORT is customer code and accounts for 80% of the memory dumps."])
    out = do.notify_dump_owner("PRD", [inc], SMTP)
    assert out["sent"] == 1
    to, body = sent[0]
    assert to == ["abap@company.com", "lead@company.com", "basis@company.com"]
    assert "-> ABAP: ZMM_REPORT" in body and "80% of the memory dumps" in body


def test_notifier_unconfigured_team_falls_back_to_basis_and_says_so(monkeypatch):
    from notifications import dump_owner as do
    monkeypatch.setenv("NOTIFY_DUMP_OWNERS", "true")
    monkeypatch.delenv("DUMP_TEAM_EMAIL_FUNCTIONAL", raising=False)
    sent = _capture(monkeypatch, do)
    inc = _Incident(["owner_team=FUNCTIONAL verdict=standard_code confidence=0.60 culprit=SAPLMEPO memory_dumps=4 programs=2"])
    out = do.notify_dump_owner("PRD", [inc], SMTP)
    assert out["sent"] == 1 and out["unreachable"]
    assert sent[0][0] == ["basis@company.com"]


def test_notifier_triage_goes_to_basis_flagged(monkeypatch):
    from notifications import dump_owner as do
    monkeypatch.setenv("NOTIFY_DUMP_OWNERS", "true")
    sent = _capture(monkeypatch, do)
    inc = _Incident(["owner_team=TRIAGE verdict=unclear confidence=0.30 culprit=- memory_dumps=4 programs=2"])
    do.notify_dump_owner("PRD", [inc], SMTP)
    assert "UNATTRIBUTED" in sent[0][1]


def test_notifier_ignores_other_rules(monkeypatch):
    from notifications import dump_owner as do
    monkeypatch.setenv("NOTIFY_DUMP_OWNERS", "true")
    sent = _capture(monkeypatch, do)
    inc = _Incident(["owner_team=ABAP verdict=x confidence=0.9 culprit=ZX"], rule_id="SAP_DUMP_SPIKE")
    assert do.notify_dump_owner("PRD", [inc], SMTP)["sent"] == 0 and not sent
