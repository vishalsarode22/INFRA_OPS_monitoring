"""27 Oct 2026: projects (named system groups), grants by the team leader, saved passwords, audit."""
from test_accounts_roles import _login, mails, team  # noqa: F401
from test_fix_monitoring_log import env  # noqa: F401  (PS4=Metro, CEQ=Centor)


def _users(auth):
    auth.add_user("tl", "teamlead-pass-1", "team_lead", "Metro")
    auth.add_user("con", "consultant-pw-1", "consultant", "Metro")
    auth.add_user("sc", "senior-cons-pw1", "senior_consultant", "Metro")


def test_manager_creates_a_mixed_project_and_joins_it(team):  # noqa: F811
    app, auth, _a = team
    m = _login(app, "mia@infrabeat.com", "manager-pass-1")
    assert m.post("/api/projects", json={"name": "Mix", "systems": ["PS4", "CEQ"]}).status_code == 200
    rows = {p["name"]: p for p in m.get("/api/projects").json()["projects"]}
    assert rows["Mix"]["systems"] == ["CEQ", "PS4"] and rows["Metro"]["systems"] == ["PS4"]
    assert m.post("/api/projects", json={"name": "Mix", "systems": []}).status_code == 400   # exists
    _users(auth)
    t = _login(app, "tl", "teamlead-pass-1")
    assert t.post("/api/projects", json={"name": "X", "systems": []}).status_code == 400      # TL may not
    assert t.post("/api/run-now/CEQ").status_code == 404                                       # not his
    assert t.post("/api/run-now/PS4").status_code != 403


def test_team_leader_decides_what_a_consultant_may_do(team):  # noqa: F811
    app, auth, _a = team
    _users(auth)
    c = _login(app, "con", "consultant-pw-1")
    assert c.post("/api/run-now/PS4").status_code == 403                 # view only by default
    assert c.post("/api/ops/park", json={"system": "PS4"}).status_code == 403
    t = _login(app, "tl", "teamlead-pass-1")
    r = t.post("/api/projects/grants", json={"username": "con", "project": "Metro", "systems": ["PS4"],
                                             "run": True, "alerts": False, "settings": False})
    assert r.status_code == 200, r.text
    assert t.post("/api/projects/grants", json={"username": "tl", "project": "Metro", "systems": "*"}).status_code == 400
    c = _login(app, "con", "consultant-pw-1")
    assert c.post("/api/run-now/PS4").status_code != 403                 # allowed now
    assert c.post("/api/ops/park", json={"system": "PS4"}).status_code == 403   # alerts still not
    assert c.post("/api/ops/thresholds", json={"system": "PS4", "key": "x"}).status_code == 403
    # a consultant cannot grant himself anything
    assert c.post("/api/projects/grants", json={"username": "con", "project": "Metro", "systems": "*",
                                                "run": True, "alerts": True, "settings": True}).status_code == 400


def test_system_subset_hides_the_rest(team, monkeypatch):  # noqa: F811
    app, auth, _a = team
    from test_fix_monitoring_log import SYSTEMS
    from core import config_loader, projects
    more = SYSTEMS + [{"name": "PS5", "customer": "Metro"}]
    monkeypatch.setattr(config_loader, "get_all_systems", lambda *a, **k: [dict(s) for s in more])
    _users(auth)
    boss = {"username": "boss", "role": "admin", "customers": None}
    projects.set_grant(boss, "con", "Metro", ["PS4"], False, False, False)
    p = auth.principal_for_token(auth.login("con", "consultant-pw-1"))
    assert p["systems"] == {"PS4"}
    assert projects.may(p, "read", "PS4") and not projects.may(p, "read", "PS5")


