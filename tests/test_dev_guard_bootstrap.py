import pytest
from sqlalchemy import select

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import bootstrap_first_admin
from b2b_commerce.dev_guard import require_dev_env, require_local_database


def test_require_dev_env_rejects_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    with pytest.raises(SystemExit):
        require_dev_env(action="dev-seed")


def test_require_dev_env_allows_dev(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    require_dev_env(action="dev-seed")


def test_require_local_database_rejects_remote(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://user:pass@db.example.com:5432/b2b_commerce",
    )
    with pytest.raises(SystemExit):
        require_local_database(action="dev-reset-data")


def test_require_local_database_allows_localhost(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://b2b_commerce:b2b_commerce@127.0.0.1:5432/b2b_commerce",
    )
    require_local_database(action="dev-reset-data")


@pytest.mark.db
@pytest.mark.asyncio
async def test_bootstrap_first_admin_creates_user(db_session):
    admin = await bootstrap_first_admin(db_session, "bootstrap-admin", "password-12")
    assert admin.login == "bootstrap-admin"
    row = await db_session.scalar(
        select(AdminUser).where(AdminUser.login == "bootstrap-admin")
    )
    assert row is not None
    assert row.id == admin.id


@pytest.mark.db
@pytest.mark.asyncio
async def test_bootstrap_first_admin_idempotent(db_session):
    await bootstrap_first_admin(db_session, "bootstrap-admin", "password-12")
    with pytest.raises(ValueError, match="уже есть"):
        await bootstrap_first_admin(db_session, "bootstrap-admin", "other-pass-99")
