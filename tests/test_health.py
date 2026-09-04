import pytest
from fastapi.testclient import TestClient

from b2b_commerce.http import format_money, format_rate
from b2b_commerce.main import app

client = TestClient(app)


# Health без БД.
def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.db
def test_api_ready():
    response = client.get("/api/ready")
    if response.status_code == 503:
        checks = response.json().get("checks", {})
        pytest.skip(f"ready deps unavailable: {checks}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"


# Форма входа отдаётся без сессии.
def test_login_page():
    response = client.get("/login")
    assert response.status_code == 200
    assert "Вход" in response.text


# Корень ведёт на логин.
def test_root_redirects_to_login():
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# Каталог без cookie — редирект на логин.
def test_catalog_requires_login():
    response = client.get("/catalog", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# JSON /auth/me без cookie — 401.
def test_api_me_unauthorized():
    response = client.get("/api/auth/me")
    assert response.status_code == 401


def test_session_cookie_secure_in_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    from starlette.responses import Response

    from b2b_commerce.auth.router import _set_session_cookie
    from b2b_commerce.config import get_settings

    response = Response()
    _set_session_cookie(response, "token-value", get_settings())
    cookie = response.headers.get("set-cookie", "")
    assert "secure" in cookie.lower()


def test_session_cookie_not_secure_in_dev(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    from starlette.responses import Response

    from b2b_commerce.auth.router import _set_session_cookie
    from b2b_commerce.config import get_settings

    response = Response()
    _set_session_cookie(response, "token-value", get_settings())
    cookie = response.headers.get("set-cookie", "")
    assert "secure" not in cookie.lower()


def test_format_money_handles_none() -> None:
    assert format_money(None) == "—"
    assert format_money("") == "—"
    assert format_money(1500) == "1 500 ₽"


def test_format_rate_shows_fraction_without_rounding() -> None:
    assert format_rate(88) == "88 ₽"
    assert format_rate("76.36") == "76,36 ₽"
    assert format_rate("76.369") == "76,36 ₽"
    assert format_rate("1234.56") == "1 234,56 ₽"
