from decimal import Decimal

import pytest

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.cart.service import (
    add_cart_item_delta,
    get_cart_view,
    remove_cart_item,
    upsert_cart_item,
)
from b2b_commerce.catalog.service import ProductInput, create_product, update_product
from b2b_commerce.companies.service import CompanyInput, create_company
from b2b_commerce.enums import ProductStatus
from b2b_commerce.inventory.models import Warehouse
from b2b_commerce.inventory.service import correct_inventory


# Создаёт админа для тестов.
async def _seed_admin(db_session):
    admin = AdminUser(
        login="cart-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


# Создаёт компанию с товаром и остатком.
async def _seed_company_product(db_session, stock: int = 10):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Cart Co"), admin.id)
    warehouse = Warehouse(code="MAIN", name="Main", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()
    product = await create_product(
        db_session,
        ProductInput(
            name="Cart Product",
            sale_price=Decimal("100"),
            cost_price=Decimal("50"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, stock, "seed", admin.id)
    return admin, company, product


@pytest.mark.db
@pytest.mark.asyncio
async def test_upsert_and_remove_cart_item(db_session):
    _, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    cart = await get_cart_view(db_session, company.company_id)
    assert len(cart.items) == 1
    assert cart.items[0].quantity == 2
    assert cart.subtotal == Decimal("200")

    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    cart = await get_cart_view(db_session, company.company_id)
    assert cart.items[0].quantity == 3

    await remove_cart_item(db_session, company.company_id, product.id)
    cart = await get_cart_view(db_session, company.company_id)
    assert cart.items == []



@pytest.mark.db
@pytest.mark.asyncio
async def test_add_cart_item_delta_increments(db_session):
    _, company, product = await _seed_company_product(db_session)
    await add_cart_item_delta(db_session, company.company_id, product.id, 1)
    await add_cart_item_delta(db_session, company.company_id, product.id, 2)
    cart = await get_cart_view(db_session, company.company_id)
    assert cart.items[0].quantity == 3

@pytest.mark.db
@pytest.mark.asyncio
async def test_upsert_rejects_inactive_product(db_session):
    admin, company, product = await _seed_company_product(db_session)
    await update_product(
        db_session,
        product.id,
        ProductInput(
            name=product.name,
            sale_price=Decimal("100"),
            cost_price=Decimal("50"),
            status=ProductStatus.INACTIVE,
        ),
        admin.id,
    )
    with pytest.raises(ValueError, match="недоступен"):
        await upsert_cart_item(db_session, company.company_id, product.id, 1)


@pytest.mark.db
@pytest.mark.asyncio
async def test_upsert_rejects_deleted_product(db_session):
    from b2b_commerce.catalog.service import soft_delete_product

    admin, company, product = await _seed_company_product(db_session)
    await soft_delete_product(db_session, product.id, admin.id)
    with pytest.raises(ValueError, match="недоступен"):
        await upsert_cart_item(db_session, company.company_id, product.id, 1)
