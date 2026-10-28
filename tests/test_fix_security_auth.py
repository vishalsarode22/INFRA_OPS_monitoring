"""
Security fixes: authentication, roles, CSRF, sessions, the removed token
endpoint, loopback-only mode with no users, report access.

Each test runs against its own state database (users + sessions).
"""
import importlib
import re

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_DB_PATH", str(tmp_path / "auth.sqlite"))
    monkeypatch.setenv("IBO_AUTH", "1")
    monkeypatch.setenv("IBO_API_TOKEN", "automation-token-123")
    from core import store, auth
    importlib.reload(store)
    auth._failures.clear()
    auth._secret_cache.clear()
    import dashboard.app as app_module
    monkeypatch.setattr(app_module, "get_systems",
                        lambda: [{"name": "PS4", "client": "100"}])
    yield app_module, auth
    monkeypatch.undo()
    importlib.reload(store)


def _client(app_module, **kw):
    return TestClient(app_module.app, **kw)


def _login(client, user, pw):
    return client.post("/login", data={"username": user, "password": pw, "next": "/"},
                       headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)


def _csrf(client):
    return client.get("/api/me").json()["csrf_token"]


# ------------------------------------------------------------ token endpoint

def test_auth_token_endpoint_is_gone_and_never_returns_the_token(env):
    app_module, auth = env
    c = _client(app_module)
    r = c.get("/api/auth-token")
    assert r.status_code == 410
    assert "automation-token-123" not in r.text


def test_bearer_token_works_for_automation_and_bad_token_is_401(env):
    app_module, auth = env
    auth.add_user("alice", "alice-password-1", "viewer")
    c = _client(app_module)
    ok = c.get("/api/me", headers={"Authorization": "Bearer automation-token-123"})
    assert ok.status_code == 200 and ok.json()["username"] == "api-token"
    assert c.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    # Bearer callers are not subject to CSRF (no cookie involved).
    r = c.post("/api/scheduler/snooze?minutes=5",
               headers={"Authorization": "Bearer automation-token-123"})
    assert r.status_code == 200


# --------------------------------------------------------------- no users

def test_no_users_loopback_is_local_admin_with_banner(env):
    app_module, auth = env
    c = _client(app_module)                       # TestClient = loopback peer
    me = c.get("/api/me").json()
    assert me["role"] == "admin" and me["local_mode"] and "No users configured" in me["banner"]
    # Writes still need the page's CSRF token...
    assert c.post("/api/scheduler/snooze?minutes=5").status_code == 401
    # ...and work with it.
    r = c.post("/api/scheduler/snooze?minutes=5", headers={"X-CSRF-Token": me["csrf_token"]})
    assert r.status_code == 200


def test_no_users_non_loopback_gets_403_with_instructions(env):
    app_module, auth = env
    c = _client(app_module, client=("10.20.30.40", 5000))
    r = c.get("/api/systems")
    assert r.status_code == 403 and "manage_users" in r.json()["detail"]
    page = c.get("/overview")
    assert page.status_code == 403 and "manage_users" in page.text
    assert c.get("/healthz").status_code == 200          # public allow-list


def test_reverse_proxied_request_is_not_treated_as_loopback(env):
    app_module, auth = env
    c = _client(app_module)
    r = c.get("/api/systems", headers={"X-Forwarded-For": "10.1.1.1"})
    assert r.status_code == 403


def test_ibo_auth_0_is_loopback_only(env, monkeypatch):
    app_module, auth = env
    monkeypatch.setenv("IBO_AUTH", "0")
    auth.add_user("alice", "alice-password-1", "admin")
    assert _client(app_module).get("/api/me").json()["local_mode"] is True
    r = _client(app_module, client=("192.168.1.9", 1)).get("/api/me")
    assert r.status_code == 403


# ------------------------------------------------------- sessions & roles

def test_login_sets_strict_httponly_cookie_and_requires_login(env):
    app_module, auth = env
    auth.add_user("alice", "alice-password-1", "viewer")
    c = _client(app_module)
    assert c.get("/api/scheduler").status_code == 401         # loopback no longer enough
    assert c.get("/overview", follow_redirects=False).status_code == 303
    r = _login(c, "alice", "alice-password-1")
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "secure" not in cookie                              # plain http test client
    assert c.get("/api/scheduler").status_code == 200


