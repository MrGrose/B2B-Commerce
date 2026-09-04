import re
from collections import defaultdict
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.catalog.models import ProductImage
from b2b_commerce.catalog.router import get_storage
from b2b_commerce.catalog.service import ProductInput, create_product
from b2b_commerce.companies.service import (
    BillingEntityInput,
    CompanyInput,
    CompanyProfileInput,
    create_billing_entity,
    create_company,
    list_companies,
    update_company_admin,
)
from b2b_commerce.enums import ProductStatus
from b2b_commerce.inventory.models import Warehouse
from b2b_commerce.inventory.service import correct_inventory
from b2b_commerce.main import app


# Создаёт админа для performance-тестов.
async def _seed_admin(db_session):
    admin = AdminUser(
        login="perf-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


# Одобряет компанию с billing entity.
async def _approve_company(db_session, admin_id, company_id, name, inn_suffix):
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name=f"ИП {inn_suffix}",
            legal_name=f"ИП {inn_suffix}",
            inn=f"770700{inn_suffix}",
        ),
        admin_id,
    )
    await update_company_admin(
        db_session,
        company_id,
        CompanyProfileInput(name=name),
        admin_id,
        billing_entity_id=entity.id,
    )


# Логинит клиента и сбрасывает временный пароль при необходимости.
async def _login(client: AsyncClient, login: str, password: str):
    response = await client.post(
        "/login",
        data={"login": login, "password": password},
        follow_redirects=False,
    )
    if response.status_code == 303 and response.headers.get("location") == "/profile":
        await client.post(
            "/change-password",
            data={"new_password": "perf-pass12"},
            follow_redirects=False,
        )
    return response



