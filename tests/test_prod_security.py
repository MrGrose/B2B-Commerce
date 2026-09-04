import importlib

import pytest
from fastapi.testclient import TestClient

from b2b_commerce.config import Settings, validate_prod_settings
from b2b_commerce.main import app

client = TestClient(app)


def test_validate_prod_settings_skips_dev():
    settings = Settings(app_env="dev", admin_password="changeme")
    validate_prod_settings(settings)


def test_validate_prod_settings_rejects_default_admin_password(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    settings = Settings(
        app_env="prod",
        admin_password="admin123",
        allowed_hosts="example.com",
        minio_secret_key="unique-minio-secret",
        database_url="postgresql+asyncpg://app:strong-pass@db.internal:5432/b2b_commerce",
    )
    with pytest.raises(SystemExit, match="ADMIN_PASSWORD"):
        validate_prod_settings(settings)




def test_validate_prod_settings_rejects_change_me_placeholder(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    settings = Settings(
        app_env="prod",
        admin_password="CHANGE_ME",
        allowed_hosts="example.com",
        minio_secret_key="unique-minio-secret",
        database_url="postgresql+asyncpg://app:strong-pass@db.internal:5432/b2b_commerce",
    )
    with pytest.raises(SystemExit, match="ADMIN_PASSWORD"):
        validate_prod_settings(settings)

def test_validate_prod_settings_rejects_default_db_and_minio(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    settings = Settings(
        app_env="prod",
        admin_password="unique-admin-pass-12",
        allowed_hosts="example.com",
        minio_secret_key="b2b-commerce-secret",
        database_url="postgresql+asyncpg://b2b_commerce:b2b_commerce@db.internal:5432/b2b_commerce",
    )
    with pytest.raises(SystemExit, match="MINIO_SECRET_KEY"):
        validate_prod_settings(settings)


def test_validate_prod_settings_rejects_localhost_only_hosts(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    settings = Settings(
        app_env="prod",
        admin_password="unique-admin-pass-12",
        allowed_hosts="localhost,127.0.0.1",
        minio_secret_key="unique-minio-secret",
        database_url="postgresql+asyncpg://app:strong-pass@db.internal:5432/b2b_commerce",
    )
    with pytest.raises(SystemExit, match="ALLOWED_HOSTS"):
        validate_prod_settings(settings)


def test_validate_prod_settings_accepts_safe_prod_config(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    settings = Settings(
        app_env="prod",
        admin_password="unique-admin-pass-12",
        allowed_hosts="b2b_commerce.example.com,127.0.0.1",
        minio_secret_key="unique-minio-secret",
        database_url="postgresql+asyncpg://app:strong-pass@db.internal:5432/b2b_commerce",
    )
    validate_prod_settings(settings)


def test_validate_prod_settings_accepts_public_host_with_loopback(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    settings = Settings(
        app_env="prod",
        admin_password="unique-admin-pass-12",
        allowed_hosts="example.com,127.0.0.1",
        minio_secret_key="unique-minio-secret",
        database_url="postgresql+asyncpg://app:strong-pass@db.internal:5432/b2b_commerce",
    )
    validate_prod_settings(settings)


def _prod_client(monkeypatch) -> TestClient:
    for key, value in {
        "APP_ENV": "prod",
        "ALLOWED_HOSTS": "example.com,127.0.0.1",
        "ADMIN_PASSWORD": "unique-admin-pass-12",
        "MINIO_SECRET_KEY": "unique-minio-secret",
        "DATABASE_URL": "postgresql+asyncpg://app:strong-pass@db.internal:5432/b2b_commerce",
    }.items():
        monkeypatch.setenv(key, value)
    import b2b_commerce.config
    import b2b_commerce.main

    importlib.reload(b2b_commerce.config)
    importlib.reload(b2b_commerce.main)
    return TestClient(b2b_commerce.main.app)


def test_prod_trusted_host_allows_loopback_and_public_health(monkeypatch):
    prod_client = _prod_client(monkeypatch)

    assert prod_client.get("/api/health", headers={"Host": "127.0.0.1"}).status_code == 200
    assert prod_client.get("/api/health", headers={"Host": "example.com"}).status_code == 200

    ready = prod_client.get("/api/ready", headers={"Host": "127.0.0.1"})
    assert ready.status_code != 400

    evil = prod_client.get("/api/health", headers={"Host": "evil.example.com"})
    assert evil.status_code == 400
    assert evil.text == "Invalid host header"


def test_docs_available_in_dev():
    response = client.get("/docs")
    assert response.status_code == 200


def test_docs_hidden_in_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    prod_client = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert prod_client.get(path).status_code == 404


def test_security_headers_on_login():
    response = client.get("/login")
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert "unpkg.com" not in response.headers.get("Content-Security-Policy", "")
    assert "unpkg.com" not in response.text


def test_hsts_only_in_prod(monkeypatch):
    dev = client.get("/login")
    assert "Strict-Transport-Security" not in dev.headers

    monkeypatch.setenv("APP_ENV", "prod")
    prod_client = TestClient(app)
    response = prod_client.get("/login", headers={"Host": "b2b_commerce.example.com"})
    assert response.headers.get("Strict-Transport-Security") == "max-age=31536000"


def test_htmx_served_locally():
    response = client.get("/login")
    assert "/static/htmx.min.js" in response.text
    assert "unpkg.com" not in response.text
