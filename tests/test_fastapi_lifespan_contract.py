def test_dashboard_uses_fastapi_lifespan():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "asynccontextmanager" in source
    assert "lifespan=lifespan" in source
    assert '@app.on_event("startup")' not in source
