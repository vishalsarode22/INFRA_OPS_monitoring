"""27 Oct 2026: RCA and alert mails in plain words, names highlighted."""
from evaluation.rca_pipeline import _rules_verdict
from notifications import mail_style as ms, plain_rca as pr
from notifications.rca_email import build_rca_html, plain_subject

CUL = [
    {"type": "user", "name": "CPIUSER", "evidence": "STAD 30 min: 13 steps, total 1.2 s, worst 296 ms, RFC",
     "impact": "highest total response time in the window"},
    {"type": "lock", "name": "BATCH_USER on /SDF/SMON_CALL_KEY",
     "evidence": "SM12: lock 613 min old, argument xyz", "impact": "stale lock -- failed update"},
    {"type": "workprocess", "name": "APC_00 dialog",
     "evidence": "ST03N 09:58-10:13 on APC_00: avg dialog 1300 ms over 60 steps; DB 1%, CPU 37%, wait 0 ms, roll 483 ms",
     "impact": "the instance is slow for every dialog user in this window"},
]
TRIG = "instance response (5-min STAT average) at or over the critical limit 2000 ms: APC_00 2948 ms"


def test_a_short_step_is_not_named_the_root_cause():
    facts = {"STAD": {"top_by_response": [{"user": "CPIUSER", "programs": ["RFC"], "worst_ms": 296,
                                           "cpu_ms": 320, "db_ms": 228}]},
             "ST03N": {"instance": "APC_00", "breakdown": {"resp_ms": 1300, "gui_ms": 566, "roll_ms": 483}}}
    sev, root, lead = _rules_verdict(facts, CUL)
    assert lead is None and "GUI time" in root and "No single user's work explains it" in root
    facts["STAD"]["top_by_response"][0]["worst_ms"] = 45000          # a 45 s step does explain it
    assert _rules_verdict(facts, CUL)[1].startswith("CPIUSER ran RFC as a single step of 45.0 s")


def test_rca_mail_is_plain_and_highlights_who():
    an = {"severity": "WARNING", "culprits": CUL, "root_cause": "x by CPIUSER", "confidence": "low",
          "confidence_reason": "model call failed (RuntimeError); rules-based fallback",
          "actions_now": [{"step": "1 lock(s) older than 60 min: check SM13 for failed updates before deleting",
                           "tcode": "SM12 / SM13"}]}
    html = build_rca_html(an, TRIG, "CAR PRD")
    assert "waited <b>2.9 seconds</b>" in html and "10 h 13 min" in html
    assert ms.chip("CPIUSER", "user") in html and ms.chip("/SDF/SMON_CALL_KEY", "table") in html
    assert "STAD 30 min" not in html and "held for more than an hour" in html and "AI was not available" in html
    assert plain_subject("[RCA]", "CAR PRD", an, TRIG) == "[RCA] CAR PRD -- WARNING: slow screens (2.9 s), look at CPIUSER"


def test_unknown_wording_is_kept_not_lost():
    p = pr.culprit({"type": "job", "name": "ZJOB", "evidence": "something new", "impact": "odd"})
    assert p["kind"] == "JOB" and "something new" in p["what"] and p["why"] == "odd"


def test_buttons_go_inside_the_card():
    page = ms.page("WARNING", "x", "t", "lead")
    out = ms.insert(page, "<b>BUTTONS</b>")
    assert out.index("BUTTONS") < out.index("</table>")


def test_value_alert_and_back_to_normal_read_plainly():
    from core import alert_engine as E
    s, h, t = E.render_email({"system": "PS4", "metric": "m", "instance": "", "label": "ABAP dumps today",
                              "level": "WARNING", "kind": "alert", "value": "7", "grade": "YELLOW",
                              "limits": "warning ≥ 5, critical ≥ 10"})
    assert s == "[InfraBeatOps] WARNING PS4: ABAP dumps today = 7"
    assert "over the warning limit of 5; it becomes critical at 10" in h and "only the Basis team" in h
    s, h, t = E.render_email({"system": "PS4", "metric": "m", "instance": "", "label": "ABAP dumps today",
                              "level": "NORMAL", "kind": "recovery", "value": "0",
                              "limits": "warning ≥ 5, critical ≥ 10"})
    assert "Nothing to do" in h and "Program crashes" not in h and "IST" in h      # short
    assert E.tidy_detail("IB_K, IB_K, IB_K, 65675, 65675 -- via X") == "IB_K (3), 65675 (2) -- via X"