def test_passwords_vault_and_audit(team, tmp_path, monkeypatch):  # noqa: F811
    app, auth, _a = team
    monkeypatch.setenv("IBO_VAULT_KEY_FILE", str(tmp_path / "vault.key"))
    from core import vault, secrets_store
    vault._fernet = None
    monkeypatch.setattr(secrets_store, "_keyring", lambda: None)
    _users(auth)
    c = _login(app, "con", "consultant-pw-1")
    r = c.post("/api/access/vault", json={"project": "Metro", "system": "PS4", "sap_user": "OM_D",
                                          "client": "100", "password": "S3cret!pw"})
    assert r.status_code == 200, r.text
    vid = r.json()["id"]
    assert c.post(f"/api/access/vault/{vid}/reveal").json()["value"] == "S3cret!pw"        # owner
    assert c.post("/api/access/vault", json={"project": "Centor", "system": "CEQ", "sap_user": "X",
                                             "password": "p"}).status_code == 400            # not his project
    s = _login(app, "sc", "senior-cons-pw1")
    rows = s.get("/api/access/passwords").json()["vault"]
    assert rows[0]["sap_user"] == "OM_D" and "S3cret" not in str(rows) and not rows[0]["can_reveal"]
    assert s.post(f"/api/access/vault/{vid}/reveal").status_code == 403
    t = _login(app, "tl", "teamlead-pass-1")
    assert t.post(f"/api/access/vault/{vid}/reveal").status_code == 403                    # TL: no reveal
    assert t.post(f"/api/access/vault/{vid}", json={"password": "N3w!"}).status_code == 200  # TL: change
    m = _login(app, "mia@infrabeat.com", "manager-pass-1")
    assert m.post(f"/api/access/vault/{vid}/reveal").json()["value"] == "N3w!"
    assert "N3w" not in open(tmp_path / "vault.key").read()
    from core import store
    raw = store.conn().execute("SELECT secret FROM vault WHERE id=?", (vid,)).fetchone()[0]
    assert "N3w" not in raw                                                                 # encrypted
    assert c.get("/api/access/audit").status_code == 403
    b = _login(app, "boss", "admin-password-1")
    acts = [r["action"] for r in b.get("/api/access/audit").json()["rows"]]
    assert "vault.reveal" in acts and "vault.change" in acts and "denied" in acts


def test_monitoring_password_change_and_reveal_rules(team, monkeypatch):  # noqa: F811
    app, auth, _a = team
    from core import vault, secrets_store
    monkeypatch.setattr(vault, "monitoring_vars", lambda: {"PS4_PASSWORD": {"PS4"}, "SHARED_PW": {"PS4", "CEQ"}})
    saved = {}
    monkeypatch.setattr(secrets_store, "save", lambda k, v, by: saved.update({k: v}) or {"key": k, "where": ".env"})
    monkeypatch.setattr(secrets_store, "status", lambda: [])
    monkeypatch.setenv("PS4_PASSWORD", "old-pw")
    _users(auth)
    t = _login(app, "tl", "teamlead-pass-1")
    rows = {r["key"]: r for r in t.get("/api/access/passwords").json()["monitoring"]}
    assert rows["PS4_PASSWORD"]["can_change"] and not rows["PS4_PASSWORD"]["can_reveal"]
    assert not rows["SHARED_PW"]["can_change"]                    # CEQ is not his
    assert t.post("/api/access/passwords/monitoring", json={"key": "PS4_PASSWORD", "value": "a", "again": "a"}).status_code == 200
    assert saved == {"PS4_PASSWORD": "a"}
    assert t.post("/api/access/passwords/monitoring/reveal", json={"key": "PS4_PASSWORD"}).status_code == 403
    c = _login(app, "con", "consultant-pw-1")
    assert c.post("/api/access/passwords/monitoring", json={"key": "PS4_PASSWORD", "value": "b", "again": "b"}).status_code == 403
    m = _login(app, "mia@infrabeat.com", "manager-pass-1")
    assert m.post("/api/access/passwords/monitoring/reveal", json={"key": "PS4_PASSWORD"}).json()["value"] == "old-pw"


# ---------------------------------------------------------------- review findings (27.10.2026)

def test_a_secret_cannot_be_injected_into_a_system(team, monkeypatch):  # noqa: F811
    app, auth, _a = team
    _users(auth)
    from core import config_editor
    monkeypatch.setattr(config_editor, "existing_entry", lambda n: {"name": "PS4", "password": "${PS4_PASSWORD}"})
    t = _login(app, "tl", "teamlead-pass-1")
    r = t.post("/api/ops/systems/save", json={"name": "PS4", "editing": True, "customer": "Metro",
                                              "alert_receivers": "${IBO_API_TOKEN}"})
    assert r.status_code == 403 and "IBO_API_TOKEN" in r.json()["detail"]


def test_naming_a_second_system_is_refused(team):  # noqa: F811
    app, auth, _a = team
    _users(auth)
    t = _login(app, "tl", "teamlead-pass-1")
    assert t.post("/api/ops/systems/save?system=PS4", json={"name": "CEQ", "editing": True}).status_code == 403
    assert t.post("/api/ops/systems/save", json={"system": "PS4", "name": "CEQ"}).status_code == 403
    # a ?system= the handler ignores does not unlock global writes
    for path, body in (("/api/ops/thresholds?system=PS4", {"key": "x"}),
                       ("/api/ops/alert-teams?system=PS4", {"teams": {"Basis": "evil@x.com"}}),
                       ("/api/ops/oncall/override?system=PS4", {"emails": "evil@x.com"}),
                       ("/api/profiles?system=PS4", {"profiles": []}),
                       ("/api/run-now?system=PS4", {})):
        assert t.post(path, json=body).status_code == 403, path


