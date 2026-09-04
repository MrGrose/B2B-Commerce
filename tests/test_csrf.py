import pytest
from httpx import ASGITransport, AsyncClient

from b2b_commerce.main import app


@pytest.fixture
async def csrf_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _csrf_token(client: AsyncClient) -> str:
    token = client.cookies.get("b2b_commerce_csrf")
    if token:
        return token
    await client.get("/login")
    return client.cookies.get("b2b_commerce_csrf")


@pytest.mark.asyncio
async def test_login_form_csrf_valid(csrf_client, monkeypatch):
    async def _never_limited(*_args, **_kwargs):
        return False

    async def _fake_auth(*_args, **_kwargs):
        return None

    monkeypatch.setattr("b2b_commerce.auth.router.is_login_rate_limited", _never_limited)
    monkeypatch.setattr("b2b_commerce.auth.router.authenticate", _fake_auth)
    await csrf_client.get("/login")
    token = await _csrf_token(csrf_client)
    response = await csrf_client.post(
        "/login",
        data={"login": "nobody", "password": "wrong-password", "csrf_token": token},
    )
    assert response.status_code == 401


@pytest.mark.no_auto_csrf
@pytest.mark.asyncio
async def test_login_form_csrf_missing(csrf_client):
    await csrf_client.get("/login")
    response = await csrf_client.post(
        "/login",
        data={"login": "nobody", "password": "wrong-password"},
    )
    assert response.status_code == 403


@pytest.mark.no_auto_csrf
@pytest.mark.asyncio
async def test_login_form_csrf_invalid(csrf_client):
    await csrf_client.get("/login")
    response = await csrf_client.post(
        "/login",
        data={"login": "nobody", "password": "wrong-password", "csrf_token": "bad-token"},
    )
    assert response.status_code == 403


@pytest.mark.no_auto_csrf
@pytest.mark.asyncio
async def test_api_login_without_csrf_still_works(csrf_client, monkeypatch):
    async def _never_limited(*_args, **_kwargs):
        return False

    async def _fake_auth(*_args, **_kwargs):
        return None

    monkeypatch.setattr("b2b_commerce.auth.router.is_login_rate_limited", _never_limited)
    monkeypatch.setattr("b2b_commerce.auth.router.authenticate", _fake_auth)
    response = await csrf_client.post(
        "/api/auth/login",
        json={"login": "nobody", "password": "wrong-password"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_logout_does_not_revoke_session(csrf_client, monkeypatch):
    async def _fake_revoke(*_args, **_kwargs):
        return None

    monkeypatch.setattr("b2b_commerce.auth.router.revoke_token", _fake_revoke)
    await csrf_client.get("/login")
    csrf_client.cookies.set("b2b_commerce_session", "session-token")
    response = await csrf_client.get("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert csrf_client.cookies.get("b2b_commerce_session") == "session-token"
