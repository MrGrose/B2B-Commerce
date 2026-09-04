from decimal import Decimal

import pytest
from httpx import AsyncClient

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.cart.service import upsert_cart_item
from b2b_commerce.catalog.models import Brand, Category, ProductImage
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
    list_active_reservations_for_product,
    list_admin_product_stock,
    reserved_quantity,
)
from b2b_commerce.invoices.service import create_invoice_from_cart


async def _seed_admin(db_session):
    admin = AdminUser(
        login="inventory-ui-admin",
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


async def _seed_product(db_session, admin, on_hand: int = 10):
    product = await create_product(
        db_session,
        ProductInput(
            name="Inventory UI Product",
            sale_price=Decimal("200"),
            cost_price=Decimal("80"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, on_hand, "seed", admin.id)
    return product


async def _seed_company(db_session, admin):
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name="Seller IE",
            legal_name="ИП Продавец",
            inn="7701234567",
            kpp="770101001",
            legal_address="Москва",
            bank_name="Тест Банк",
            bik="044525225",
            bank_account="40702810100000000001",
            corr_account="30101810400000000225",
        ),
        admin.id,
    )
    company_name = "Inventory UI Co"
    company = await create_company(
        db_session,
        CompanyInput(
            name=company_name,
            legal_name="ООО Тест",
            inn="7707123456",
            contact_email="ui@example.com",
            contact_phone="+79991112233",
            billing_entity_id=entity.id,
        ),
        admin.id,
    )
    return company, company_name



async def _login(client: AsyncClient, login: str, password: str):
    return await client.post(
        "/login",
        data={"login": login, "password": password},
        follow_redirects=False,
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_inventory_redirects_to_products(client, db_session):
    await _seed_admin(db_session)
    await _login(client, "inventory-ui-admin", "admin-pass")

    response = await client.get("/admin/inventory", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/products"


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_products_list_shows_stock_columns(client, db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await _seed_product(db_session, admin, on_hand=12)

    await _login(client, "inventory-ui-admin", "admin-pass")
    page = await client.get("/admin/products")

    assert page.status_code == 200
    assert "Остаток" in page.text
    assert "Резерв" in page.text
    assert "Доступно" in page.text
    assert product.name in page.text
    assert 'class="catalog-grid"' in page.text
    assert 'class="product-card"' in page.text
    assert f'href="/admin/products/{product.id}"' in page.text
    assert "200 ₽" in page.text
    assert ">12</dd>" in page.text


# Карточка админки показывает категорию, бренд и бейдж наличия.
@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_products_card_shows_category_brand_and_stock_badge(client, db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    brand = Brand(name="Head", slug="head-ui")
    category = Category(name="Ракетки", slug="rackets-ui")
    db_session.add_all([brand, category])
    await db_session.flush()
    product = await create_product(
        db_session,
        ProductInput(
            name="Head Evo Card",
            brand_id=brand.id,
            category_id=category.id,
            sale_price=Decimal("18900"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, 24, "seed", admin.id)

    await _login(client, "inventory-ui-admin", "admin-pass")
    page = await client.get("/admin/products")

    assert page.status_code == 200
    assert "Head Evo Card" in page.text
    assert "Ракетки" in page.text
    assert "<code>Head</code>" in page.text
    assert "Много" in page.text
    assert "18 900 ₽" in page.text


# Карточка с фото рендерит img на /media/{storage_key}.
@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_products_card_renders_image(client, db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await _seed_product(db_session, admin, on_hand=3)
    db_session.add(
        ProductImage(
            product_id=product.id,
            storage_key="products/demo/head-evo-delta.png",
            sort_order=0,
        )
    )
    await db_session.commit()

    await _login(client, "inventory-ui-admin", "admin-pass")
    page = await client.get("/admin/products")

    assert page.status_code == 200
    assert 'src="/media/products/demo/head-evo-delta.png"' in page.text
    assert f'alt="{product.name}"' in page.text
    assert "product-visual" in page.text


# Неактивный товар в карточке помечается бейджем, без бейджа наличия.
@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_products_card_inactive_badge(client, db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await create_product(
        db_session,
        ProductInput(
            name="Inactive Card Product",
            sale_price=Decimal("100"),
            status=ProductStatus.INACTIVE,
        ),
        admin.id,
    )

    await _login(client, "inventory-ui-admin", "admin-pass")
    page = await client.get("/admin/products")

    assert page.status_code == 200
    assert product.name in page.text
    assert "Неактивен" in page.text
    assert "Много" not in page.text
    assert "Достаточно" not in page.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_products_reserve_link_when_reserved(client, db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await _seed_product(db_session, admin, on_hand=10)
    company, _company_name = await _seed_company(db_session, admin)
    await upsert_cart_item(db_session, company.company_id, product.id, 4)
    await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
        idempotency_key="inventory-ui-reserve-link",
    )

    await _login(client, "inventory-ui-admin", "admin-pass")
    page = await client.get("/admin/products")

    assert page.status_code == 200
    assert f'data-reservations-url="/admin/products/{product.id}/reservations"' in page.text
    assert "stock-reserve-link" in page.text
    assert ">4</a>" in page.text or f">{4}</a>" in page.text

@pytest.mark.db
@pytest.mark.asyncio
async def test_product_reservations_fragment_lists_invoice(client, db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await _seed_product(db_session, admin, on_hand=10)
    company, company_name = await _seed_company(db_session, admin)
    await upsert_cart_item(db_session, company.company_id, product.id, 4)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
        idempotency_key="inventory-ui-reserve",
    )

    await _login(client, "inventory-ui-admin", "admin-pass")
    response = await client.get(f"/admin/products/{product.id}/reservations")

    assert response.status_code == 200
    assert invoice.number in response.text
    assert company_name in response.text
    assert ">4<" in response.text or ">4</" in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_list_admin_product_stock_matches_availability(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await _seed_product(db_session, admin, on_hand=10)
    company, company_name = await _seed_company(db_session, admin)
    await upsert_cart_item(db_session, company.company_id, product.id, 4)
    await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
        idempotency_key="inventory-ui-stock",
    )

    stock = await list_admin_product_stock(db_session, [product.id])
    row = stock[product.id]
    assert row.on_hand == 10
    assert row.reserved == 4
    assert row.available == 6
    assert await get_availability(db_session, product.id) == 6


@pytest.mark.db
@pytest.mark.asyncio
async def test_list_active_reservations_for_product(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await _seed_product(db_session, admin, on_hand=10)
    company, company_name = await _seed_company(db_session, admin)
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
        idempotency_key="inventory-ui-list-res",
    )

    rows = await list_active_reservations_for_product(db_session, product.id)
    assert len(rows) == 1
    assert rows[0].invoice_number == invoice.number
    assert rows[0].company_name == company_name
    assert rows[0].quantity == 3



@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_dashboard_lists_inactive_product_with_low_stock(db_session):
    from b2b_commerce.admin.service import get_admin_dashboard

    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await create_product(
        db_session,
        ProductInput(
            name="Inactive Low Stock",
            sale_price=Decimal("200"),
            cost_price=Decimal("80"),
            status=ProductStatus.INACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, 2, "manual", admin.id)

    dashboard = await get_admin_dashboard(db_session)
    assert dashboard.low_stock_count >= 1
    assert any(row.product_id == product.id for row in dashboard.low_stock)
    assert any(row.available == 2 for row in dashboard.low_stock if row.product_id == product.id)


@pytest.mark.db
@pytest.mark.asyncio
async def test_product_inventory_correction_still_works(client, db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await _seed_product(db_session, admin, on_hand=5)

    await _login(client, "inventory-ui-admin", "admin-pass")
    response = await client.post(
        f"/admin/products/{product.id}/inventory/correct",
        data={"quantity": "8", "reason": "Инвентаризация"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "Остаток обновлён" in response.text
    assert await get_availability(db_session, product.id) == 8
    assert await reserved_quantity(db_session, product.id) == 0


# Карточка товара в админке: галерея слева, параметры справа.
@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_product_detail_uses_demo_layout(client, db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    brand = Brand(name="Dunlop", slug="dunlop-detail")
    category = Category(name="Ракетки", slug="rackets-detail")
    db_session.add_all([brand, category])
    await db_session.flush()
    product = await create_product(
        db_session,
        ProductInput(
            name="Ракетка Dunlop FX Hybrid",
            brand_id=brand.id,
            category_id=category.id,
            description="Профессиональная ракетка для клубных игроков",
            cost_price=Decimal("9800"),
            sale_price=Decimal("12500"),
            model_year=2026,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    db_session.add(
        ProductImage(product_id=product.id, storage_key="products/demo/cover.png", sort_order=0)
    )
    await db_session.commit()

    await _login(client, "inventory-ui-admin", "admin-pass")
    page = await client.get(f"/admin/products/{product.id}")

    assert page.status_code == 200
    html = page.text
    assert "product-detail-grid" in html
    assert "gallery-panel" in html
    assert 'src="/media/products/demo/cover.png"' in html
    assert "Параметры товара" in html
    assert "sale-field" in html
    assert 'id="product-params-form"' in html
    assert "Ракетка Dunlop FX Hybrid" in html
    assert "Профессиональная ракетка для клубных игроков" in html
    assert "Dunlop" in html
    assert "Ракетки" in html
    assert "12500" in html
    assert "9800" in html
    assert "inventory-stats" in html
    assert "История цен" in html
    assert "Активен — на сайте" in html
    assert "Можно несколько сразу" in html
    assert html.count("Профессиональная ракетка для клубных игроков") == 1