def test_consultant_grant_cannot_be_stretched_to_another_system(team, monkeypatch):  # noqa: F811
    app, auth, _a = team
    _users(auth)
    from core import projects
    boss = {"username": "boss", "role": "admin", "customers": None}
    projects.set_grant(boss, "con", "Metro", "*", False, True, False)
    from core import store
    store.conn().execute("UPDATE users SET customers='Metro,Centor' WHERE username='con'")
    c = _login(app, "con", "consultant-pw-1")
    assert c.post("/api/ops/park?system=PS4", json={"system": "CEQ"}).status_code == 403


def test_team_leader_cannot_approve_access_requests(team):  # noqa: F811
    app, auth, accounts = team
    _users(auth)
    auth.add_user("tl2", "teamlead-pass-2", "team_lead", "Centor")
    r = _login(app, "tl2", "teamlead-pass-2").post("/api/access/requests", json={"projects": ["Metro"]})
    assert r.status_code == 200, r.text
    rid = accounts.list_requests({"username": "boss", "role": "admin", "customers": None})["to_decide"][0]["id"]
    t = _login(app, "tl", "teamlead-pass-1")
    assert t.post(f"/api/access/requests/{rid}/decide", json={"approve": True}).status_code in (400, 403)


def test_platform_secrets_are_never_monitoring_passwords(monkeypatch, tmp_path):
    from core import vault, config_loader
    (tmp_path / "systems.yaml").write_text(
        "systems:\n- name: PS4\n  password: ${PS4_PASSWORD}\n  notes: ${GEMINI_API_KEY}\n  x: ${IBO_API_TOKEN}\n")
    monkeypatch.setattr(config_loader, "CONFIG_DIR", str(tmp_path))
    assert set(vault.monitoring_vars()) == {"PS4_PASSWORD"}


def test_viewer_cannot_look_up_accounts(team):  # noqa: F811
    app, auth, _a = team
    auth.add_user("vv", "viewer-pass-12", "viewer", "Metro")
    v = _login(app, "vv", "viewer-pass-12")
    assert v.get("/api/projects/grants?username=boss").status_code == 404


def test_an_existing_password_cannot_be_moved_into_a_shown_field(team, monkeypatch):  # noqa: F811
    app, auth, _a = team
    _users(auth)
    from core import config_editor
    monkeypatch.setattr(config_editor, "existing_entry", lambda n: {
        "name": "PS4", "rfc": {"password": "${PS4_RFC_PASSWORD}"}, "manager_receivers": "${PS4_MGR}"})
    monkeypatch.setattr(config_editor, "save_system", lambda p, e: {"ok": True})
    t = _login(app, "tl", "teamlead-pass-1")
    r = t.post("/api/ops/systems/save", json={"name": "PS4", "editing": True, "customer": "Metro",
                                              "notes": "${PS4_RFC_PASSWORD}"})
    assert r.status_code == 403
    ok = t.post("/api/ops/systems/save", json={"name": "PS4", "editing": True, "customer": "Metro",
                                               "rfc_password": "${PS4_RFC_PASSWORD}",
                                               "manager_receivers": "${PS4_MGR}"})
    assert ok.status_code == 200, ok.text


def test_json_variants_are_checked_too(team):  # noqa: F811
    app, auth, _a = team
    _users(auth)
    t = _login(app, "tl", "teamlead-pass-1")
    r = t.post("/api/ops/park", content=b'{"system": "CEQ"}', headers={"Content-Type": "application/vnd.api+json"})
    assert r.status_code in (403, 404)


def test_project_manager_cannot_rewrite_other_customers_profiles(team, monkeypatch):  # noqa: F811
    app, auth, _a = team
    from core import profiles
    saved = {}
    monkeypatch.setattr(profiles, "_load_raw", lambda: {"profiles": [{"id": "ceq-daily", "system": "CEQ"}],
                                                        "alerts": {"CRITICAL": {}}})
    monkeypatch.setattr(profiles, "save_profiles", lambda p, a=None: saved.update(p=p, a=a))
    m = _login(app, "mia@infrabeat.com", "manager-pass-1")
    assert m.post("/api/profiles", json={"profiles": [{"id": "x", "system": "CEQ"}]}).status_code == 403
    r = m.post("/api/profiles", json={"profiles": [{"id": "ps4-q", "system": "PS4"}], "alerts": {}})
    assert r.status_code == 200, r.text
    assert [p["id"] for p in saved["p"]] == ["ceq-daily", "ps4-q"] and saved["a"] is None
    assert m.post("/api/profiles/alerts", json={}).status_code == 403
    assert m.post("/api/scheduler/pause").status_code == 403
