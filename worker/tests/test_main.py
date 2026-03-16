"""Tests for the FastAPI worker service routes.

Uses httpx AsyncClient with the FastAPI test transport.
Dependencies are not wired (lifespan skipped) for unit-level tests;
only the health endpoint is exercised here.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.models import HealthResponse, TaskPhase, TaskStatus


# ---------------------------------------------------------------------------
# Minimal app fixture (no external deps required)
# ---------------------------------------------------------------------------


def _create_test_app() -> FastAPI:
    """Build a minimal FastAPI app with just the health route."""
    test_app = FastAPI()

    @test_app.get("/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        return HealthResponse()

    return test_app


@pytest.fixture
def test_app() -> FastAPI:
    return _create_test_app()


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """GET /health returns service status."""

    @pytest.mark.asyncio
    async def test_health_returns_200(self, test_app: FastAPI) -> None:
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_health_response_body(self, test_app: FastAPI) -> None:
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

        body = response.json()
        assert body["status"] == "healthy"
        assert body["version"] == "0.1.0"


# ---------------------------------------------------------------------------
# Task status model (unit-level, no HTTP)
# ---------------------------------------------------------------------------


class TestTaskStatusModel:
    """TaskStatus round-trip through JSON."""

    def test_running_status_serializes(self) -> None:
        status = TaskStatus(task_id="t-100", phase=TaskPhase.RUNNING)
        data = status.model_dump()
        assert data["phase"] == "Running"
        assert data["task_id"] == "t-100"
