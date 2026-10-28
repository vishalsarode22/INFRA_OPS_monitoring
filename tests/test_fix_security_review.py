"""Fixes from the security review (Oct 2026)."""
import os

import pytest
from fastapi.testclient import TestClient

from test_fix_monitoring_log import env  # noqa: F401  (fixture: PS4=Metro, CEQ=another customer)

BROWSER = {"Sec-Fetch-Site": "same-origin"}


def _login(app_module, user, pw):
    c = TestClient(app_module.app)
    r = c.post("/login", data={"username": user, "password": pw, "next": "/"}, headers=BROWSER,
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def test_scoped_user_cannot_download_another_customers_report(env, tmp_path, monkeypatch):  # noqa: F811
    app_module, auth, _ops = env
    monkeypatch.setenv("IBO_AUTH", "1")
    root = tmp_path / "reports"
    for rel in ("2026-10-25/CEQ_report.xlsx", "2026-10-25/CEQ/CEQ_sheet.pdf",
                "2026-10-25/PS4_report.xlsx", "2026-10-25/PS4/PS4_sheet.pdf", "2026-10-25/orphan.xlsx"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("secret " + rel)
    monkeypatch.setattr(app_module, "REPORTS_DIR", str(root))
    auth.add_user("metro", "metro-password-1", "viewer", "Metro")
    auth.add_user("ada", "admin-password!", "admin")
    v = _login(app_module, "metro", "metro-password-1")
    assert v.get("/reports/2026-10-25/PS4_report.xlsx").status_code == 200
    assert v.get("/reports/2026-10-25/PS4/PS4_sheet.pdf").status_code == 200
    assert v.get("/reports/2026-10-25/CEQ_report.xlsx").status_code == 404      # was 200
    assert v.get("/reports/2026-10-25/CEQ/CEQ_sheet.pdf").status_code in (403, 404)
    assert v.get("/reports/2026-10-25/orphan.xlsx").status_code == 404          # not attributable
    a = _login(app_module, "ada", "admin-password!")
    assert a.get("/reports/2026-10-25/CEQ_report.xlsx").status_code == 200     # unscoped admin


def test_report_owner_attribution():
    import dashboard.app as A
    systems = [{"name": "CEAT S4P"}, {"name": "CEAT S4_QAS"}, {"name": "PS4"}]
    assert A._report_owner("2026-10-25/CEAT_S4P/x.pdf", systems) == "CEAT S4P"
    assert A._report_owner("2026-10-25/CEAT_S4_QAS_report.xlsx", systems) == "CEAT S4_QAS"
    assert A._report_owner("2026-10-25/PS4.xlsx", systems) == "PS4"
    assert A._report_owner("2026-10-25/PS40_report.xlsx", systems) is None   # prefix must end at a separator


def test_bare_login_post_is_refused_but_browser_form_works(env, monkeypatch):  # noqa: F811
    app_module, auth, _ops = env
    monkeypatch.setenv("IBO_AUTH", "1")
    auth.add_user("ada", "admin-password!", "admin")
    c = TestClient(app_module.app)
    r = c.post("/login", data={"username": "ada", "password": "admin-password!"}, follow_redirects=False)
    assert r.status_code == 403 and "Cross-site" in r.text
    r = c.post("/login", data={"username": "ada", "password": "admin-password!"},
               headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303


def test_login_lockout_uses_real_client_behind_trusted_proxy(monkeypatch):
    import dashboard.app as A

    class R:
        def __init__(self, peer, **h):
            self.client = type("C", (), {"host": peer})()
            self.headers = {k.replace("_", "-"): v for k, v in h.items()}

    monkeypatch.setenv("IBO_TRUST_PROXY", "0")
    assert A._client_address(R("127.0.0.1", x_forwarded_for="10.1.2.3")) == "127.0.0.1"
    monkeypatch.setenv("IBO_TRUST_PROXY", "1")
    assert A._client_address(R("127.0.0.1", x_forwarded_for="6.6.6.6, 10.1.2.3")) == "10.1.2.3"
    assert A._client_address(R("10.9.9.9", x_forwarded_for="1.1.1.1")) == "10.9.9.9"   # not via our proxy


def test_rate_limiter_uses_the_proxy_appended_address():
    from dashboard import rate_limit
    mw = rate_limit.RateLimitMiddleware.__new__(rate_limit.RateLimitMiddleware)
    mw.settings = type("S", (), {"trust_proxy": True})()
    scope = {"headers": [(b"x-forwarded-for", b"6.6.6.6, 10.1.2.3")], "client": ("127.0.0.1", 1)}
    assert mw._client_ip(scope) == "10.1.2.3"


def test_env_backups_go_outside_the_project_and_are_pruned(tmp_path, monkeypatch):
    from core.env_safety import backup_env
    envf = tmp_path / ".env"
    envf.write_text("SMTP_PASSWORD=x\n")
    monkeypatch.setenv("IBO_RUNTIME_DIR", str(tmp_path / "runtime"))
    bdir = tmp_path / "runtime" / "env_backups"
    bdir.mkdir(parents=True)
    for d in range(1, 12):
        (bdir / f"env-202610{d:02d}.bak").write_text("old")
    dest = backup_env(str(envf), keep=7)
    assert dest and os.path.dirname(dest) == str(bdir)
    assert len(list(bdir.iterdir())) == 7
    assert not list(tmp_path.glob(".env.bak*"))
