from uuid import uuid4

import pytest

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.catalog.models import Category
from b2b_commerce.catalog.service import (
    CategoryRow,
    ProductInput,
    create_category,
    create_product,
    delete_category,
    list_category_rows,
    update_category,
)
from b2b_commerce.enums import ProductStatus


async def _seed_admin(db_session):
    admin = AdminUser(
        login=f"categories-admin-{uuid4().hex[:8]}",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin



@pytest.mark.db
@pytest.mark.asyncio
async def test_create_and_list_category_rows(db_session):
    admin = await _seed_admin(db_session)
    created = await create_category(db_session, "Новая категория QA", admin.id)
    assert created.name == "Новая категория QA"
    rows = await list_category_rows(db_session)
    match = next(row for row in rows if row.id == created.id)
    assert isinstance(match, CategoryRow)
    assert match.product_count == 0


@pytest.mark.db
@pytest.mark.asyncio
async def test_update_category_name_keeps_slug(db_session):
    admin = await _seed_admin(db_session)
    created = await create_category(db_session, "Ракетки QA", admin.id)
    slug = created.slug
    updated = await update_category(
        db_session,
        created.id,
        "Ракетки обновлённые",
        admin.id,
    )
    assert updated is not None
    assert updated.name == "Ракетки обновлённые"
    assert updated.slug == slug


@pytest.mark.db
@pytest.mark.asyncio
async def test_delete_empty_category(db_session):
    admin = await _seed_admin(db_session)
    created = await create_category(db_session, f"Удаляемая-{uuid4().hex[:6]}", admin.id)
    deleted = await delete_category(db_session, created.id, admin.id)
    assert deleted is True
    assert await db_session.get(Category, created.id) is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_delete_category_with_products_fails(db_session):
    admin = await _seed_admin(db_session)
    category = await create_category(db_session, f"С товарами-{uuid4().hex[:6]}", admin.id)
    await create_product(
        db_session,
        ProductInput(
            name="Товар в категории",
            category_id=category.id,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    with pytest.raises(ValueError, match="удаление невозможно"):
        await delete_category(db_session, category.id, admin.id)


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_categories_page_requires_login(client):
    response = await client.get("/admin/categories", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_categories_crud_http(db_session, client):
    admin = await _seed_admin(db_session)
    login = await client.post(
        "/login",
        data={"login": admin.login, "password": "admin-pass"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    page = await client.get("/admin/settings")
    assert page.status_code == 200
    assert "Категории" in page.text
    assert "Бренды" in page.text

    name = f"HTTP Cat {uuid4().hex[:6]}"
    create = await client.post(
        "/admin/categories",
        data={"name": name},
        follow_redirects=False,
    )
    assert create.status_code == 303
    assert create.headers["location"] == "/admin/settings?tab=catalog&open=categories"

    listed = await client.get("/admin/settings")
    assert name in listed.text

@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_brands_crud_http(db_session, client):
    admin = await _seed_admin(db_session)
    login = await client.post(
        "/login",
        data={"login": admin.login, "password": "admin-pass"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    name = f"HTTP Brand {uuid4().hex[:6]}"
    create = await client.post(
        "/admin/brands",
        data={"name": name},
        follow_redirects=False,
    )
    assert create.status_code == 303
    assert create.headers["location"] == "/admin/settings?tab=catalog&open=brands"

    listed = await client.get("/admin/settings?tab=catalog&open=brands")
    assert name in listed.text