# Считает SQL-запросы по подстрокам таблиц во время callback.
def _count_inventory_queries(sync_engine, callback):
    counts: dict[str, int] = defaultdict(int)

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if "inventory" in lowered or "inventory_reservations" in lowered:
            counts["inventory"] += 1

    event.listen(sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        callback()
    finally:
        event.remove(sync_engine, "before_cursor_execute", before_cursor_execute)
    return dict(counts)


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_list_uses_batch_availability_not_n_plus_one(db_session, client, db_engine):
    admin = await _seed_admin(db_session)
    warehouse = Warehouse(code="MAIN", name="Основной", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()

    company = await create_company(db_session, CompanyInput(name="Perf Shop"), admin.id)
    await _approve_company(db_session, admin.id, company.company_id, "Perf Shop", "0001")
    await _login(client, company.login, company.temporary_password)

    for index in range(12):
        product = await create_product(
            db_session,
            ProductInput(
                name=f"Perf Product {index}",
                sale_price=1000 + index,
                status=ProductStatus.ACTIVE,
            ),
            admin.id,
        )
        await correct_inventory(
            db_session,
            product.id,
            5 + index,
            "seed",
            admin.id,
        )

    sync_engine = db_engine.sync_engine

    async def load_catalog():
        response = await client.get("/catalog")
        assert response.status_code == 200

    counts = _count_inventory_queries(sync_engine, lambda: None)
    assert counts.get("inventory", 0) == 0

    inventory_queries: list[int] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if "inventory" in lowered or "inventory_reservations" in lowered:
            inventory_queries.append(1)

    event.listen(sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        response = await client.get("/catalog")
        assert response.status_code == 200
    finally:
        event.remove(sync_engine, "before_cursor_execute", before_cursor_execute)

    assert len(inventory_queries) <= 4


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_list_renders_single_cover_image_with_lazy_loading(db_session, client):
    admin = await _seed_admin(db_session)
    warehouse = Warehouse(code="MAIN", name="Основной", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()

    company = await create_company(db_session, CompanyInput(name="Cover Shop"), admin.id)
    await _approve_company(db_session, admin.id, company.company_id, "Cover Shop", "0002")
    await _login(client, company.login, company.temporary_password)

    product = await create_product(
        db_session,
        ProductInput(name="Alpha Gallery", sale_price=1500, status=ProductStatus.ACTIVE),
        admin.id,
    )
    db_session.add_all(
        [
            ProductImage(
                product_id=product.id,
                storage_key=f"products/{product.id}/cover.png",
                sort_order=0,
            ),
            ProductImage(
                product_id=product.id,
                storage_key=f"products/{product.id}/alt.png",
                sort_order=1,
            ),
        ]
    )
    await db_session.commit()
    await correct_inventory(db_session, product.id, 3, "seed", admin.id)

    for index in range(6):
        extra = await create_product(
            db_session,
            ProductInput(
                name=f"Extra {index}",
                sale_price=900,
                status=ProductStatus.ACTIVE,
            ),
            admin.id,
        )
        await correct_inventory(db_session, extra.id, 1, "seed", admin.id)

    lazy_product = await create_product(
        db_session,
        ProductInput(name="Zulu Cover", sale_price=800, status=ProductStatus.ACTIVE),
        admin.id,
    )
    db_session.add(
        ProductImage(
            product_id=lazy_product.id,
            storage_key=f"products/{lazy_product.id}/lazy.png",
            sort_order=0,
        )
    )
    await db_session.commit()
    await correct_inventory(db_session, lazy_product.id, 1, "seed", admin.id)

    response = await client.get("/catalog")
    assert response.status_code == 200
    html = response.text

    gallery_card = re.search(
        rf'<a class="product-card-media" href="/catalog/products/{product.id}">.*?</a>',
        html,
        re.DOTALL,
    )
    assert gallery_card is not None
    card_html = gallery_card.group(0)
    assert card_html.count("/media/") == 1
    assert 'decoding="async"' in card_html
    assert 'loading="eager"' in card_html

    lazy_card = re.search(
        rf'<a class="product-card-media" href="/catalog/products/{lazy_product.id}">.*?</a>',
        html,
        re.DOTALL,
    )
    assert lazy_card is not None
    assert 'loading="lazy"' in lazy_card.group(0)
    assert "catalog-card-gallery__dots" not in html


@pytest.mark.db
@pytest.mark.asyncio
async def test_static_assets_send_public_cache_header(client):
    response = await client.get("/static/app.css")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "public, max-age=3600"


@pytest.mark.db
@pytest.mark.asyncio
async def test_media_cache_control_header(db_session, client):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Media Cache Co"), admin.id)
    await _approve_company(db_session, admin.id, company.company_id, "Media Cache Co", "0099")
    storage = AsyncMock()
    storage.get_object.return_value = (b"png-bytes", "image/png")
    app.dependency_overrides[get_storage] = lambda: storage

    await _login(client, company.login, company.temporary_password)
    response = await client.get("/media/products/demo/cover.png")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "private, max-age=604800"
    storage.get_object.assert_awaited_once_with("products/demo/cover.png")
    app.dependency_overrides.pop(get_storage, None)


@pytest.mark.db
@pytest.mark.asyncio
async def test_list_companies_avoids_account_and_billing_n_plus_one(db_session, db_engine):
    admin = await _seed_admin(db_session)
    for index in range(8):
        company = await create_company(
            db_session,
            CompanyInput(name=f"Batch Co {index}"),
            admin.id,
        )
        await _approve_company(
            db_session,
            admin.id,
            company.company_id,
            f"Batch Co {index}",
            f"{index:04d}",
        )

    account_queries: list[int] = []
    billing_queries: list[int] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if "company_accounts" in lowered:
            account_queries.append(1)
        if "billing_entities" in lowered:
            billing_queries.append(1)

    sync_engine = db_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        views, total = await list_companies(db_session, page=1)
    finally:
        event.remove(sync_engine, "before_cursor_execute", before_cursor_execute)

    assert total >= 8
    assert len(views) == 8
    assert len(account_queries) <= 2
    assert len(billing_queries) <= 2
