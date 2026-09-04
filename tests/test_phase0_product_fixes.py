import pytest

from b2b_commerce.cart.service import upsert_cart_item
from b2b_commerce.companies.service import get_company_profile_metrics
from b2b_commerce.config import Settings
from b2b_commerce.invoices.service import (
    confirm_payment,
    create_invoice_from_cart,
    ship_invoice,
)
from test_invoices import _login, _login_customer, _seed_company_product


@pytest.mark.db
@pytest.mark.asyncio
async def test_profile_metrics_include_shipped_invoice_total(db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    await confirm_payment(db_session, invoice.id, admin.id)
    await ship_invoice(db_session, invoice.id, admin.id)

    metrics = await get_company_profile_metrics(db_session, company.company_id)
    assert metrics.invoice_count == 1
    assert metrics.paid_total == invoice.total


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_ship_button_hidden_after_ship(db_session, client):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    await confirm_payment(db_session, invoice.id, admin.id)
    await ship_invoice(db_session, invoice.id, admin.id)
    await _login(client, "invoice-admin", "admin-pass")

    page = await client.get(f"/admin/invoices/{invoice.id}")
    assert page.status_code == 200
    assert "Отгрузить" not in page.text
    assert 'action="/admin/invoices/' + str(invoice.id) + '/ship"' not in page.text
    assert 'class="panel invoice-actions"' not in page.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_invoice_list_preview_uses_icon_button(db_session, client):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    await create_invoice_from_cart(db_session, company.company_id, admin.id, Settings())
    await _login_customer(client, company)

    page = await client.get("/invoices")
    assert page.status_code == 200
    assert 'aria-label="Предпросмотр счёта"' in page.text
    assert 'data-lucide="search"' in page.text
    assert 'class="btn btn-sm btn-icon btn-ghost invoice-preview-btn' in page.text
