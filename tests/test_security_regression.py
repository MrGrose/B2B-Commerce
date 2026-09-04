"""Финальный чеклист security hardening перед VPS."""

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from b2b_commerce.infra.security import CSRF_HEADER
from b2b_commerce.main import app

client = TestClient(app)


def test_regression_docs_hidden_in_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    prod = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert prod.get(path).status_code == 404


def test_regression_security_headers_no_unpkg():
    response = client.get("/login")
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert "unpkg.com" not in response.headers.get("Content-Security-Policy", "")
    assert "/static/htmx.min.js" in response.text
    assert "unpkg.com" not in response.text


def test_regression_hsts_prod_only(monkeypatch):
    assert "Strict-Transport-Security" not in client.get("/login").headers
    monkeypatch.setenv("APP_ENV", "prod")
    prod = TestClient(app)
    response = prod.get("/login", headers={"Host": "b2b_commerce.example.com"})
    assert response.headers.get("Strict-Transport-Security") == "max-age=31536000"


@pytest.mark.no_auto_csrf
def test_regression_html_post_without_csrf_blocked():
    client.get("/login")
    response = client.post("/login", data={"login": "x", "password": "y"})
    assert response.status_code == 403


@pytest.mark.no_auto_csrf
def test_regression_api_login_without_csrf_allowed(monkeypatch):
    async def _never_limited(*_args, **_kwargs):
        return False

    async def _fake_auth(*_args, **_kwargs):
        return None

    monkeypatch.setattr("b2b_commerce.auth.router.is_login_rate_limited", _never_limited)
    monkeypatch.setattr("b2b_commerce.auth.router.authenticate", _fake_auth)
    response = client.post("/api/auth/login", json={"login": "x", "password": "y"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_regression_get_logout_does_not_revoke(monkeypatch):
    async def _fake_revoke(*_args, **_kwargs):
        raise AssertionError("revoke_token must not be called on GET /logout")

    monkeypatch.setattr("b2b_commerce.auth.router.revoke_token", _fake_revoke)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.get("/login")
        ac.cookies.set("b2b_commerce_session", "keep-me")
        response = await ac.get("/logout", follow_redirects=False)
        assert response.status_code == 303
        assert ac.cookies.get("b2b_commerce_session") == "keep-me"


@pytest.mark.no_auto_csrf
@pytest.mark.asyncio
async def test_regression_cookie_auth_api_requires_csrf_header(monkeypatch):
    async def _fake_revoke(*_args, **_kwargs):
        return None

    monkeypatch.setattr("b2b_commerce.auth.router.revoke_token", _fake_revoke)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.get("/login")
        ac.cookies.set("b2b_commerce_session", "session-token")
        ac.cookies.set("b2b_commerce_csrf", "csrf-token")
        missing = await ac.post("/api/auth/logout")
        assert missing.status_code == 403
        bad = await ac.post(
            "/api/auth/logout",
            headers={CSRF_HEADER: "wrong"},
        )
        assert bad.status_code == 403


def test_regression_media_anonymous_redirects():
    response = client.get("/media/products/x/y.png", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers.get("location") == "/login"


def test_regression_media_traversal_404():
    response = client.get("/media/products/../etc/passwd", follow_redirects=False)
    assert response.status_code in {303, 404}


@pytest.mark.no_auto_csrf
def test_regression_prod_rejects_foreign_origin(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    prod = TestClient(app)
    prod.get("/login", headers={"Host": "b2b_commerce.example.com"})
    token = prod.cookies.get("b2b_commerce_csrf")
    response = prod.post(
        "/login",
        data={"login": "x", "password": "y", "csrf_token": token},
        headers={"Origin": "https://evil.example", "Host": "b2b_commerce.example.com"},
    )
    assert response.status_code == 403
