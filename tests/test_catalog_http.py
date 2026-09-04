from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.catalog.models import Brand, Category
from b2b_commerce.catalog.service import ProductInput, create_product, soft_delete_product
from b2b_commerce.companies.service import CompanyInput, create_company
from b2b_commerce.enums import ProductStatus


def _app_js() -> str:
    js_path = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "static" / "app.js"
    return js_path.read_text(encoding="utf-8")

async def _seed_catalog_company(db_session):
    admin = AdminUser(
        login="catalog-http-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    created = await create_company(
        db_session,
        CompanyInput(name="Catalog HTTP Co"),
        admin.id,
    )
    category = Category(name="Ракетки QA", slug=f"rackets-{uuid4().hex[:8]}")
    db_session.add(category)
    await db_session.flush()
    for index in range(31):
        await create_product(
            db_session,
            ProductInput(
                name=f"Product {index:02d}",
                category_id=category.id,
                status=ProductStatus.ACTIVE,
            ),
            admin.id,
        )
    await db_session.commit()
    return created, category


async def _login_catalog_client(client, login: str, password: str):
    response = await client.post(
        "/login",
        data={"login": login, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    if response.headers["location"] == "/profile":
        change = await client.post(
            "/change-password",
            data={"new_password": "catalog-pass12"},
            follow_redirects=False,
        )
        assert change.status_code == 200


async def _seed_catalog_filters(db_session):
    admin = AdminUser(
        login=f"catalog-filter-admin-{uuid4().hex[:8]}",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    company = await create_company(
        db_session,
        CompanyInput(name="Catalog Filter Co"),
        admin.id,
    )
    category = Category(name="Ракетки Filter", slug=f"rackets-filter-{uuid4().hex[:8]}")
    wilson = Brand(name="Wilson HTTP", slug=f"wilson-http-{uuid4().hex[:8]}")
    head = Brand(name="Head HTTP", slug=f"head-http-{uuid4().hex[:8]}")
    db_session.add_all([category, wilson, head])
    await db_session.flush()
    for index, (brand, year, name) in enumerate(
        [
            (wilson, 2026, "Wilson Blade Alpha"),
            (wilson, 2025, "Wilson Classic"),
            (head, 2026, "Head Speed"),
        ]
    ):
        await create_product(
            db_session,
            ProductInput(
                name=name,
                brand_id=brand.id,
                category_id=category.id,
                model_year=year,
                status=ProductStatus.ACTIVE,
            ),
            admin.id,
        )
    for index in range(32):
        await create_product(
            db_session,
            ProductInput(
                name=f"Filler Product {index:02d}",
                brand_id=head.id,
                category_id=category.id,
                model_year=2024,
                status=ProductStatus.ACTIVE,
            ),
            admin.id,
        )
    await db_session.commit()
    return company, category, wilson, head


async def _seed_catalog_sort_pagination(db_session):
    admin = AdminUser(
        login=f"catalog-sort-admin-{uuid4().hex[:8]}",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    company = await create_company(
        db_session,
        CompanyInput(name="Catalog Sort Pag Co"),
        admin.id,
    )
    category = Category(name="Sort Pagination", slug=f"sort-pag-{uuid4().hex[:8]}")
    db_session.add(category)
    await db_session.flush()
    for index in range(35):
        await create_product(
            db_session,
            ProductInput(
                name=f"Sort Price {index:03d}",
                category_id=category.id,
                sale_price=Decimal(str((index + 1) * 100)),
                status=ProductStatus.ACTIVE,
            ),
            admin.id,
        )
    await db_session.commit()
    return company, category


LONG_PRODUCT_DESCRIPTION = "\n".join(
    f"Длинная строка описания {index}." for index in range(1, 15)
)


async def _seed_detail_products(db_session):
    admin = AdminUser(
        login=f"catalog-detail-admin-{uuid4().hex[:8]}",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    company = await create_company(
        db_session,
        CompanyInput(name="Catalog Detail Co"),
        admin.id,
    )
    long_product = await create_product(
        db_session,
        ProductInput(
            name="Detail Long Description",
            description=LONG_PRODUCT_DESCRIPTION,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    short_product = await create_product(
        db_session,
        ProductInput(
            name="Detail Short Description",
            description="Короткое описание.",
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    empty_product = await create_product(
        db_session,
        ProductInput(
            name="Detail Empty Description",
            description=None,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await db_session.commit()
    return company, long_product, short_product, empty_product


async def _fetch_product_detail(client, company, product_id):
    await _login_catalog_client(client, company.login, company.temporary_password)
    return await client.get(f"/catalog/products/{product_id}", follow_redirects=False)


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_search_preserves_category_id(db_session, client):
    company, category = await _seed_catalog_company(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)
    response = await client.get(
        "/catalog",
        params={
            "q": "Product",
            "category_id": str(category.id),
            "brand_id": "22222222-2222-2222-2222-222222222222",
            "model_year": "2024",
            "sort": "price_asc",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert f'name="category_id" value="{category.id}"' in response.text
    assert 'id="brand_id"' in response.text
    assert 'id="model_year"' in response.text
    assert '<option value="price_asc" selected>' in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_pagination_preserves_query_params(db_session, client):
    company, category, wilson, head = await _seed_catalog_filters(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)
    page1 = await client.get(
        "/catalog",
        params={
            "q": "Filler",
            "category_id": str(category.id),
            "brand_id": str(head.id),
            "model_year": "2024",
            "sort": "price_desc",
        },
        follow_redirects=False,
    )
    assert page1.status_code == 200
    assert "page=2" in page1.text
    page2 = await client.get(
        "/catalog",
        params={
            "q": "Filler",
            "category_id": str(category.id),
            "brand_id": str(head.id),
            "model_year": "2024",
            "sort": "price_desc",
            "page": 2,
        },
        follow_redirects=False,
    )
    assert page2.status_code == 200
    assert f"brand_id={head.id}" in page2.text
    assert "model_year=2024" in page2.text
    assert "sort=price_desc" in page2.text
    assert f"category_id={category.id}" in page2.text

@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_filters_brand_and_model_year(db_session, client):
    company, category, wilson, head = await _seed_catalog_filters(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)

    filtered = await client.get(
        "/catalog",
        params={
            "brand_id": str(wilson.id),
            "model_year": "2026",
        },
        follow_redirects=False,
    )
    assert filtered.status_code == 200
    assert "Wilson Blade Alpha" in filtered.text
    assert "Wilson Classic" not in filtered.text
    assert "Head Speed" not in filtered.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_filters_combined_with_category_and_search(db_session, client):
    company, category, wilson, head = await _seed_catalog_filters(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)

    response = await client.get(
        "/catalog",
        params={
            "q": "Blade",
            "category_id": str(category.id),
            "brand_id": str(wilson.id),
            "model_year": "2026",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Wilson Blade Alpha" in response.text
    assert "Head Speed" not in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_filters_empty_result(db_session, client):
    company, category, wilson, head = await _seed_catalog_filters(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)

    response = await client.get(
        "/catalog",
        params={
            "brand_id": str(head.id),
            "model_year": "2025",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Ничего не найдено" in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_filter_select_resets_page(db_session, client):
    company, category, wilson, head = await _seed_catalog_filters(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)

    page2 = await client.get(
        "/catalog",
        params={"brand_id": str(head.id), "model_year": "2024", "page": 2},
        follow_redirects=False,
    )
    assert page2.status_code == 200
    assert "Filler Product 31" in page2.text

    switched = await client.get(
        "/catalog",
        params={"brand_id": str(wilson.id), "model_year": "2026"},
        follow_redirects=False,
    )
    assert switched.status_code == 200
    assert "Wilson Blade Alpha" in switched.text
    assert "Filler Product 31" not in switched.text

@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_sort_select_and_query_params(db_session, client):
    company, category, wilson, head = await _seed_catalog_filters(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)

    response = await client.get(
        "/catalog",
        params={
            "q": "Filler",
            "category_id": str(category.id),
            "brand_id": str(head.id),
            "model_year": "2024",
            "sort": "price_asc",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'id="sort"' in response.text
    assert '<option value="price_asc" selected>' in response.text
    assert "sort=price_asc" in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_sort_pagination_preserves_params(db_session, client):
    company, category, wilson, head = await _seed_catalog_filters(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)

    page2 = await client.get(
        "/catalog",
        params={
            "q": "Filler",
            "category_id": str(category.id),
            "brand_id": str(head.id),
            "model_year": "2024",
            "sort": "price_desc",
            "page": 2,
        },
        follow_redirects=False,
    )
    assert page2.status_code == 200
    assert "sort=price_desc" in page2.text
    assert f"brand_id={head.id}" in page2.text
    assert "model_year=2024" in page2.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_sort_change_resets_page(db_session, client):
    company, category = await _seed_catalog_sort_pagination(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)

    page2_asc = await client.get(
        "/catalog",
        params={
            "category_id": str(category.id),
            "sort": "price_asc",
            "page": 2,
        },
        follow_redirects=False,
    )
    assert page2_asc.status_code == 200
    assert 'aria-current="page">2</span>' in page2_asc.text
    assert "Sort Price 030" in page2_asc.text
    assert "Sort Price 034" in page2_asc.text
    assert "Sort Price 029" not in page2_asc.text

    page1_desc = await client.get(
        "/catalog",
        params={
            "category_id": str(category.id),
            "sort": "price_desc",
        },
        follow_redirects=False,
    )
    assert page1_desc.status_code == 200
    assert 'aria-current="page">1</span>' in page1_desc.text
    assert '<option value="price_desc" selected>' in page1_desc.text
    assert "Sort Price 034" in page1_desc.text
    assert "Sort Price 005" in page1_desc.text
    assert "Sort Price 000" not in page1_desc.text
    assert "Sort Price 004" not in page1_desc.text
    assert "Sort Price 005" not in page2_asc.text
    assert "page=2" not in page1_desc.request.url.query.decode()


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_sort_empty_result(db_session, client):
    company, category, wilson, head = await _seed_catalog_filters(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)

    response = await client.get(
        "/catalog",
        params={
            "brand_id": str(head.id),
            "model_year": "2025",
            "sort": "stock_desc",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Ничего не найдено" in response.text
    assert '<option value="stock_desc" selected>' in response.text

@pytest.mark.db
@pytest.mark.asyncio
async def test_product_detail_long_description_toggle_markup(db_session, client):
    company, long_product, _, _ = await _seed_detail_products(db_session)
    response = await _fetch_product_detail(client, company, long_product.id)
    assert response.status_code == 200
    assert 'data-description-toggle' in response.text
    assert 'product-description-text description is-clamped' in response.text
    assert LONG_PRODUCT_DESCRIPTION in response.text
    assert 'class="btn btn-link product-description-toggle"' in response.text
    assert 'aria-controls="product-description-text"' in response.text
    assert 'aria-expanded="false"' in response.text
    assert 'Показать полностью' in response.text
    assert 'syncToggleVisibility' in _app_js()


@pytest.mark.db
@pytest.mark.asyncio
async def test_product_detail_short_description_without_toggle(db_session, client):
    company, _, short_product, _ = await _seed_detail_products(db_session)
    response = await _fetch_product_detail(client, company, short_product.id)
    assert response.status_code == 200
    assert "Короткое описание." in response.text
    assert 'data-description-toggle' in response.text
    assert 'class="btn btn-link product-description-toggle"' in response.text
    assert 'hidden' in response.text.split('product-description-toggle', 1)[1][:40]


@pytest.mark.db
@pytest.mark.asyncio
async def test_product_detail_missing_description_without_toggle(db_session, client):
    company, _, _, empty_product = await _seed_detail_products(db_session)
    response = await _fetch_product_detail(client, company, empty_product.id)
    assert response.status_code == 200
    assert "Описание не указано." in response.text
    assert '<div class="product-description" data-description-toggle' not in response.text
    assert 'class="btn btn-link product-description-toggle' not in response.text

async def _seed_detail_access_products(db_session):
    admin = AdminUser(
        login=f"catalog-access-admin-{uuid4().hex[:8]}",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    company = await create_company(
        db_session,
        CompanyInput(name="Catalog Access Co"),
        admin.id,
    )
    active = await create_product(
        db_session,
        ProductInput(name="Active Detail Product", status=ProductStatus.ACTIVE),
        admin.id,
    )
    inactive = await create_product(
        db_session,
        ProductInput(name="Inactive Detail Product", status=ProductStatus.INACTIVE),
        admin.id,
    )
    deleted = await create_product(
        db_session,
        ProductInput(name="Deleted Detail Product", status=ProductStatus.ACTIVE),
        admin.id,
    )
    await soft_delete_product(db_session, deleted.id, admin.id)
    await db_session.commit()
    return company, active, inactive, deleted


@pytest.mark.db
@pytest.mark.asyncio
async def test_catalog_page_beyond_last_clamps_content_without_redirect(db_session, client):
    company, category = await _seed_catalog_sort_pagination(db_session)
    await _login_catalog_client(client, company.login, company.temporary_password)

    response = await client.get(
        "/catalog",
        params={"category_id": str(category.id), "page": 99},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.headers.get("location") is None
    assert 'aria-current="page">2</span>' in response.text
    assert "Sort Price 034" in response.text
    assert "Sort Price 000" not in response.text
    assert response.request.url.params.get("page") == "99"


@pytest.mark.db
@pytest.mark.asyncio
async def test_product_detail_active_returns_200(db_session, client):
    company, active, _, _ = await _seed_detail_access_products(db_session)
    response = await _fetch_product_detail(client, company, active.id)
    assert response.status_code == 200
    assert "Active Detail Product" in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_product_detail_inactive_returns_404(db_session, client):
    company, _, inactive, _ = await _seed_detail_access_products(db_session)
    response = await _fetch_product_detail(client, company, inactive.id)
    assert response.status_code == 404


@pytest.mark.db
@pytest.mark.asyncio
async def test_product_detail_soft_deleted_returns_404(db_session, client):
    company, _, _, deleted = await _seed_detail_access_products(db_session)
    response = await _fetch_product_detail(client, company, deleted.id)
    assert response.status_code == 404

