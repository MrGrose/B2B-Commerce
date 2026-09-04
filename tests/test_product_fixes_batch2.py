from datetime import UTC, datetime, timedelta

import pytest

from b2b_commerce.cart.service import upsert_cart_item
from b2b_commerce.config import Settings
from b2b_commerce.invoices.service import (
    confirm_payment,
    count_invoices_created_since,
    create_invoice_from_cart,
    ship_invoice,
)
from test_invoices import _login, _login_customer, _seed_company_product


@pytest.mark.db
@pytest.mark.asyncio
async def test_recent_invoice_badge_excludes_shipped(db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    since = datetime.now(UTC) - timedelta(hours=24)
    assert await count_invoices_created_since(db_session, since) == 1

    await confirm_payment(db_session, invoice.id, admin.id)
    assert await count_invoices_created_since(db_session, since) == 1

    await ship_invoice(db_session, invoice.id, admin.id)
    assert await count_invoices_created_since(db_session, since) == 0


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_invoice_shows_cart_notes(db_session, client):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
        notes="Отгрузить после 15 числа",
    )
    await _login(client, "invoice-admin", "admin-pass")

    page = await client.get(f"/admin/invoices/{invoice.id}")
    assert page.status_code == 200
    assert "Комментарий клиента" in page.text
    assert "Отгрузить после 15 числа" in page.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_invoice_preview_modal_present_on_list_and_detail(db_session, client):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    await _login_customer(client, company)

    list_page = await client.get("/invoices")
    assert list_page.status_code == 200
    assert 'id="invoice-pdf-preview-modal"' in list_page.text
    assert 'data-invoice-preview-url="/invoices/' in list_page.text

    detail_page = await client.get(f"/invoices/{invoice.id}")
    assert detail_page.status_code == 200
    assert 'id="invoice-pdf-preview-modal"' in detail_page.text
    assert f'data-invoice-preview-url="/invoices/{invoice.id}/preview.pdf"' in detail_page.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_invoice_preview_pdf_allows_same_origin_frame(db_session, client):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    await _login_customer(client, company)

    response = await client.get(f"/invoices/{invoice.id}/preview.pdf")
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("application/pdf")
    assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert response.headers.get("Content-Security-Policy") == "frame-ancestors 'self'"


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_invoice_list_24h_counters_match_filtered_list(db_session, client):
    admin, company, product = await _seed_company_product(db_session, stock=50)

    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    awaiting = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
        idempotency_key="inv-24h-await",
    )

    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    paid = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
        idempotency_key="inv-24h-paid",
    )
    await confirm_payment(db_session, paid.id, admin.id)

    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    shipped = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
        idempotency_key="inv-24h-shipped",
    )
    await confirm_payment(db_session, shipped.id, admin.id)
    await ship_invoice(db_session, shipped.id, admin.id)

    await _login(client, "invoice-admin", "admin-pass")

    page_24h = await client.get("/admin/invoices?created=24h")
    assert page_24h.status_code == 200
    assert "Все (3)" in page_24h.text
    assert "За 24 ч (3)" in page_24h.text
    assert "Ожидают оплаты (1)" in page_24h.text
    assert "Оплачены (1)" in page_24h.text
    assert "Отгружены (1)" in page_24h.text
    assert awaiting.number in page_24h.text
    assert paid.number in page_24h.text
    assert shipped.number in page_24h.text

    page_paid_24h = await client.get("/admin/invoices?status=paid&created=24h")
    assert page_paid_24h.status_code == 200
    assert paid.number in page_paid_24h.text
    assert f"><strong>{awaiting.number}</strong>" not in page_paid_24h.text
