"""
tests/integration/test_health_endpoint.py — Integration Tests for /health
===========================================================================

WHAT IS AN INTEGRATION TEST vs UNIT TEST?
------------------------------------------
Unit test:       Tests one function/class in isolation (mocks everything else).
Integration test: Tests multiple components working together through
                  real HTTP requests against the full FastAPI app.

This file tests the /health endpoint end-to-end:
- The request goes through middleware, routing, and the handler.
- We mock the database check so tests don't need a real PostgreSQL.

LESSON: Integration tests catch wiring bugs — e.g., a route is defined
but not registered in api.py, or middleware breaks a specific endpoint.
Unit tests would miss those.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
class TestHealthEndpoint:
    """Tests for GET /health"""

    async def test_health_returns_200(self, app_client):
        """
        The health endpoint should always return 200 OK.
        We patch check_database_connection to return True
        so this test doesn't need a real database.
        """
        with patch(
            "src.main.check_database_connection",
            new=AsyncMock(return_value=True),
        ):
            response = await app_client.get("/health")

        assert response.status_code == 200

    async def test_health_response_structure(self, app_client):
        """
        Response must contain all required fields.
        Flutter's app initialisation reads these fields.
        """
        with patch(
            "src.main.check_database_connection",
            new=AsyncMock(return_value=True),
        ):
            response = await app_client.get("/health")

        body = response.json()
        assert "status" in body
        assert "app" in body
        assert "version" in body
        assert "environment" in body
        assert "database" in body

    async def test_health_when_db_connected(self, app_client):
        """When database is reachable, status should be 'healthy'."""
        with patch(
            "src.main.check_database_connection",
            new=AsyncMock(return_value=True),
        ):
            response = await app_client.get("/health")

        body = response.json()
        assert body["status"] == "healthy"
        assert body["database"] == "ok"

    async def test_health_when_db_unreachable(self, app_client):
        """
        When database is unreachable, status should be 'degraded'.
        The endpoint still returns 200 — the app is up, just impaired.
        This is the correct pattern for health checks: return 200 for
        'alive' and 503 only when the app truly cannot serve any traffic.
        """
        with patch(
            "src.main.check_database_connection",
            new=AsyncMock(return_value=False),
        ):
            response = await app_client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["database"] == "unreachable"

    async def test_nonexistent_route_returns_404(self, app_client):
        """
        A request to an unknown path should return 404.
        This verifies our exception handlers don't swallow routing errors.
        """
        response = await app_client.get("/this-does-not-exist")
        assert response.status_code == 404

    async def test_cors_header_present(self, app_client):
        """
        CORS headers should be present on responses.
        Flutter makes cross-origin requests, so this is essential.
        """
        with patch(
            "src.main.check_database_connection",
            new=AsyncMock(return_value=True),
        ):
            response = await app_client.get(
                "/health",
                headers={"Origin": "http://flutter-app.local"},
            )

        # With CORS_ORIGINS=["*"], any origin should be allowed
        assert "access-control-allow-origin" in response.headers
