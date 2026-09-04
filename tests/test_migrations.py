import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_TEST_DB = os.environ.get(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://b2b_commerce:b2b_commerce@127.0.0.1:5432/b2b_commerce_migtest",
)
HEAD_REVISION = "0001_initial"


# Запускает Alembic с указанной БД.
def _run_alembic(database_url: str, *args: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout


# Создаёт тестовую БД, если её ещё нет.
async def _ensure_database(database_url: str) -> None:
    db_name = database_url.rsplit("/", 1)[-1]
    admin_url = f"{database_url.rsplit('/', 1)[0]}/postgres"
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name},
            )
            exists = result.scalar()
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except Exception as exc:
        pytest.fail(f"PostgreSQL недоступен для migration tests: {exc}")
    finally:
        await engine.dispose()


# Сбрасывает public schema.
async def _reset_schema(database_url: str) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


# Возвращает имена колонок таблицы.
async def _table_columns(database_url: str, table: str) -> set[str]:
    engine = create_async_engine(database_url, pool_pre_ping=True)

    def _read(connection):
        return {column["name"] for column in inspect(connection).get_columns(table)}

    async with engine.connect() as conn:
        columns = await conn.run_sync(_read)
    await engine.dispose()
    return columns


# Возвращает version_num из alembic_version.
async def _alembic_version(database_url: str) -> str:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT version_num FROM alembic_version"))
        value = row.scalar_one()
    await engine.dispose()
    return value


# Возвращает имена sequence в public schema.
async def _sequence_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, pool_pre_ping=True)

    def _read(connection):
        return set(inspect(connection).get_sequence_names())

    async with engine.connect() as conn:
        names = await conn.run_sync(_read)
    await engine.dispose()
    return names


@pytest.mark.db
@pytest.mark.asyncio
async def test_fresh_database_upgrade_head():
    await _ensure_database(MIGRATION_TEST_DB)
    await _reset_schema(MIGRATION_TEST_DB)
    _run_alembic(MIGRATION_TEST_DB, "upgrade", "head")

    version = await _alembic_version(MIGRATION_TEST_DB)
    assert version == HEAD_REVISION

    product_columns = await _table_columns(MIGRATION_TEST_DB, "products")
    assert "sku" not in product_columns
    assert {"model_year", "deleted_at", "search_tsv", "brand_name"}.issubset(product_columns)

    invoice_columns = await _table_columns(MIGRATION_TEST_DB, "invoices")
    assert "buyer_kpp" in invoice_columns

    item_columns = await _table_columns(MIGRATION_TEST_DB, "invoice_items")
    assert "sort_order" in item_columns

    category_columns = await _table_columns(MIGRATION_TEST_DB, "categories")
    assert "margin_percent" in category_columns

    sequences = await _sequence_names(MIGRATION_TEST_DB)
    assert "invoice_number_seq" in sequences
