"""Roles (admin > manager > team lead = senior consultant > consultant = viewer = client),
e-mail sign-up, confirmation, forgot/reset password, invitations and access requests."""
import re

import pytest
from fastapi.testclient import TestClient

from test_fix_monitoring_log import env  # noqa: F401  (PS4=Metro, CEQ=Centor)

BROWSER = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def mails(monkeypatch):
    from core import accounts
    sent = []

    def fake(to, subject, html):
        sent.append({"to": to, "subject": subject, "html": html})
        return True
    monkeypatch.setattr(accounts, "send_mail", fake)
    monkeypatch.setenv("IBO_PUBLIC_URL", "https://ibops.example")
    accounts._hits.clear()
    return sent


def _link(mail, path):
    m = re.search(r"https://ibops\.example(/" + path + r"\?token=[A-Za-z0-9_\-]+)", mail["html"])
    assert m, mail["html"]
    return m.group(1)


def _client(app_module):
    return TestClient(app_module.app)


def _login(app_module, user, pw):
    c = _client(app_module)
    r = c.post("/login", data={"username": user, "password": pw, "next": "/"}, headers=BROWSER,
               follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    c.headers["X-CSRF-Token"] = c.get("/api/me").json()["csrf_token"]
    return c


@pytest.fixture
def team(env, monkeypatch, mails):  # noqa: F811
    app_module, auth, _ops = env
    monkeypatch.setenv("IBO_AUTH", "1")
    monkeypatch.setattr(app_module, "_run_one_system_background", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_begin_manual_hold", lambda: None)
    app_module._release_manual_claim()
    from core import accounts, store
    accounts.ensure_schema()
    auth.add_user("boss", "admin-password-1", "admin", "*")
    auth.add_user("mia@infrabeat.com", "manager-pass-1", "manager", "Metro")
    store.conn().execute("UPDATE users SET email=username WHERE username LIKE '%@%'")
    return app_module, auth, accounts


# ------------------------------------------------------------------ roles

@pytest.mark.parametrize("role,level", [("admin", 3), ("manager", 2), ("team_lead", 2),
                                        ("senior_consultant", 2), ("operator", 2), ("consultant", 1),
                                        ("viewer", 1), ("client", 1)])
def test_role_levels(env, role, level):  # noqa: F811
    _app, auth, _ops = env
    assert auth.role_level(role) == level
    p = {"role": role}
    assert auth.has_role(p, "viewer")
    assert auth.has_role(p, "operator") == (level >= 2)
    assert auth.has_role(p, "admin") == (level == 3)


def test_what_each_role_may_grant(env):  # noqa: F811
    _app, auth, _ops = env
    assert "manager" in auth.grantable_roles({"role": "admin"})
    assert set(auth.grantable_roles({"role": "manager"})) == {"team_lead", "senior_consultant", "consultant",
                                                              "viewer", "client"}
    for r in ("team_lead", "senior_consultant", "consultant", "viewer", "client"):
        assert auth.grantable_roles({"role": r}) == ()


def test_consultant_reads_but_cannot_act_team_lead_can(team):
    app_module, auth, _acc = team
    auth.add_user("con", "consultant-pw-1", "consultant", "Metro")
    auth.add_user("tl", "teamlead-pass-1", "team_lead", "Metro")
    c = _login(app_module, "con", "consultant-pw-1")
    assert c.get("/api/me").json()["role_label"] == "Consultant"
    assert c.post("/api/run-now/PS4").status_code == 403          # read-only
    t = _login(app_module, "tl", "teamlead-pass-1")
    assert t.post("/api/run-now/PS4").status_code != 403          # acts (may be 409/200)
    assert t.post("/api/run-now/CEQ").status_code == 404          # other project: invisible


def test_fresh_signup_sees_no_project(team, mails):
    app_module, auth, accounts = team
    accounts.signup("newbie@infrabeat.com", "New Bie", "newbie-pass-1")
    accounts.verify(_link(mails[-1], "verify").split("token=")[1], "newbie-pass-1")
    c = _login(app_module, "newbie@infrabeat.com", "newbie-pass-1")
    me = c.get("/api/me").json()
    assert me["customers"] == [] and me["role"] == "consultant"
    assert "no project" in me["banner"] and me["banner_href"] == "/team"
    assert c.get("/api/overview").json().get("systems", []) == []


# ---------------------------------------------------------------- sign-up

def test_signup_only_for_company_domain(team):
    app_module, _auth, _acc = team
    c = _client(app_module)
    r = c.post("/signup", data={"email": "eve@gmail.com", "full_name": "Eve", "password": "long-enough-1",
                                "password2": "long-enough-1"}, headers=BROWSER)
    assert r.status_code == 400 and "infrabeat.com" in r.text


def test_signup_confirm_then_login(team, mails):
    app_module, _auth, _acc = team
    c = _client(app_module)
    r = c.post("/signup", data={"email": "Ravi@Infrabeat.com", "full_name": "Ravi K", "password": "ravi-pass-123",
                                "password2": "ravi-pass-123"}, headers=BROWSER)
    assert r.status_code == 200 and "Check your inbox" in r.text
    assert mails[-1]["to"] == "ravi@infrabeat.com"
    r = c.post("/login", data={"username": "ravi@infrabeat.com", "password": "ravi-pass-123"}, headers=BROWSER,
               follow_redirects=False)
    assert r.status_code == 403 and "Confirm your e-mail" in r.text
    link = _link(mails[-1], "verify")
    assert "Confirm your e-mail" in c.get(link).text                  # GET only shows the form
    token = link.split("token=")[1]
    assert c.post("/verify", data={"token": token, "password": "wrong-password-1"}, headers=BROWSER).status_code == 400
    assert "E-mail confirmed" in c.post("/verify", data={"token": token, "password": "ravi-pass-123"},
                                        headers=BROWSER).text
    assert "expired" in c.get(link).text                              # one use
    _login(app_module, "ravi@infrabeat.com", "ravi-pass-123")


def test_signup_existing_address_does_not_reveal_or_duplicate(team, mails):
    _app, _auth, accounts = team
    res = accounts.signup("mia@infrabeat.com", "x", "whatever-pass-1")
    assert res == {"created": False, "mailed": True}
    assert "already has" in mails[-1]["html"]


# -------------------------------------------------------- forgot / reset

def test_forgot_and_reset_password(team, mails):
    app_module, _auth, _acc = team
    c = _client(app_module)
    r = c.post("/forgot", data={"email": "mia@infrabeat.com"}, headers=BROWSER)
    assert "Check your inbox" in r.text
    link = _link(mails[-1], "reset")
    assert "Choose a password" in c.get(link).text
    token = link.split("token=")[1]
    r = c.post("/reset", data={"token": token, "password": "brand-new-pass-1", "password2": "brand-new-pass-1"},
               headers=BROWSER)
    assert "Password saved" in r.text
    _login(app_module, "mia@infrabeat.com", "brand-new-pass-1")
    r = c.post("/reset", data={"token": token, "password": "another-pass-12", "password2": "another-pass-12"},
               headers=BROWSER)
    assert r.status_code == 400                                       # link used up


def test_forgot_unknown_address_same_answer_no_mail(team, mails):
    app_module, _auth, _acc = team
    n = len(mails)
    r = _client(app_module).post("/forgot", data={"email": "nobody@infrabeat.com"}, headers=BROWSER)
    assert "Check your inbox" in r.text and len(mails) == n


def test_change_own_password(team):
    app_module, _auth, _acc = team
    c = _login(app_module, "mia@infrabeat.com", "manager-pass-1")
    assert c.post("/api/me/password", json={"current": "wrong-one-xx", "new": "next-pass-123"}).status_code == 400
    assert c.post("/api/me/password", json={"current": "manager-pass-1", "new": "next-pass-123"}).status_code == 200
    _login(app_module, "mia@infrabeat.com", "next-pass-123")


# ------------------------------------------------------- access requests

def test_request_approved_by_that_projects_manager(team, mails):
    app_module, auth, accounts = team
    accounts.signup("ravi@infrabeat.com", "Ravi", "ravi-pass-123")
    accounts.verify(_link(mails[-1], "verify").split("token=")[1], "ravi-pass-123")
    r = _login(app_module, "ravi@infrabeat.com", "ravi-pass-123")
    out = r.post("/api/access/requests", json={"projects": ["Metro", "Centor"], "role_wanted": "team_lead"}).json()
    assert {m["project"] for m in out["created"]} == {"Metro", "Centor"}
    assert any(m["to"] == "mia@infrabeat.com" and "Metro" in m["subject"] for m in mails)   # Metro's manager
    assert any(m["to"] == "boss" or "Centor" in m["subject"] for m in mails)                 # no manager: admins

    m = _login(app_module, "mia@infrabeat.com", "manager-pass-1")
    todo = m.get("/api/team").json()["requests"]["to_decide"]
    assert [t["project"] for t in todo] == ["Metro"]                  # Centor is not hers
    centor = [x for x in accounts.list_requests({"role": "admin", "customers": None})["to_decide"]
              if x["project"] == "Centor"][0]
    assert m.post(f"/api/access/requests/{centor['id']}/decide", json={"approve": True}).status_code == 400
    res = m.post(f"/api/access/requests/{todo[0]['id']}/decide", json={"approve": True, "role": "team_lead"}).json()
    assert res["status"] == "approved" and res["role"] == "team_lead"
    assert "approved" in mails[-1]["subject"] and mails[-1]["to"] == "ravi@infrabeat.com"

    r = _login(app_module, "ravi@infrabeat.com", "ravi-pass-123")      # rights changed: sign in again
    me = r.get("/api/me").json()
    assert me["customers"] == ["Metro"] and me["role"] == "team_lead"


def test_manager_cannot_make_managers_or_touch_admins(team):
    app_module, _auth, _acc = team
    m = _login(app_module, "mia@infrabeat.com", "manager-pass-1")
    r = m.post("/api/team/users", json={"email": "x@infrabeat.com", "role": "manager", "projects": ["Metro"]})
    assert r.status_code == 400
    assert m.post("/api/team/users/boss", json={"disabled": True}).status_code == 400
    r = m.post("/api/team/users", json={"email": "c@client.com", "role": "client", "projects": ["Centor"]})
    assert r.status_code == 400 and "do not manage" in r.json()["detail"]


def test_manager_invites_a_client_to_her_project(team, mails):
    app_module, _auth, _acc = team
    m = _login(app_module, "mia@infrabeat.com", "manager-pass-1")
    r = m.post("/api/team/users", json={"email": "ops@metro-client.com", "full_name": "Metro Ops",
                                        "role": "client", "projects": ["Metro"]}).json()
    assert r["mailed"] and r["user"]["role"] == "client" and r["user"]["projects"] == ["Metro"]
    link = _link(mails[-1], "reset")
    c = _client(app_module)
    c.post("/reset", data={"token": link.split("token=")[1], "password": "client-pass-12",
                           "password2": "client-pass-12"}, headers=BROWSER)
    cl = _login(app_module, "ops@metro-client.com", "client-pass-12")
    assert cl.post("/api/run-now/PS4").status_code == 403              # client is read-only
    assert cl.get("/api/team").json()["users"] == []                   # and sees nobody


def test_manager_sees_only_people_on_her_projects(team):
    app_module, auth, _acc = team
    auth.add_user("a1", "password-aaa1", "viewer", "Metro")
    auth.add_user("c1", "password-ccc1", "viewer", "Centor")
    m = _login(app_module, "mia@infrabeat.com", "manager-pass-1")
    names = {u["username"] for u in m.get("/api/team").json()["users"]}
    assert "a1" in names and "c1" not in names and "boss" not in names


def test_role_change_refused_when_person_also_on_other_projects(team):
    app_module, auth, _acc = team
    auth.add_user("both", "password-both1", "viewer", "Metro,Centor")
    m = _login(app_module, "mia@infrabeat.com", "manager-pass-1")
    r = m.post("/api/team/users/both", json={"role": "team_lead"})
    assert r.status_code == 400 and "ask an admin" in r.json()["detail"]
    assert m.post("/api/team/users/both", json={"remove_projects": ["Metro"]}).json()["projects"] == ["Centor"]
    a = _login(app_module, "boss", "admin-password-1")
    assert a.post("/api/team/users/both", json={"role": "team_lead"}).json()["role"] == "team_lead"


def test_legacy_operator_and_star_users_keep_working(team):
    app_module, auth, _acc = team
    auth.add_user("old", "old-operator-1", "operator", "*")
    c = _login(app_module, "old", "old-operator-1")
    assert c.get("/api/me").json()["customers"] == "*"
    assert c.post("/api/run-now/PS4").status_code != 403


def test_no_project_user_is_held_on_team_page(team, mails):
    app_module, _auth, accounts = team
    accounts.signup("new@infrabeat.com", "New", "newbie-pass-12")
    accounts.verify(_link(mails[-1], "verify").split("token=")[1], "newbie-pass-12")
    c = _login(app_module, "new@infrabeat.com", "newbie-pass-12")
    assert c.get("/api/errors").status_code == 403
    assert c.get("/errors", follow_redirects=False).headers["location"] == "/team"
    assert c.get("/api/team").status_code == 200
    assert c.post("/api/access/requests", json={"projects": ["Metro"]}).status_code == 200


def test_manager_cannot_disable_or_take_over_someone_on_other_projects(team, monkeypatch):
    app_module, auth, accounts = team
    from core import store
    auth.add_user("both@infrabeat.com", "password-both1", "team_lead", "Metro,Centor")
    store.conn().execute("UPDATE users SET email=username WHERE username='both@infrabeat.com'")
    m = _login(app_module, "mia@infrabeat.com", "manager-pass-1")
    assert m.post("/api/team/users/both@infrabeat.com", json={"disabled": True}).status_code == 400
    monkeypatch.setattr(accounts, "send_mail", lambda *a: False)       # mail down
    r = m.post("/api/team/users/both@infrabeat.com/reset")
    assert r.status_code == 400 and "link" not in r.json()


# ---- found in the security review

def test_squatted_signup_cannot_be_confirmed_by_the_squatter(team, mails):
    app_module, _auth, accounts = team
    accounts.signup("victim@infrabeat.com", "Attacker", "attacker-pass-1")      # squat
    accounts.signup("victim@infrabeat.com", "Victim", "victim-pass-123")        # the owner signs up
    token = _link(mails[-1], "verify").split("token=")[1]
    with pytest.raises(accounts.AccountError):
        accounts.verify(token, "attacker-pass-1")
    assert accounts.verify(token, "victim-pass-123") == "victim@infrabeat.com"
    _login(app_module, "victim@infrabeat.com", "victim-pass-123")
    r = _client(app_module).post("/login", data={"username": "victim@infrabeat.com", "password": "attacker-pass-1"},
                                 headers=BROWSER, follow_redirects=False)
    assert r.status_code == 401


def test_manager_cannot_approve_another_manager(team, mails):
    app_module, auth, accounts = team
    from core import store
    auth.add_user("nick@infrabeat.com", "manager-pass-2", "manager", "Centor")
    store.conn().execute("UPDATE users SET email=username WHERE username='nick@infrabeat.com'")
    made = accounts.request_access({"username": "nick@infrabeat.com"}, ["Metro"], "consultant")
    m = _login(app_module, "mia@infrabeat.com", "manager-pass-1")
    r = m.post(f"/api/access/requests/{made[0]['id']}/decide", json={"approve": True, "role": "consultant"})
    assert r.status_code == 400 and "admin" in r.json()["detail"]


def test_manager_cannot_touch_people_outside_her_projects(team, mails):
    app_module, auth, _acc = team
    auth.add_user("carl", "carl-pass-1234", "consultant", "Centor")
    m = _login(app_module, "mia@infrabeat.com", "manager-pass-1")
    assert m.post("/api/team/users/carl", json={"full_name": "Hacked"}).status_code == 400
    assert m.post("/api/team/users/carl/reset").status_code == 400
    auth.add_user("legacyop", "legacy-pass-12", "operator", "*")
    assert m.post("/api/team/users/legacyop", json={"remove_projects": ["Metro"]}).status_code == 400


def test_lockout_counts_per_account_not_per_typed_name(team):
    _app, auth, _acc = team
    auth._failures.clear()
    for i in range(5):
        auth.login("mia@infrabeat.com" if i % 2 else "MIA@infrabeat.com", "wrong-password", "10.1.1.1")
    with pytest.raises(auth.LoginLocked):
        auth.login("mia@infrabeat.com", "manager-pass-1", "10.1.1.1")


def test_no_project_user_cannot_see_run_status(team, mails):
    app_module, _auth, accounts = team
    accounts.signup("new2@infrabeat.com", "New", "newbie-pass-12")
    accounts.verify(_link(mails[-1], "verify").split("token=")[1], "newbie-pass-12")
    c = _login(app_module, "new2@infrabeat.com", "newbie-pass-12")
    assert c.get("/api/run-status").status_code == 403


def test_cli_sets_email_for_a_legacy_login(team, mails):
    app_module, auth, _acc = team
    from dashboard import manage_users
    assert manage_users.main(["email", "boss", "staff.a@infrabeat.com"]) == 0
    _login(app_module, "staff.a@infrabeat.com", "admin-password-1")
    _client(app_module).post("/forgot", data={"email": "staff.a@infrabeat.com"}, headers=BROWSER)
    assert mails[-1]["to"] == "staff.a@infrabeat.com"
