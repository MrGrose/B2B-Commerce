from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.catalog.service import ProductInput, create_product
from b2b_commerce.enums import ProductStatus

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "templates"


def _read(rel_path: str) -> str:
    return (TEMPLATES / rel_path).read_text(encoding="utf-8")


def test_filter_bar_macro_has_dialog_and_trigger() -> None:
    html = _read("macros/filter_bar.html")
    assert 'macro filter_bar(' in html
    assert 'data-filter-dialog-open' in html
    assert 'data-filter-dialog-close' in html
    assert '<dialog' in html
    assert 'sliders-horizontal' in html
    assert 'data-lucide="x"' in html


def test_catalog_list_uses_filter_bar() -> None:
    html = _read("catalog/list.html")
    assert 'from "macros/filter_bar.html" import filter_bar' in html
    assert "filter_bar(" in html
    assert "'catalog-filters'" in html
    assert "catalog-toolbar" not in html


def test_admin_products_list_uses_filter_bar() -> None:
    html = _read("products/list.html")
    assert 'from "macros/filter_bar.html" import filter_bar' in html
    assert "filter_bar(" in html
    assert "'admin-products-filters'" in html


def test_filter_bar_single_named_sort_control() -> None:
    html = _read("macros/filter_bar.html")
    assert html.count('name="sort"') == 2  # admin + catalog branches, one rendered per page
    assert html.count('name="brand_id"') == 1
    assert 'name="model_year"' in html
    assert 'name="category_id"' in html
    assert 'name="status"' in html
    assert 'data-filter-field-twin="category_id"' not in html
    assert 'data-filter-field-twin="status"' not in html


def test_admin_filter_bar_compact_toolbar_markup() -> None:
    html = _read("macros/filter_bar.html")
    assert "filter-bar__inline--desktop" not in html
    assert "filter-bar__apply--desktop" not in html
    assert "filter-bar__field--dialog-only" not in html
    assert 'data-filter-field-twin="category_id"' not in html
    assert 'data-filter-field-twin="status"' not in html
    assert html.count('type="submit">Применить</button>') == 1



async def _seed_admin(db_session):
    admin = AdminUser(
        login="filter-bar-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


async def _login(client: AsyncClient):
    return await client.post(
        "/login",
        data={"login": "filter-bar-admin", "password": "admin-pass"},
        follow_redirects=False,
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_products_filter_markup_and_url_preservation(client, db_session):
    admin = await _seed_admin(db_session)
    await create_product(
        db_session,
        ProductInput(
            name="Filter Bar Product",
            sale_price=Decimal("100"),
            cost_price=Decimal("50"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await _login(client)

    page = await client.get(
        "/admin/products",
        params={
            "q": "Filter",
            "sort": "price_asc",
            "status": "active",
        },
        follow_redirects=False,
    )
    assert page.status_code == 200
    assert 'class="filter-bar filter-bar--admin"' in page.text
    assert 'data-filter-dialog-open' in page.text
    assert 'id="admin-products-filters-dialog"' in page.text
    assert 'filter-bar__inline--desktop' not in page.text
    assert 'filter-bar__apply--desktop' not in page.text
    assert 'name="q"' in page.text
    assert 'id="sort"' in page.text
    dialog_start = page.text.index('id="admin-products-filters-dialog"')
    toolbar_end = page.text.index('id="admin-products-filters-dialog"')
    toolbar = page.text[:toolbar_end]
    assert 'id="sort"' in toolbar
    assert 'name="category_id"' not in toolbar
    assert 'name="status"' not in toolbar
    assert 'name="brand_id"' not in toolbar
    dialog = page.text[dialog_start:]
    assert 'name="brand_id"' in dialog
    assert 'name="category_id"' in dialog
    assert 'name="model_year"' in dialog
    assert 'name="status"' in dialog
    assert 'type="submit">Применить</button>' in dialog
    assert "Filter Bar Product" in page.text

    page2 = await client.get(
        "/admin/products",
        params={
            "q": "Filter",
            "sort": "price_asc",
            "status": "active",
            "page": 2,
        },
        follow_redirects=False,
    )
    assert page2.status_code == 200
    assert "sort=price_asc" in page2.text
    assert "status=active" in page2.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_products_filter_form_has_no_page_field(client, db_session):
    await _seed_admin(db_session)
    await _login(client)
    page = await client.get("/admin/products", params={"q": "x", "page": 3}, follow_redirects=False)
    assert page.status_code == 200
    assert 'name="page"' not in page.text
