import os
import re
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from b2b_commerce.db import Base, get_session
from b2b_commerce.infra.security import CSRF_EXEMPT_PATHS, CSRF_FORM_FIELD, CSRF_HEADER
from b2b_commerce.main import app
from b2b_commerce.tables import load_models

DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://b2b_commerce:b2b_commerce@127.0.0.1:5432/b2b_commerce_test",
)


# Пропускает DB-тесты, если Postgres недоступен.
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "db: tests that require PostgreSQL",
    )
    config.addinivalue_line(
        "markers",
        "no_auto_csrf: disable automatic CSRF injection in HTTP client helpers",
    )
    config.addinivalue_line(
        "markers",
        "no_rate_limit_client_key: use real request.client.host for rate limit tests",
    )


_TEST_DB_SETUP_LOCK_ID = 0x7061646C  # serializes schema reset on shared b2b_commerce_test


@pytest_asyncio.fixture
async def db_engine():
    load_models()
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"SELECT pg_advisory_lock({_TEST_DB_SETUP_LOCK_ID})"))
            try:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(
                    text(
                        "CREATE SEQUENCE IF NOT EXISTS invoice_number_seq "
                        "START WITH 1 INCREMENT BY 1"
                    )
                )
            finally:
                await conn.execute(text(f"SELECT pg_advisory_unlock({_TEST_DB_SETUP_LOCK_ID})"))
    except Exception as exc:
        await engine.dispose()
        pytest.fail(f"PostgreSQL недоступен для тестов ({DATABASE_URL}): {exc}")
    yield engine
    await engine.dispose()




@pytest_asyncio.fixture
async def client(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr("b2b_commerce.http.SessionLocal", factory)

    async def override_get_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


def _needs_csrf(url: str, kwargs: dict[str, Any]) -> bool:
    path = str(url)
    if path in CSRF_EXEMPT_PATHS:
        return False
    if kwargs.get("data") is not None:
        data = kwargs["data"]
        if isinstance(data, dict) and CSRF_FORM_FIELD in data:
            return False
    if kwargs.get("headers") and CSRF_HEADER in (kwargs.get("headers") or {}):
        return False
    return True


def _apply_csrf_token(kwargs: dict[str, Any], token: str) -> None:
    has_json = kwargs.get("json") is not None
    empty_body = kwargs.get("data") is None and kwargs.get("files") is None
    if has_json or empty_body:
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault(CSRF_HEADER, token)
        kwargs["headers"] = headers
        return
    data = kwargs.get("data")
    if isinstance(data, dict):
        kwargs["data"] = {**data, CSRF_FORM_FIELD: token}
        return
    headers = dict(kwargs.get("headers") or {})
    headers.setdefault(CSRF_HEADER, token)
    kwargs["headers"] = headers


# Достаёт CSRF-токен из cookie клиента или HTML meta.
def csrf_token_from_client(client: TestClient) -> str:
    token = client.cookies.get("b2b_commerce_csrf")
    if token:
        return token
    response = client.get("/login")
    token = client.cookies.get("b2b_commerce_csrf")
    if token:
        return token
    match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    if match:
        return match.group(1)
    raise AssertionError("CSRF-токен не найден")


# Добавляет CSRF в form data.
def with_csrf_form(client: TestClient, data: dict) -> dict:
    return {**data, CSRF_FORM_FIELD: csrf_token_from_client(client)}


# Заголовок CSRF для cookie-auth API.
def csrf_headers(client: TestClient) -> dict[str, str]:
    return {CSRF_HEADER: csrf_token_from_client(client)}


async def _async_csrf_token(client: AsyncClient) -> str:
    token = client.cookies.get("b2b_commerce_csrf")
    if token:
        return token
    response = await client.get("/login")
    token = client.cookies.get("b2b_commerce_csrf")
    if token:
        return token
    match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    if match:
        return match.group(1)
    raise AssertionError("CSRF-токен не найден")


@pytest.fixture(autouse=True)
def _unique_rate_limit_client_key(request, monkeypatch):
    if request.node.get_closest_marker("no_rate_limit_client_key"):
        return
    key = f"test-{abs(hash(request.node.nodeid))}"
    monkeypatch.setattr("b2b_commerce.auth.router._client_key", lambda _request, k=key: k)


@pytest.fixture(autouse=True)
def _auto_csrf_testclient_post(request, monkeypatch):
    if request.node.get_closest_marker("no_auto_csrf"):
        return
    original_post = TestClient.post

    def post_with_csrf(self, url, *args, **kwargs):
        if _needs_csrf(url, kwargs):
            _apply_csrf_token(kwargs, csrf_token_from_client(self))
        return original_post(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "post", post_with_csrf)


@pytest.fixture(autouse=True)
def _auto_csrf_asyncclient_post(request, monkeypatch):
    if request.node.get_closest_marker("no_auto_csrf"):
        return
    original_post = AsyncClient.post

    async def post_with_csrf(self, url, *args, **kwargs):
        if _needs_csrf(url, kwargs):
            _apply_csrf_token(kwargs, await _async_csrf_token(self))
        return await original_post(self, url, *args, **kwargs)

    monkeypatch.setattr(AsyncClient, "post", post_with_csrf)
    original_put = AsyncClient.put

    async def put_with_csrf(self, url, *args, **kwargs):
        if _needs_csrf(url, kwargs):
            _apply_csrf_token(kwargs, await _async_csrf_token(self))
        return await original_put(self, url, *args, **kwargs)

    monkeypatch.setattr(AsyncClient, "put", put_with_csrf)
    original_delete = AsyncClient.delete

    async def delete_with_csrf(self, url, *args, **kwargs):
        if _needs_csrf(url, kwargs):
            _apply_csrf_token(kwargs, await _async_csrf_token(self))
        return await original_delete(self, url, *args, **kwargs)

    monkeypatch.setattr(AsyncClient, "delete", delete_with_csrf)
    original_patch = AsyncClient.patch

    async def patch_with_csrf(self, url, *args, **kwargs):
        if _needs_csrf(url, kwargs):
            _apply_csrf_token(kwargs, await _async_csrf_token(self))
        return await original_patch(self, url, *args, **kwargs)

    monkeypatch.setattr(AsyncClient, "patch", patch_with_csrf)
