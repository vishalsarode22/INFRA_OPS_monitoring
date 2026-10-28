from fastapi.testclient import TestClient

from dashboard.app import app


def test_healthz_contract():
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "InfraBeatOps"


def test_readyz_contract():
    client = TestClient(app)
    response = client.get("/readyz")
    assert response.status_code in {200, 503}
    payload = response.json()
    assert payload["status"] in {"ready", "not_ready"}
    assert "checks" in payload


def test_readyz_never_exposes_secrets():
    client = TestClient(app)
    payload = client.get("/readyz").json()
    text = str(payload).lower()
    assert "password" not in text
    assert "api_key" not in text
