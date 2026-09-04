from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.catalog.service import ProductInput, create_product
from b2b_commerce.companies.models import Company
from b2b_commerce.companies.service import COMPANIES_PAGE_SIZE
from b2b_commerce.enums import CompanyStatus, ProductStatus
from b2b_commerce.http import admin_companies_url, catalog_url

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "templates"
STATIC = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "static"


def _read(rel_path: str) -> str:
    return (TEMPLATES / rel_path).read_text(encoding="utf-8")


def _app_css() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


def test_list_page_url_macro_defined() -> None:
    html = _read("macros/ui.html")
    assert "macro list_page_url" in html
    assert "admin_companies_url" in html
    assert "admin_products_url" in html


def test_pagination_macro_markup() -> None:
    html = _read("macros/ui.html")
    assert "macro pagination" in html
    assert "pagination__nav" in html
    assert "pagination__pages" in html
    assert "pagination__page" in html
    assert "pagination__ellipsis" in html
    assert "data-lucide=\"chevron-left\"" in html
    assert "data-lucide=\"chevron-right\"" in html
    assert "aria-current=\"page\"" in html
    assert "rel=\"prev\"" in html
    assert "rel=\"next\"" in html


def test_catalog_list_uses_pagination_macro() -> None:
    html = _read("catalog/list.html")
    assert "pagination(page, total_pages" in html
    assert "prev_href=" not in html


def test_products_list_uses_products_href_kind() -> None:
    html = _read("products/list.html")
    assert "pagination(page, total_pages, filters.q" in html
    assert "'products'" in html
    assert "prev_href=" not in html


def test_companies_list_uses_companies_href_kind() -> None:
    html = _read("companies/list.html")
    assert "pagination(page, total_pages, q" in html
    assert "'companies'" in html
    assert "prev_href=" not in html


def test_pagination_styles_present() -> None:
    css = _app_css()
    assert "Phase 11f — Pagination" in css
    assert ".pagination__pages" in css
    assert ".pagination__nav.btn-icon" in css or "btn-icon" in css


def test_admin_companies_url_matches_list_contract() -> None:
    assert admin_companies_url() == "/admin/companies"
    assert admin_companies_url("pending", "club", 3) == (
        "/admin/companies?status=pending&q=club&page=3"
    )


def test_catalog_url_still_preserves_filters_for_pagination() -> None:
    url = catalog_url(
        "racket", "cat-1", brand_id="brand-1", model_year="2024", sort="price_desc", page=2
    )
    assert url.startswith("/catalog?")
    assert "q=racket" in url
    assert "category_id=cat-1" in url
    assert "brand_id=brand-1" in url
    assert "model_year=2024" in url
    assert "sort=price_desc" in url
    assert "page=2" in url



async def _seed_phase11_admin(db_session):
    admin = AdminUser(
        login="phase11-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


async def _login_admin(client: AsyncClient):
    return await client.post(
        "/login",
        data={"login": "phase11-admin", "password": "admin-pass"},
        follow_redirects=False,
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_products_page_beyond_last_clamps_without_redirect(client, db_session):
    admin = await _seed_phase11_admin(db_session)
    for index in range(COMPANIES_PAGE_SIZE + 1):
        await create_product(
            db_session,
            ProductInput(
                name=f"Clamp Product {index:03d}",
                sale_price=Decimal("100"),
                cost_price=Decimal("50"),
                status=ProductStatus.ACTIVE,
            ),
            admin.id,
        )
    await _login_admin(client)

    response = await client.get(
        "/admin/products",
        params={"page": 99},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.headers.get("location") is None
    assert 'aria-current="page">2</span>' in response.text
    assert "Clamp Product 000" in response.text
    assert "Clamp Product 030" not in response.text
    assert response.request.url.params.get("page") == "99"


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_companies_page_beyond_last_clamps_without_redirect(client, db_session):
    await _seed_phase11_admin(db_session)
    for index in range(COMPANIES_PAGE_SIZE + 1):
        db_session.add(
            Company(
                name=f"Clamp Co {index:03d}",
                inn=f"77010000{index:02d}",
                status=CompanyStatus.ACTIVE.value,
            )
        )
    await db_session.commit()
    await _login_admin(client)

    response = await client.get(
        "/admin/companies",
        params={"page": 99},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.headers.get("location") is None
    assert 'aria-current="page">2</span>' in response.text
    assert 'aria-current="page">2' in response.text
    assert response.text.count('data-label="Компания"') == 1
    assert response.request.url.params.get("page") == "99"
