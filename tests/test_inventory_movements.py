from decimal import Decimal

import pytest

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.cart.service import upsert_cart_item
from b2b_commerce.catalog.router import _admin_product_context
from b2b_commerce.catalog.service import ProductInput, create_product
from b2b_commerce.companies.service import (
    BillingEntityInput,
    CompanyInput,
    create_billing_entity,
    create_company,
)
from b2b_commerce.config import Settings
from b2b_commerce.enums import ProductStatus
from b2b_commerce.inventory.models import Warehouse
from b2b_commerce.inventory.service import (
    correct_inventory,
    get_availability,
    list_inventory_rows,
)
from b2b_commerce.invoices.service import create_invoice_from_cart


async def _seed_admin(db_session):
    admin = AdminUser(
        login="movement-admin",
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
async def test_correct_inventory_rejects_below_reserved(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(name="T", legal_name="ООО Т", inn="7711111111"),
        admin.id,
    )
    company = await create_company(
        db_session,
        CompanyInput(name="Reserve Co", billing_entity_id=entity.id),
        admin.id,
    )
    product = await create_product(
        db_session,
        ProductInput(name="Reserved item", status=ProductStatus.ACTIVE, sale_price=Decimal("100")),
        admin.id,
    )
    await correct_inventory(db_session, product.id, 10, "seed", admin.id)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 4)
    await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
    )
    with pytest.raises(ValueError, match="зарезервировано"):
        await correct_inventory(db_session, product.id, 2, "import", admin.id)


# Строка остатков админки по товару.
async def _inventory_row_for(db_session, product_id):
    rows = await list_inventory_rows(db_session)
    return next(row for row in rows if row.product_id == product_id)


# reserved ≤ on_hand, available ≥ 0, available = on_hand − reserved.
def _assert_stock_triplet(on_hand: int, reserved: int, available: int) -> None:
    assert reserved <= on_hand
    assert available >= 0
    assert available == on_hand - reserved


# Остаток 50 без резерва → доступно 50.
@pytest.mark.db
@pytest.mark.asyncio
async def test_inventory_available_equals_on_hand_when_unreserved(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await create_product(
        db_session,
        ProductInput(name="Unreserved stock", status=ProductStatus.ACTIVE),
        admin.id,
    )
    await correct_inventory(db_session, product.id, 50, "seed", admin.id)

    row = await _inventory_row_for(db_session, product.id)
    assert row.quantity_on_hand == 50
    assert row.reserved == 0
    assert row.available == 50
    assert row.warehouse_code == "MAIN"
    _assert_stock_triplet(row.quantity_on_hand, row.reserved, row.available)
    assert await get_availability(db_session, product.id) == 50


# Остаток 50, активный резерв 1 → доступно 49; карточка совпадает со списком.
@pytest.mark.db
@pytest.mark.asyncio
async def test_inventory_available_subtracts_active_reservation(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(name="T", legal_name="ООО Т", inn="7711111111"),
        admin.id,
    )
    company = await create_company(
        db_session,
        CompanyInput(name="Triplet Co", billing_entity_id=entity.id),
        admin.id,
    )
    product = await create_product(
        db_session,
        ProductInput(
            name="Reserved stock",
            status=ProductStatus.ACTIVE,
            sale_price=Decimal("100"),
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, 50, "seed", admin.id)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
    )

    row = await _inventory_row_for(db_session, product.id)
    assert row.quantity_on_hand == 50
    assert row.reserved == 1
    assert row.available == 49
    _assert_stock_triplet(row.quantity_on_hand, row.reserved, row.available)
    assert await get_availability(db_session, product.id) == 49

    context = await _admin_product_context(db_session, product.id)
    assert context is not None
    assert context["on_hand"] == row.quantity_on_hand
    assert context["reserved"] == row.reserved
    assert context["availability"] == row.available
