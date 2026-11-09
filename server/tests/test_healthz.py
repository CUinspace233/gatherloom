from fastapi.testclient import TestClient

from app.main import app


def test_healthz_reports_api_liveness() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
