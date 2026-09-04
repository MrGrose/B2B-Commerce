from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.catalog.models import Brand, PriceHistory
from b2b_commerce.catalog.service import (
    STOREFRONT_PAGE_SIZE,
    ProductInput,
    compute_margin,
    count_products_admin,
    count_products_storefront,
    create_product,
    list_products_admin,
    list_products_storefront,
    list_storefront_brand_stats,
    list_storefront_model_year_stats,
    normalize_storefront_sort,
    soft_delete_product,
    update_product,
)
from b2b_commerce.companies.service import CompanyInput, create_company
from b2b_commerce.enums import InvoiceStatus, ProductStatus, ReservationStatus, StockMovementType
from b2b_commerce.inventory.models import Inventory, InventoryReservation, StockMovement, Warehouse
from b2b_commerce.inventory.service import correct_inventory, get_availability
from b2b_commerce.invoices.models import Invoice


class _FakeStorage:
    async def put_object_async(self, key: str, data: bytes, content_type: str) -> None:
        del key, data, content_type


# Создаёт админа для тестов.
async def _seed_admin(db_session):
    admin = AdminUser(
        login="catalog-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


# Создаёт склад MAIN.
async def _seed_warehouse(db_session):
    warehouse = Warehouse(code="MAIN", name="Основной", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()
    return warehouse


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.db
@pytest.mark.asyncio
async def test_create_product_records_price_history(db_session):
    admin = await _seed_admin(db_session)
    product = await create_product(
        db_session,
        ProductInput(
            name="Racket Pro",
            cost_price=Decimal("1000"),
            sale_price=Decimal("1500"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    history = (
        await db_session.scalars(select(PriceHistory).where(PriceHistory.product_id == product.id))
    ).all()
    assert len(history) == 1
    assert history[0].sale_price == Decimal("1500")
    assert product.margin_percent == Decimal("33.33")


@pytest.mark.db
@pytest.mark.asyncio
async def test_update_product_appends_price_history(db_session):
    admin = await _seed_admin(db_session)
    product = await create_product(
        db_session,
        ProductInput(
            name="Ball Pack",
            cost_price=Decimal("100"),
            sale_price=Decimal("200"),
        ),
        admin.id,
    )
    updated = await update_product(
        db_session,
        product.id,
        ProductInput(
            name="Ball Pack XL",
            cost_price=Decimal("100"),
            sale_price=Decimal("250"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    assert updated is not None
    history = (
        await db_session.scalars(select(PriceHistory).where(PriceHistory.product_id == product.id))
    ).all()
    assert len(history) == 2


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_lists_only_active(db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(name="Inactive item", status=ProductStatus.INACTIVE),
        admin.id,
    )
    active = await create_product(
        db_session,
        ProductInput(name="Active item", status=ProductStatus.ACTIVE),
        admin.id,
    )
    rows = await list_products_storefront(db_session)
    assert len(rows) == 1
    assert rows[0].id == active.id


@pytest.mark.db
@pytest.mark.asyncio
async def test_soft_delete_unused_product(db_session):
    admin = await _seed_admin(db_session)
    product = await create_product(
        db_session,
        ProductInput(name="To delete", status=ProductStatus.ACTIVE),
        admin.id,
    )
    deleted = await soft_delete_product(db_session, product.id, admin.id)
    assert deleted is not None
    assert deleted.deleted_at is not None
    assert deleted.status == ProductStatus.ACTIVE

    rows = await list_products_admin(db_session)
    assert all(row.id != product.id for row in rows)


@pytest.mark.db
@pytest.mark.asyncio
async def test_soft_delete_with_invoice_rejected(db_session):
    from b2b_commerce.cart.service import upsert_cart_item
    from b2b_commerce.companies.service import (
        BillingEntityInput,
        CompanyInput,
        create_billing_entity,
        create_company,
    )
    from b2b_commerce.config import Settings
    from b2b_commerce.invoices.service import create_invoice_from_cart

    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(name="T", legal_name="ООО Т", inn="7711111111"),
        admin.id,
    )
    company = await create_company(
        db_session,
        CompanyInput(name="Delete Co", billing_entity_id=entity.id),
        admin.id,
    )
    product = await create_product(
        db_session,
        ProductInput(
            name="Invoice bound",
            sale_price=Decimal("200"),
            cost_price=Decimal("80"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, 5, "seed", admin.id)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
    )

    with pytest.raises(ValueError, match="счетах"):
        await soft_delete_product(db_session, product.id, admin.id)


@pytest.mark.db
@pytest.mark.asyncio
async def test_inactive_product_visible_in_admin_filter(db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(name="Inactive", status=ProductStatus.INACTIVE),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Active",
            status=ProductStatus.ACTIVE,
            sale_price=Decimal("100"),
        ),
        admin.id,
    )
    active_only = await list_products_admin(db_session, status=ProductStatus.ACTIVE)
    assert len(active_only) == 1
    assert active_only[0].status == ProductStatus.ACTIVE
    inactive_only = await list_products_admin(db_session, status=ProductStatus.INACTIVE)
    assert len(inactive_only) == 1
    assert inactive_only[0].status == ProductStatus.INACTIVE


@pytest.mark.db
@pytest.mark.asyncio
async def test_inventory_correction_initial_movement(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await create_product(
        db_session,
        ProductInput(name="Stock item", status=ProductStatus.ACTIVE),
        admin.id,
    )
    inventory = await correct_inventory(db_session, product.id, 10, "Первичный ввод", admin.id)
    assert inventory.quantity_on_hand == 10
    movement = await db_session.scalar(
        select(StockMovement).where(StockMovement.product_id == product.id)
    )
    assert movement is not None
    assert movement.type == StockMovementType.INITIAL
    assert movement.delta == 10
    assert await get_availability(db_session, product.id) == 10


@pytest.mark.db
@pytest.mark.asyncio
async def test_inventory_correction_second_is_correction(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    product = await create_product(
        db_session,
        ProductInput(name="Adjust item", status=ProductStatus.ACTIVE),
        admin.id,
    )
    await correct_inventory(db_session, product.id, 5, "Старт", admin.id)
    await correct_inventory(db_session, product.id, 8, "Инвентаризация", admin.id)
    movements = (await db_session.scalars(select(StockMovement))).all()
    assert len(movements) == 2
    assert movements[1].type == StockMovementType.CORRECTION
    assert movements[1].delta == 3
    inventory = await db_session.scalar(select(Inventory).where(Inventory.product_id == product.id))
    assert inventory is not None
    assert inventory.quantity_on_hand == 8




def _jpeg_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (1, 1), color="red").save(buffer, format="JPEG")
    return buffer.getvalue()

@pytest.mark.db
@pytest.mark.asyncio
async def test_add_product_image_stores_key(db_session):
    from b2b_commerce.catalog.service import add_product_image

    admin = await _seed_admin(db_session)
    product = await create_product(
        db_session,
        ProductInput(name="Photo item", status=ProductStatus.ACTIVE),
        admin.id,
    )
    image = await add_product_image(
        db_session,
        product.id,
        _jpeg_bytes(),
        "image/jpeg",
        _FakeStorage(),
        admin.id,
    )
    assert image is not None
    assert image.storage_key.startswith(f"products/{product.id}/")


@pytest.mark.db
@pytest.mark.asyncio
async def test_list_products_admin_status_filter(db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(name="Inactive", status=ProductStatus.INACTIVE),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Active",
            status=ProductStatus.ACTIVE,
            sale_price=Decimal("100"),
        ),
        admin.id,
    )
    active_only = await list_products_admin(db_session, status=ProductStatus.ACTIVE)
    assert len(active_only) == 1
    assert active_only[0].status == ProductStatus.ACTIVE
    all_products = await list_products_admin(db_session)
    assert len(all_products) == 2


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_search_by_name(db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(name="Viper Racket", status=ProductStatus.ACTIVE),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(name="Ball Pack", status=ProductStatus.ACTIVE),
        admin.id,
    )
    by_name = await list_products_storefront(db_session, search="viper")
    assert len(by_name) == 1
    assert by_name[0].name == "Viper Racket"
    by_ball = await list_products_storefront(db_session, search="ball")
    assert len(by_ball) == 1
    assert by_ball[0].name == "Ball Pack"


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_category_filter_and_stats(db_session):
    from b2b_commerce.catalog.models import Category
    from b2b_commerce.catalog.service import list_storefront_category_stats

    admin = await _seed_admin(db_session)
    rackets = Category(name="Ракетки", slug="rackets-test")
    balls = Category(name="Мячи", slug="balls-test")
    db_session.add_all([rackets, balls])
    await db_session.flush()
    await create_product(
        db_session,
        ProductInput(
            name="Racket One",
            category_id=rackets.id,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Ball One",
            category_id=balls.id,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    rackets_only = await list_products_storefront(db_session, category_id=rackets.id)
    assert len(rackets_only) == 1
    assert rackets_only[0].name == "Racket One"
    assert await count_products_storefront(db_session, category_id=balls.id) == 1
    stats = await list_storefront_category_stats(db_session)
    assert len(stats) == 2
    assert {item.name for item in stats} == {"Ракетки", "Мячи"}


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_brand_and_model_year_filters(db_session):
    from b2b_commerce.catalog.models import Category

    admin = await _seed_admin(db_session)
    wilson = Brand(name="Wilson QA", slug="wilson-qa")
    head = Brand(name="Head QA", slug="head-qa")
    rackets = Category(name="Ракетки FY", slug="rackets-fy")
    db_session.add_all([wilson, head, rackets])
    await db_session.flush()
    await create_product(
        db_session,
        ProductInput(
            name="Wilson Blade 2026",
            brand_id=wilson.id,
            category_id=rackets.id,
            model_year=2026,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Wilson Pro 2025",
            brand_id=wilson.id,
            category_id=rackets.id,
            model_year=2025,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Head Alpha 2026",
            brand_id=head.id,
            category_id=rackets.id,
            model_year=2026,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Draft Wilson",
            brand_id=wilson.id,
            category_id=rackets.id,
            model_year=2024,
            status=ProductStatus.INACTIVE,
        ),
        admin.id,
    )

    by_brand = await list_products_storefront(db_session, brand_id=wilson.id)
    assert [item.name for item in by_brand] == ["Wilson Blade 2026", "Wilson Pro 2025"]

    by_year = await list_products_storefront(db_session, model_year=2026)
    assert {item.name for item in by_year} == {"Wilson Blade 2026", "Head Alpha 2026"}

    combined = await list_products_storefront(
        db_session,
        brand_id=wilson.id,
        category_id=rackets.id,
        model_year=2026,
        search="blade",
    )
    assert len(combined) == 1
    assert combined[0].name == "Wilson Blade 2026"

    assert await count_products_storefront(
        db_session,
        brand_id=head.id,
        model_year=2099,
    ) == 0

    brand_stats = await list_storefront_brand_stats(db_session)
    assert [(item.name, item.count) for item in brand_stats] == [("Head QA", 1), ("Wilson QA", 2)]

    year_stats = await list_storefront_model_year_stats(db_session)
    assert [(item.year, item.count) for item in year_stats] == [(2026, 2), (2025, 1)]


def test_normalize_storefront_sort() -> None:
    assert normalize_storefront_sort("price_asc") == "price_asc"
    assert normalize_storefront_sort("unknown") is None
    assert normalize_storefront_sort("") is None
    assert normalize_storefront_sort(None) is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_sort_price_date_and_default(db_session):
    from b2b_commerce.catalog.models import Category

    admin = await _seed_admin(db_session)
    category = Category(name="Sort QA", slug="sort-qa")
    db_session.add(category)
    await db_session.flush()
    await create_product(
        db_session,
        ProductInput(
            name="Zulu Racket",
            category_id=category.id,
            sale_price=Decimal("900"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Alpha Racket",
            category_id=category.id,
            sale_price=Decimal("100"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )

    default_rows = await list_products_storefront(db_session, category_id=category.id)
    assert [item.name for item in default_rows] == ["Alpha Racket", "Zulu Racket"]

    price_desc = await list_products_storefront(
        db_session,
        category_id=category.id,
        sort="price_desc",
    )
    assert [item.name for item in price_desc] == ["Zulu Racket", "Alpha Racket"]

    price_asc = await list_products_storefront(
        db_session,
        category_id=category.id,
        search="racket",
        sort="price_asc",
    )
    assert [item.name for item in price_asc] == ["Alpha Racket", "Zulu Racket"]

    date_desc = await list_products_storefront(
        db_session,
        category_id=category.id,
        sort="date_desc",
    )
    assert [item.name for item in date_desc] == ["Alpha Racket", "Zulu Racket"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_sort_stock_by_available(db_session):
    admin = await _seed_admin(db_session)
    warehouse = await _seed_warehouse(db_session)
    company = await create_company(
        db_session,
        CompanyInput(name="Stock Sort Co"),
        admin.id,
    )
    product_a = await create_product(
        db_session,
        ProductInput(
            name="Product A",
            sale_price=Decimal("100"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    product_b = await create_product(
        db_session,
        ProductInput(
            name="Product B",
            sale_price=Decimal("100"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product_a.id, 10, "seed", admin.id)
    await correct_inventory(db_session, product_b.id, 5, "seed", admin.id)
    active_invoice = Invoice(
        company_id=company.company_id,
        number=f"active-{uuid4().hex[:8]}",
        status=InvoiceStatus.AWAITING_PAYMENT.value,
        subtotal=Decimal("800"),
        total=Decimal("800"),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    released_invoice = Invoice(
        company_id=company.company_id,
        number=f"released-{uuid4().hex[:8]}",
        status=InvoiceStatus.CANCELED.value,
        subtotal=Decimal("0"),
        total=Decimal("0"),
    )
    db_session.add_all([active_invoice, released_invoice])
    await db_session.flush()
    db_session.add_all(
        [
            InventoryReservation(
                invoice_id=active_invoice.id,
                product_id=product_a.id,
                warehouse_id=warehouse.id,
                quantity=8,
                status=ReservationStatus.ACTIVE.value,
            ),
            InventoryReservation(
                invoice_id=released_invoice.id,
                product_id=product_a.id,
                warehouse_id=warehouse.id,
                quantity=100,
                status=ReservationStatus.RELEASED.value,
            ),
        ]
    )
    await db_session.commit()
    assert await get_availability(db_session, product_a.id) == 2

    target_names = {"Product A", "Product B"}
    by_stock_asc = await list_products_storefront(db_session, sort="stock_asc")
    names_asc = [item.name for item in by_stock_asc if item.name in target_names]
    assert names_asc == ["Product A", "Product B"]

    by_stock_desc = await list_products_storefront(db_session, sort="stock_desc")
    names_desc = [item.name for item in by_stock_desc if item.name in target_names]
    assert names_desc == ["Product B", "Product A"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_sort_with_filters_and_empty(db_session):
    from b2b_commerce.catalog.models import Category

    admin = await _seed_admin(db_session)
    brand = Brand(name="Sort Brand", slug="sort-brand")
    category = Category(name="Sort Cat", slug="sort-cat")
    db_session.add_all([brand, category])
    await db_session.flush()
    await create_product(
        db_session,
        ProductInput(
            name="Match A",
            brand_id=brand.id,
            category_id=category.id,
            model_year=2026,
            sale_price=Decimal("500"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Match B",
            brand_id=brand.id,
            category_id=category.id,
            model_year=2025,
            sale_price=Decimal("100"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )

    rows = await list_products_storefront(
        db_session,
        category_id=category.id,
        brand_id=brand.id,
        model_year=2026,
        search="match",
        sort="price_desc",
    )
    assert len(rows) == 1
    assert rows[0].name == "Match A"

    empty = await list_products_storefront(
        db_session,
        category_id=category.id,
        brand_id=brand.id,
        model_year=2024,
        sort="price_asc",
    )
    assert empty == []


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_fulltext_search_rank(db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(
            name="Wilson Blade Padel",
            description="Professional carbon racket",
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Random accessory",
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    hits = await list_products_storefront(db_session, search="wilson blade")
    assert len(hits) == 1
    assert hits[0].name == "Wilson Blade Padel"




@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_search_russian_only(db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(name="Ракетка Pro V2", status=ProductStatus.ACTIVE),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(name="Other item", status=ProductStatus.ACTIVE),
        admin.id,
    )
    hits = await list_products_storefront(db_session, search="ракетка")
    assert len(hits) == 1
    assert hits[0].name == "Ракетка Pro V2"


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_search_description_only(db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(
            name="Generic Item",
            description="Уникальное описание titan carbon",
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Another Item",
            description="обычный текст",
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    hits = await list_products_storefront(db_session, search="titan")
    assert len(hits) == 1
    assert hits[0].name == "Generic Item"


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_search_model_year_via_q(db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(
            name="Year Product",
            model_year=2026,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Old Product",
            model_year=2024,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    hits = await list_products_storefront(db_session, search="2026")
    assert {item.name for item in hits} == {"Year Product"}


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_explicit_sort_overrides_fts_rank(db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(
            name="Wilson Blade Elite",
            sale_price=Decimal("900"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Budget Wilson",
            sale_price=Decimal("100"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(name="Unrelated", sale_price=Decimal("50"), status=ProductStatus.ACTIVE),
        admin.id,
    )

    by_rank = await list_products_storefront(db_session, search="wilson")
    assert [item.name for item in by_rank] == ["Budget Wilson", "Wilson Blade Elite"]

    by_price_desc = await list_products_storefront(db_session, search="wilson", sort="price_desc")
    assert [item.name for item in by_price_desc] == ["Wilson Blade Elite", "Budget Wilson"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_storefront_hides_soft_deleted_product(db_session):
    admin = await _seed_admin(db_session)
    brand = Brand(name="Delete Brand", slug=f"delete-brand-{uuid4().hex[:8]}")
    db_session.add(brand)
    await db_session.flush()
    active = await create_product(
        db_session,
        ProductInput(
            name="Active Store Item",
            brand_id=brand.id,
            model_year=2026,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    deleted = await create_product(
        db_session,
        ProductInput(
            name="ZZZUNIQUE_DELETED_ONLY",
            brand_id=brand.id,
            model_year=2026,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await soft_delete_product(db_session, deleted.id, admin.id)

    rows = await list_products_storefront(db_session)
    assert len(rows) == 1
    assert rows[0].id == active.id
    assert await list_products_storefront(db_session, search="ZZZUNIQUE_DELETED_ONLY") == []
    assert await count_products_storefront(db_session, search="ZZZUNIQUE_DELETED_ONLY") == 0

    brand_stats = await list_storefront_brand_stats(db_session)
    assert [(item.name, item.count) for item in brand_stats] == [("Delete Brand", 1)]

    year_stats = await list_storefront_model_year_stats(db_session)
    assert [(item.year, item.count) for item in year_stats] == [(2026, 1)]

@pytest.mark.db
@pytest.mark.asyncio
async def test_null_cost_price_allowed(db_session):
    admin = await _seed_admin(db_session)
    product = await create_product(
        db_session,
        ProductInput(
            name="Unknown cost",
            sale_price=Decimal("500"),
            cost_price=None,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    assert product.cost_price is None
    assert product.margin_percent is None


@pytest.mark.asyncio
async def test_compute_margin_null_cost():
    assert compute_margin(None, Decimal("100")) is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_search_by_brand_name(db_session):
    admin = await _seed_admin(db_session)
    brand = Brand(name="Bullpadel", slug="bullpadel")
    db_session.add(brand)
    await db_session.flush()
    await create_product(
        db_session,
        ProductInput(
            name="Elite Pro",
            brand_id=brand.id,
            sale_price=Decimal("1000"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Other Racket",
            sale_price=Decimal("900"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )

    rows = await list_products_admin(db_session, q="bullpadel")
    assert len(rows) == 1
    assert rows[0].name == "Elite Pro"


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_filter_by_model_year(db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(
            name="Model 2026",
            model_year=2026,
            sale_price=Decimal("100"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await create_product(
        db_session,
        ProductInput(
            name="Model 2025",
            model_year=2025,
            sale_price=Decimal("100"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )

    rows = await list_products_admin(db_session, model_year=2026)
    assert len(rows) == 1
    assert rows[0].name == "Model 2026"


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_sort_by_price_and_stock(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    cheap = await create_product(
        db_session,
        ProductInput(
            name="Cheap",
            sale_price=Decimal("100"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    pricey = await create_product(
        db_session,
        ProductInput(
            name="Pricey",
            sale_price=Decimal("500"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, cheap.id, 2, "seed", admin.id)
    await correct_inventory(db_session, pricey.id, 10, "seed", admin.id)

    by_price = await list_products_admin(db_session, sort="price_desc")
    assert [item.name for item in by_price[:2]] == ["Pricey", "Cheap"]

    by_stock = await list_products_admin(db_session, sort="stock_desc")
    assert [item.name for item in by_stock[:2]] == ["Pricey", "Cheap"]


@pytest.mark.asyncio
async def test_list_products_admin_pagination(db_session):
    admin = AdminUser(login="admin-pag", password_hash=hash_password("secret"))
    db_session.add(admin)
    await db_session.flush()

    for index in range(STOREFRONT_PAGE_SIZE + 5):
        await create_product(
            db_session,
            ProductInput(name=f"Paginated {index}", sale_price=Decimal("100")),
            admin.id,
        )

    total = await count_products_admin(db_session)
    assert total == STOREFRONT_PAGE_SIZE + 5

    first_page = await list_products_admin(
        db_session,
        offset=0,
        limit=STOREFRONT_PAGE_SIZE,
        sort="date_desc",
    )
    second_page = await list_products_admin(
        db_session,
        offset=STOREFRONT_PAGE_SIZE,
        limit=STOREFRONT_PAGE_SIZE,
        sort="date_desc",
    )
    assert len(first_page) == STOREFRONT_PAGE_SIZE
    assert len(second_page) == 5
    assert {item.id for item in first_page}.isdisjoint({item.id for item in second_page})
