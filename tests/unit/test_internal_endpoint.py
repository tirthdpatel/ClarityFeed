"""Unit tests for backend/api/internal.py — GitHub Actions trigger endpoint."""
from __future__ import annotations

import os

# Override settings before importing app code
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("INTERNAL_SECRET", "test_secret_123")

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.database.session import init_db, drop_all


@pytest.fixture
def client():
    """FastAPI test client with tables created."""
    init_db()
    yield TestClient(app)
    drop_all()


class TestInternalEndpoint:
    """Tests for POST /internal/collect."""

    def test_valid_secret_accepted(self, client) -> None:
        """Valid INTERNAL_SECRET returns 200 and starts collection."""
        response = client.post(
            "/internal/collect",
            headers={"X-Internal-Secret": "test_secret_123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "collection_started"
        assert "timestamp" in data

    def test_invalid_secret_returns_403(self, client) -> None:
        """Invalid INTERNAL_SECRET returns 403 Forbidden."""
        response = client.post(
            "/internal/collect",
            headers={"X-Internal-Secret": "wrong_secret"},
        )
        assert response.status_code == 403

    def test_missing_secret_returns_403(self, client) -> None:
        """Missing INTERNAL_SECRET header returns 403 Forbidden."""
        response = client.post("/internal/collect")
        assert response.status_code == 403

    def test_health_endpoint(self, client) -> None:
        """GET /health returns ok status."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["version"] == "0.1.0"
        assert data["platform"] == "render-free-tier"

    def test_sources_endpoint(self, client) -> None:
        """GET /sources returns a list."""
        response = client.get("/sources")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