def test_change_list_groups_transports():
    from core.change_tracking import summary_lines
    rows = [{"at": "2026-10-26 16:58:00", "title": f"transport DS4K91{i} imported (RC 0)"} for i in range(6)]
    rows.append({"at": "2026-10-26 16:58:00", "title": "transport ALL imported (RC 0)"})
    rows.append({"at": "2026-10-26 10:55:00", "title": "profile parameter sapgui/user_scripting = TRUE"})
    lines = summary_lines(rows)
    assert lines[0].startswith("26 Oct 16:58 -- 6 transports imported, all OK (DS4K910, DS4K911, DS4K912 and 3 more)")
    assert len(lines) == 2


def test_vpn_down_and_back_mails():
    from core import reachability as R
    h = R.plain_down_mail("PS4", R.VPN_DOWN, "[VPN NOT CONNECTED] ... reconnect stopped after 4 tries",
                          "2026-10-27 09:10:48", 1800, "Infrabeat VPN")
    assert "probably fine" in h and "press <b>Reset</b>" in h
    h = R.plain_recovered_mail("PS4", {"state": R.VPN_DOWN, "since": "2026-10-27 09:10:48", "message": "m"}, True)
    assert "answers again" in h and "because the VPN was down" in h and "still paused" in h


def test_systems_in_vpn_keeper_are_reported_as_one_vpn(monkeypatch):
    from core import reachability as R, vpn_keeper as vk
    monkeypatch.setattr(vk, "load_config", lambda: [{"name": "AG", "systems": ["CARFOUR QAS"]}])
    monkeypatch.setattr(vk, "find_exe", lambda: None)
    assert R._keeper_vpn("CARFOUR QAS") == "AG" and R._keeper_vpn("PRD") == ""
    note = R._keeper_note("AG")
    assert "Auto-reconnect is OFF" in note
    h = R.plain_down_mail("CARFOUR QAS", R.VPN_DOWN, "VPN not connected (AG): x." + note, "2026-10-27 12:58:22", 1800)
    assert "Automatic reconnect is off" in h


def test_automatic_rca_is_off_unless_switched_on(monkeypatch):
    from core.rca_trigger import TriggerConfig, sweep_decision
    monkeypatch.delenv("IBO_RCA_AUTO", raising=False)
    tc = TriggerConfig.from_yaml({"enabled": True, "trigger": {"mode": "instance"}})
    assert tc.automatic is False
    assert sweep_decision("PS4", 9000, "i", tc)[1].startswith("automatic RCA is off")
    monkeypatch.setenv("IBO_RCA_AUTO", "1")
    assert TriggerConfig.from_yaml({"trigger": {}}).automatic is True


def test_reminders_go_to_basis_with_the_manager_in_copy(monkeypatch):
    from core import alert_engine as E
    monkeypatch.setattr(E, "recipients", lambda c: {"email": {"to": ["b@infrabeat.com"], "cc": []},
                                                    "manager": ["m@infrabeat.com"]})
    r = E.mail_recipients({"name": "PS4"}, {"grade": "RED", "kind": "repeat"})
    assert r == {"to": ["b@infrabeat.com"], "cc": ["m@infrabeat.com"]}


def test_never_sent_from_a_personal_mailbox(monkeypatch):
    import pytest
    import smtplib
    from notifications import mailer, graph_mail
    monkeypatch.delenv("IBO_MAIL_TRANSPORT", raising=False)
    monkeypatch.delenv("IBO_ALLOW_PERSONAL_SENDER", raising=False)
    monkeypatch.setattr(graph_mail, "_env", lambda n: "")
    assert mailer.personal_sender({"from_email": "user.b.personal.example@gmail.com"}) == "user.b.personal.example@gmail.com"
    assert mailer.personal_sender({"from_email": "ibops@infrabeat.com", "username": "ibops@infrabeat.com"}) == ""
    with pytest.raises(smtplib.SMTPSenderRefused):
        mailer._smtp_once(None, {"host": "smtp.gmail.com", "from_email": "x@gmail.com"}, ["a@infrabeat.com"])
    assert mailer.classify(smtplib.SMTPSenderRefused(550, "refused by IBOPS", "x")).permanent


def test_graph_is_used_when_configured_even_without_the_transport_line(monkeypatch):
    from notifications import graph_mail
    vals = {"IBO_GRAPH_CLIENT_ID": "id", "IBO_GRAPH_SENDER": "ibops@infrabeat.com"}
    monkeypatch.setattr(graph_mail, "_env", lambda n: vals.get(n, ""))
    monkeypatch.delenv("IBO_MAIL_TRANSPORT", raising=False)
    assert graph_mail.enabled()
    monkeypatch.setenv("IBO_MAIL_TRANSPORT", "smtp")
    assert not graph_mail.enabled()
