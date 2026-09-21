import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import HealthResponse, app

client = TestClient(app)


def test_healthz_returns_200_with_expected_shape():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ingestion"}


def test_health_response_rejects_missing_fields():
    with pytest.raises(ValidationError):
        HealthResponse(status="ok")
