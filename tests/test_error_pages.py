import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from b2b_commerce.main import app, unhandled_exception_handler

client = TestClient(app)


def test_not_found_html() -> None:
    response = client.get("/this-page-does-not-exist-11d", follow_redirects=False)
    assert response.status_code == 404
    assert "Страница не найдена" in response.text
    assert "error-page" in response.text
    assert "На вход" in response.text


def test_api_not_found_json() -> None:
    response = client.get("/api/this-endpoint-does-not-exist-11d")
    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"


@pytest.mark.asyncio
async def test_unhandled_exception_html_uses_server_error_template() -> None:
    request = MagicMock()
    request.url.path = "/catalog"
    response = await unhandled_exception_handler(request, RuntimeError("boom"))
    assert response.status_code == 500
    assert response.template.name == "server_error.html"


@pytest.mark.asyncio
async def test_unhandled_exception_api_returns_json() -> None:
    request = MagicMock()
    request.url.path = "/api/catalog"
    response = await unhandled_exception_handler(request, RuntimeError("boom"))
    assert response.status_code == 500
    payload = json.loads(response.body.decode())
    assert payload == {"detail": "Внутренняя ошибка сервера"}
