from fastapi.testclient import TestClient
from dashboard.app import app

def test_security_headers_are_present():
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"

def test_permissions_policy_is_restrictive():
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert "camera=()" in response.headers["Permissions-Policy"]
    assert "microphone=()" in response.headers["Permissions-Policy"]

def test_history_days_lower_bound_is_rejected():
    with TestClient(app) as client:
        response = client.get("/api/history/SYS/metric?days=0")
    assert response.status_code == 400

def test_history_days_upper_bound_is_rejected():
    with TestClient(app) as client:
        response = client.get("/api/history/SYS/metric?days=366")
    assert response.status_code == 400

def test_health_response_does_not_expose_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "DO_NOT_EXPOSE_THIS_VALUE")
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert "DO_NOT_EXPOSE_THIS_VALUE" not in response.text

def test_system_creation_error_does_not_expose_internal_exception(monkeypatch):
    import dashboard.app as module
    # POST /api/systems is token-gated. The test is about what the handler
    # leaks on failure, which is only reachable once authenticated -- an
    # unauthenticated request stops at 401 and proves nothing about leakage.
    monkeypatch.setenv("IBO_API_TOKEN", "test-token-security")
    def boom(*args, **kwargs):
        raise RuntimeError("SECRET_INTERNAL_PATH")
    monkeypatch.setattr(module, "add_system", boom)
    payload = {
        "name": "TEST",
        "client": "000",
        "connection_name": "TEST",
        "username": "u",
        "password": "p",
    }
    with TestClient(app) as client:
        response = client.post("/api/systems", json=payload,
                               headers={"X-IBO-Token": "test-token-security"})
    assert response.status_code == 500
    assert "SECRET_INTERNAL_PATH" not in response.text
    assert response.json()["detail"] == "Unable to create system."
