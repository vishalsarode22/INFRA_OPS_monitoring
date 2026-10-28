"""Security review 28.10.2026 -- fixes for findings 1-4, 6-8."""


def test_safe_next_rejects_control_characters_and_hosts():
    import dashboard.app as app
    for bad in ["/\t/evil.com", "/\n/evil.com", "//evil.com", "/\\evil", "https://evil.com", "/ /evil.com"]:
        assert app._safe_next(bad) == "/", bad
    assert app._safe_next("/wall?project=CEAT") == "/wall?project=CEAT"


def test_watchdog_name_is_never_a_path():
    from core import reachability as R
    assert R.watchdog_status("..\\..\\dashboard\\snapshots\\CEQ") is None
    assert R.watchdog_status("../../etc/passwd") is None


def test_access_categories():
    from core import access
    assert access.category("/api/ops/actions/7/approve", "POST") == "run"
    assert access.category("/api/delivery-settings", "GET") == "admin"
    assert access.category("/api/delivery-accounts", "GET") == "admin"
    assert access.category("/api/reach-test", "GET") == "admin"


def test_scope_check_normalises_system_names(monkeypatch):
    import dashboard.app as app
    from core import config_loader
    all_ = [{"name": "CEQ"}, {"name": "PS4"}]
    monkeypatch.setattr(config_loader, "get_all_systems", lambda: all_)
    monkeypatch.setattr(app, "get_systems", lambda: [{"name": "PS4"}])

    class Req:
        def __init__(self, path, q=()):
            self.url = type("U", (), {"path": path})()
            self.query_params = type("Q", (), {"multi_items": lambda s: list(q)})()
    p = {"customers": {"Metro"}, "systems": {"PS4"}}
    for path in ["/api/status/CEQ.", "/api/status/C.EQ", "/api/status/ceq", "/api/status/CEQ%20", "/api/status/CEQ"]:
        assert app._scope_violation(Req(path), p), path
    assert not app._scope_violation(Req("/api/status/PS4"), p)
    assert app._scope_violation(Req("/api/alerts", [("system", "ceq.")]), p)


def test_wrong_passwords_from_one_machine_do_not_lock_the_user_everywhere():
    from core import auth
    auth._failures.clear()
    for _ in range(auth.LOCK_AFTER_USER):
        auth._note_failure("admin", "10.9.9.9")
    assert auth.login_locked("admin", "10.9.9.9")
    assert not auth.login_locked("admin", "10.1.0.50")
    auth._clear_failures("admin")
    assert not auth.login_locked("admin", "10.9.9.9")
    auth._failures.clear()


def test_retention_prunes_history_lines(tmp_path, monkeypatch):
    from core import store
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "s.sqlite"))
    if hasattr(store._local, "conn"):
        del store._local.conn
    store._initialised = False
    store.conn().execute("INSERT INTO lines(path, ts, line) VALUES ('a','2020-01-01 00:00:00','old')")
    store.conn().execute("INSERT INTO lines(path, ts, line) VALUES ('a','2099-01-01 00:00:00','new')")
    assert store.prune_lines(400) == 1
    assert [r[0] for r in store.conn().execute("SELECT line FROM lines")] == ["new"]
