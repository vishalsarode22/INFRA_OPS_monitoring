"""
Covers the process split / state / auth round:
store (documents, lines, jobs, heartbeats), utils.atomic routed to the
store, alert ledger repeat suppression, auth (passwords, sessions, roles,
customer scope), remote-card scope, sweep run lock, parser health,
retention planning, host/IP masking, recipients.

Each test points the store at its own database file.
"""
import os
import importlib


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("IBO_STATE_BACKEND", "sqlite")
    from core import store
    importlib.reload(store)
    return store


def test_documents_round_trip_and_lazy_import(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path, monkeypatch)
    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"a": 1}', encoding="utf-8")
    assert store.get_document(str(legacy)) == {"a": 1}          # imported from disk
    legacy.unlink()
    assert store.get_document(str(legacy)) == {"a": 1}          # now from the DB
    store.put_document(str(legacy), {"a": 2})
    assert store.get_document(str(legacy)) == {"a": 2}


def test_utils_atomic_goes_to_the_store(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    from utils.atomic import atomic_write_json, read_json
    p = str(tmp_path / "esc.json")
    atomic_write_json(p, {"x": 1})
    assert not os.path.exists(p)                 # no file written
    assert read_json(p) == {"x": 1}
    monkeypatch.setenv("IBO_STATE_BACKEND", "files")
    atomic_write_json(p, {"x": 2})
    assert os.path.exists(p) and read_json(p) == {"x": 2}


def test_lines_and_jobs_and_heartbeats(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path, monkeypatch)
    p = str(tmp_path / "h.jsonl")
    store.append_lines(p, ['{"m":"cpu"}', '{"m":"mem"}'])
    assert list(store.read_lines(p)) == ['{"m":"cpu"}', '{"m":"mem"}']

    jid = store.enqueue("mail", {"to": ["a@b.c"]}, max_attempts=2)
    job = store.claim_job("w1")
    assert job["id"] == jid and job["attempts"] == 1
    assert store.claim_job("w2") is None                       # taken
    store.finish_job(jid, False, "smtp down", retry_in=0)
    assert store.claim_job("w1")["attempts"] == 2              # retried
    store.finish_job(jid, False, "smtp down", retry_in=0)
    assert store.jobs_summary() == {"failed": 1}

    store.beat("jobs", "ok")
    hb = store.heartbeats()
    assert hb[0]["service"] == "jobs" and hb[0]["age_seconds"] <= 1


def test_ledger_holds_repeats_but_not_changes(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    from core import alert_ledger
    importlib.reload(alert_ledger)
    ok, _ = alert_ledger.should_send("PS4", "critical", ["YELLOW", "cpu"], 3600)
    assert ok
    alert_ledger.record("PS4", "critical", ["YELLOW", "cpu"], "CRITICAL", "s", ["b@x"])
    ok, why = alert_ledger.should_send("PS4", "critical", ["cpu", "YELLOW"], 3600)
    assert not ok and "held" in why
    ok, _ = alert_ledger.should_send("PS4", "critical", ["RED", "cpu"], 3600)   # escalation
    assert ok
    alert_ledger.record("PS4", "critical", ["RED", "cpu"], "CRITICAL", "s2", ["b@x"])
    assert alert_ledger.today_summary()["PS4"]["critical"] == {"sent": 2, "suppressed": 1}
    # A check that repeats every few seconds records ONE hold per 10 min, not one per check.
    for _ in range(5):
        alert_ledger.should_send("PS4", "critical", ["YELLOW", "cpu"], 3600)
    assert alert_ledger.today_summary()["PS4"]["critical"] == {"sent": 2, "suppressed": 1}


def test_auth_roles_sessions_and_scope(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    from core import auth
    importlib.reload(auth)
    auth.add_user("admin", "pw-admin", "admin", "*")
    auth.add_user("metro", "pw-metro", "viewer", "Metro")
    assert auth.login("metro", "wrong") is None
    tok = auth.login("metro", "pw-metro")
    p = auth.principal_for_token(tok)
    assert p["role"] == "viewer" and p["customers"] == {"Metro"}

    systems = [{"name": "PS4", "customer": "Metro"}, {"name": "CEQ", "customer": "Centor"},
               {"name": "PRD"}]
    t = auth.current_user.set(p)
    try:
        assert [s["name"] for s in auth.filter_systems(systems)] == ["PS4"]
        assert not auth.may_write()
    finally:
        auth.current_user.reset(t)
    t = auth.current_user.set(auth.principal_for_token(auth.login("admin", "pw-admin")))
    try:
        assert len(auth.filter_systems(systems)) == 3 and auth.is_admin()
    finally:
        auth.current_user.reset(t)

    auth.disable_user("metro")
    assert auth.principal_for_token(tok) is None


def test_remote_cards_follow_customer_scope(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    from core import auth
    importlib.reload(auth)
    from dashboard import remote_sources
    items = [{"system": "PS4"},                                   # local
             {"system": "SARLOHA", "remote": True, "source": "ubuntu", "customer": "Sarloha"},
             {"system": "X", "remote": True, "source": "ubuntu", "customer": ""}]
    t = auth.current_user.set({"username": "s", "role": "viewer", "customers": {"Sarloha"}})
    try:
        assert [i["system"] for i in remote_sources._scope_filter(items)] == ["PS4", "SARLOHA"]
    finally:
        auth.current_user.reset(t)
    assert len(remote_sources._scope_filter(items)) == 3        # no user: no filter


def test_run_lock_excludes_a_second_holder(tmp_path, monkeypatch):
    from core import run_lock
    monkeypatch.setattr(run_lock, "LOCK_PATH", str(tmp_path / "sweep.lock"))
    with run_lock.sweep_lock("PS4") as got:
        assert got
        who = run_lock.owner()
        # Same process holds it; a probe from this process sees the fcntl/msvcrt
        # lock as held only on platforms where locks are per-descriptor.
        assert who is None or who["system"] == "PS4"
    assert run_lock.owner() is None


def test_parser_health_flags_a_capture_that_extracts_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_STATE_BACKEND", "files")
    from core import parser_health
    monkeypatch.setattr(parser_health, "STATE_PATH", str(tmp_path / "ph.json"))

    class R:
        def __init__(self, extra):
            self.tcode, self.display_value, self.extra_data, self.detail = "SM50", "captured (1)", extra, ""

    assert parser_health.assess("PS4", [R({"a": 1, "b": 2})])["broken"] == []
    r = R({"ocr_lines": 12})
    v = parser_health.assess("PS4", [r])
    assert v["broken"][0]["tcode"] == "SM50" and r.extra_data["parser_health"] == "broken"
    assert "PARSER CHECK" in r.detail


def test_retention_plan_keeps_recent_and_drops_old(tmp_path, monkeypatch):
    from core import retention
    root = tmp_path / "reports"
    (root / "2020-01-01" / "PS4").mkdir(parents=True)
    (root / "2020-01-01" / "PS4" / "r.pdf").write_text("x")
    import datetime as dt
    today = dt.datetime.now().strftime("%Y-%m-%d")
    (root / today / "PS4" / "evidence").mkdir(parents=True)
    monkeypatch.setattr(retention, "REPORTS_DIR", str(root))
    plan = retention.plan()
    assert any(p.endswith("2020-01-01") for p in plan["report_days"])
    assert not plan["evidence_dirs"]


def test_host_masking_round_trip_and_leaves_metrics_alone(monkeypatch):
    from evaluation import ai_pseudonymizer as ap
    monkeypatch.setattr(ap, "_configured_hosts", lambda: ["sapqas01"])
    m = ap.HostMasker()
    text = ("sap.icm.state on sapqas01 (10.2.2.158), app sapxps4ci.sap.example.in, "
            "mail a.b@corp.example.in, release 7.50.2")
    masked = m.mask(text)
    assert "sapqas01" not in masked and "10.2.2.158" not in masked
    assert "sapxps4ci" not in masked
    assert "sap.icm.state" in masked and "7.50.2" in masked
    assert "a.b@corp.example.in" in masked            # e-mail left to ai_privacy
    assert m.unmask(masked) == text


def test_manager_list_shares_the_escalation_keys(monkeypatch):
    from core import recipients
    monkeypatch.setenv("ALERT_MANAGER_EMAILS", "mgr@x.com")
    monkeypatch.setenv("IBO_BASIS_EMAILS", "basis@x.com")
    assert recipients.manager(None) == ["mgr@x.com"]
    assert recipients.basis_and_manager({"manager_receivers": "m2@x.com"}) == (["m2@x.com"], ["basis@x.com"])


def test_two_simultaneous_alerts_for_the_same_thing_send_once(tmp_path, monkeypatch):
    """Two systems behind one VPN report at the same moment: one mail, not two."""
    import threading
    _fresh_store(tmp_path, monkeypatch)
    from core import alert_ledger
    importlib.reload(alert_ledger)
    results = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(alert_ledger.should_send("network:Pune_TTML", "vpn_down", "Pune_TTML", 1800)[0])

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert results.count(True) == 1
    alert_ledger.record("network:Pune_TTML", "vpn_down", "Pune_TTML", "WARNING", "subj", ["b@x"])
    sent = [a for a in alert_ledger.recent(20) if not a["suppressed"]]
    assert len(sent) == 1 and sent[0]["subject"] == "subj"     # reservation filled in, not duplicated
