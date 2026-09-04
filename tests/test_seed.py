from pathlib import Path

import pytest
from sqlalchemy import func, select

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.catalog.import_service import ImportService
from b2b_commerce.catalog.models import Product
from b2b_commerce.inventory.models import Warehouse

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_XLSX = REPO_ROOT / "tests" / "fixtures" / "demo_price_list.xlsx"


async def _seed_admin(db_session):
    admin = AdminUser(
        login="seed-test-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


async def _seed_warehouse(db_session):
    warehouse = Warehouse(code="MAIN", name="Основной", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()
    return warehouse


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_import_loads_demo_products(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)

    report = await ImportService().run_from_path(FIXTURES_XLSX, db_session, admin.id)

    assert report.created == 12
    assert report.updated == 0
    assert report.errors == 0
    count = await db_session.scalar(select(func.count()).select_from(Product))
    assert count == 12


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_import_is_idempotent(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)

    first = await ImportService().run_from_path(FIXTURES_XLSX, db_session, admin.id)
    second = await ImportService().run_from_path(FIXTURES_XLSX, db_session, admin.id)

    assert first.created == 12
    assert second.created == 0
    assert second.updated == 12
    count = await db_session.scalar(select(func.count()).select_from(Product))
    assert count == 12