def test_secure_cookie_over_https_and_forwarded_proto_only_when_trusted(env, monkeypatch):
    app_module, auth = env
    auth.add_user("alice", "alice-password-1", "viewer")
    c = _client(app_module)
    r = c.post("/login", data={"username": "alice", "password": "alice-password-1"},
               headers={"X-Forwarded-Proto": "https", "Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert "secure" not in r.headers["set-cookie"].lower()     # proxy not trusted
    monkeypatch.setenv("IBO_TRUST_PROXY", "1")
    r = c.post("/login", data={"username": "alice", "password": "alice-password-1"},
               headers={"X-Forwarded-Proto": "https", "Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert "secure" in r.headers["set-cookie"].lower()


def test_viewer_operator_admin_permissions(env):
    app_module, auth = env
    auth.add_user("vic", "viewer-password", "viewer")
    auth.add_user("otto", "operator-password", "operator")
    auth.add_user("ada", "admin-password!", "admin")

    viewer = _client(app_module)
    _login(viewer, "vic", "viewer-password")
    h = {"X-CSRF-Token": _csrf(viewer)}
    assert viewer.get("/api/scheduler").status_code == 200
    assert viewer.post("/api/scheduler/snooze?minutes=5", headers=h).status_code == 403
    assert viewer.delete("/api/systems/PS4", headers=h).status_code == 403

    op = _client(app_module)
    _login(op, "otto", "operator-password")
    h = {"X-CSRF-Token": _csrf(op)}
    assert op.post("/api/scheduler/snooze?minutes=5", headers=h).status_code == 200
    assert op.delete("/api/systems/PS4", headers=h).status_code == 403
    assert op.post("/api/delivery-settings", json={}, headers=h).status_code == 403
    assert op.get("/api/users").status_code == 403

    admin = _client(app_module)
    _login(admin, "ada", "admin-password!")
    assert admin.get("/api/users").status_code == 200


def test_csrf_required_and_cross_origin_rejected(env):
    app_module, auth = env
    auth.add_user("otto", "operator-password", "operator")
    c = _client(app_module)
    _login(c, "otto", "operator-password")
    token = _csrf(c)
    assert c.post("/api/scheduler/snooze?minutes=5").status_code == 403
    assert c.post("/api/scheduler/snooze?minutes=5", headers={"X-CSRF-Token": "x" * 64}).status_code == 403
    evil = {"X-CSRF-Token": token, "Origin": "https://evil.example"}
    assert c.post("/api/scheduler/snooze?minutes=5", headers=evil).status_code == 403
    good = {"X-CSRF-Token": token, "Origin": "http://testserver"}
    assert c.post("/api/scheduler/snooze?minutes=5", headers=good).status_code == 200


def test_login_lockout_per_username(env):
    app_module, auth = env
    auth.add_user("alice", "alice-password-1", "viewer")
    c = _client(app_module)
    for _ in range(auth.LOCK_AFTER_USER):
        assert _login(c, "alice", "wrong-password").status_code == 401
    # Locked: even the right password is refused for the window.
    assert _login(c, "alice", "alice-password-1").status_code == 429


def test_session_idle_and_absolute_expiry(env, monkeypatch):
    app_module, auth = env
    from core import store
    auth.add_user("alice", "alice-password-1", "viewer")
    tok = auth.login("alice", "alice-password-1")
    assert auth.principal_for_token(tok) is not None
    store.conn().execute("UPDATE sessions SET created_at='2000-01-01 00:00:00' WHERE token=?", (tok,))
    assert auth.principal_for_token(tok) is None              # absolute cap
    tok = auth.login("alice", "alice-password-1")
    store.conn().execute("UPDATE sessions SET expires_at='2000-01-01 00:00:00' WHERE token=?", (tok,))
    assert auth.principal_for_token(tok) is None              # idle


def test_logout_ends_session(env):
    app_module, auth = env
    auth.add_user("alice", "alice-password-1", "viewer")
    c = _client(app_module)
    _login(c, "alice", "alice-password-1")
    assert c.get("/api/scheduler").status_code == 200
    c.post("/logout", follow_redirects=False)
    assert c.get("/api/scheduler").status_code == 401


def test_password_hash_is_salted_pbkdf2_200k(env):
    app_module, auth = env
    h1, h2 = auth.hash_password("same-password"), auth.hash_password("same-password")
    assert h1 != h2 and h1.startswith("pbkdf2$") and int(h1.split("$")[1]) >= 200_000


# ------------------------------------------------------------ reports (8)

def test_reports_need_a_login(env, tmp_path, monkeypatch):
    app_module, auth = env
    rep = tmp_path / "reports" / "2026-10-23"
    rep.mkdir(parents=True)
    (rep / "PS4_report.pdf").write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(app_module, "REPORTS_DIR", str(tmp_path / "reports"))
    auth.add_user("vic", "viewer-password", "viewer")
    c = _client(app_module)
    assert c.get("/reports/2026-10-23/PS4_report.pdf").status_code == 401
    assert c.get("/api/reports/PS4").status_code == 401
    _login(c, "vic", "viewer-password")
    assert c.get("/reports/2026-10-23/PS4_report.pdf").status_code == 200
    assert c.get("/reports/..%2F..%2F.env").status_code in (403, 404)


# ---------------------------------------------------------- audit actor

def test_incident_action_records_logged_in_user_not_body_actor(env, monkeypatch):
    app_module, auth = env
    from dashboard import intelligence_api as ia
    seen = {}

    class _Inc:
        incident_id, title = "INC1", "t"

        def to_dict(self):
            return {"incident_id": "INC1"}

    monkeypatch.setattr(ia, "_find_persistent_incident", lambda iid: ("PS4", [_Inc()], _Inc()))
    monkeypatch.setattr(ia, "apply_incident_action",
                        lambda inc, action, **kw: seen.update(kw))
    monkeypatch.setattr(ia, "save_incidents", lambda *a: None)
    auth.add_user("otto", "operator-password", "operator")
    c = _client(app_module)
    _login(c, "otto", "operator-password")
    r = c.post("/api/incidents/INC1/action",
               json={"action": "ACKNOWLEDGE", "actor": "<img src=x onerror=alert(1)>"},
               headers={"X-CSRF-Token": _csrf(c)})
    assert r.status_code == 200, r.text
    assert seen["actor"] == "otto"


# ------------------------------------------------------- headers / hosts

def test_security_headers_and_trusted_hosts(env):
    app_module, auth = env
    c = _client(app_module)
    r = c.get("/healthz")
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    bad = _client(app_module, base_url="http://attacker.example")
    assert bad.get("/healthz").status_code == 400


def test_manage_users_cli_never_takes_password_on_command_line(env, capsys):
    app_module, auth = env
    from dashboard import manage_users
    answers = iter(["short", "short", "a-long-password", "a-long-password"])
    assert manage_users.main(["add", "Bob", "--role", "operator"], prompt=lambda _: next(answers)) == 0
    out = capsys.readouterr().out
    assert "a-long-password" not in out
    assert any(u["username"] == "bob" and u["role"] == "operator" for u in auth.list_users())
    assert auth.login("bob", "a-long-password")
    assert manage_users.main(["remove", "bob"]) == 0
    assert not any(u["username"] == "bob" for u in auth.list_users())
    with pytest.raises(SystemExit):
        manage_users.main(["add", "carl", "--password", "x"])


def test_shell_js_sends_csrf_and_no_page_uses_the_old_token():
    import os
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard", "static")
    for name in os.listdir(root):
        if name.endswith((".html", ".js")):
            text = open(os.path.join(root, name), encoding="utf-8").read()
            assert "/api/auth-token" not in text, name
            assert "X-IBO-Token" not in text, name
    shell = open(os.path.join(root, "shell.js"), encoding="utf-8").read()
    assert "X-CSRF-Token" in shell
    assert re.search(r"'\"'\s*:\s*\"&quot;\"|\"'\": \"&#39;\"", shell)
