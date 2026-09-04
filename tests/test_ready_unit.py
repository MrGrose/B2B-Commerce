"""Unit-тесты readiness без живых Postgres/Redis."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from b2b_commerce.infra.health import router as health_router


@pytest.fixture
def ready_app():
    app = FastAPI()
    app.include_router(health_router)
    return app


@pytest.mark.asyncio
async def test_ready_returns_200_when_deps_ok(ready_app):
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    mock_redis.aclose = AsyncMock()

    with (
        patch("b2b_commerce.infra.health.SessionLocal", return_value=mock_session),
        patch("b2b_commerce.infra.health.Redis.from_url", return_value=mock_redis),
    ):
        transport = ASGITransport(app=ready_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/api/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_ready_returns_503_when_postgres_fails(ready_app):
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None
    mock_session.execute = AsyncMock(side_effect=RuntimeError("db down"))

    with patch("b2b_commerce.infra.health.SessionLocal", return_value=mock_session):
        transport = ASGITransport(app=ready_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["postgres"] == "error"
