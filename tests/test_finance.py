from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.cart.service import upsert_cart_item
from b2b_commerce.catalog.service import ProductInput, create_product, update_product
from b2b_commerce.companies.models import Company
from b2b_commerce.companies.service import (
    BillingEntityInput,
    CompanyInput,
    create_billing_entity,
    create_company,
)
from b2b_commerce.config import Settings
from b2b_commerce.enums import ProductStatus
from b2b_commerce.finance.service import get_finance_summary
from b2b_commerce.inventory.models import Warehouse
from b2b_commerce.inventory.service import correct_inventory
from b2b_commerce.invoices.models import Invoice
from b2b_commerce.invoices.service import (
    confirm_payment,
    create_invoice_from_cart,
    ship_invoice,
)


# Создаёт админа.
async def _seed_admin(db_session):
    admin = AdminUser(
        login="finance-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


# Компания, товар и остаток.
async def _seed_company_product(
    db_session,
    stock: int = 10,
    sale_price: Decimal = Decimal("200"),
    cost_price: Decimal = Decimal("80"),
):
    admin = await _seed_admin(db_session)
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(name="T", legal_name="ООО Т", inn="7711111111"),
        admin.id,
    )
    company = await create_company(
        db_session,
        CompanyInput(name="Finance Co", billing_entity_id=entity.id),
        admin.id,
    )
    warehouse = Warehouse(code="MAIN", name="Main", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()
    product = await create_product(
        db_session,
        ProductInput(
            name="Finance Product",
            sale_price=sale_price,
            cost_price=cost_price,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, stock, "seed", admin.id)
    return admin, company, product


@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_summary_empty(db_session):
    summary = await get_finance_summary(db_session)
    assert summary.period == "30d"
    assert summary.paid_count == 0
    assert summary.paid_total == Decimal("0")
    assert summary.unpaid_count == 0
    assert summary.unpaid_total == Decimal("0")
    assert summary.shipped_revenue == Decimal("0")
    assert summary.shipped_cost == Decimal("0")
    assert summary.warehouse_stock_value == Decimal("0")
    assert summary.by_company == []
    assert summary.by_product == []


@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_summary_unpaid_invoices(db_session):
    admin, company, product = await _seed_company_product(db_session)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
        idempotency_key="finance-unpaid",
    )

    summary = await get_finance_summary(db_session)
    assert summary.unpaid_count == 1
    assert summary.unpaid_total == Decimal("400")
    assert len(summary.unpaid_invoices) == 1
    assert summary.unpaid_invoices[0].company_name == "Finance Co"
    assert summary.unpaid_invoices[0].total == Decimal("400")


@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_margin_uses_invoice_snapshots(db_session):
    admin, company, product = await _seed_company_product(
        db_session,
        stock=10,
        sale_price=Decimal("200"),
        cost_price=Decimal("80"),
    )
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
        idempotency_key="finance-shipped",
    )
    await confirm_payment(db_session, invoice.id, admin.id)
    await ship_invoice(db_session, invoice.id, admin.id)

    await update_product(
        db_session,
        product.id,
        ProductInput(
            name=product.name,
            sale_price=Decimal("500"),
            cost_price=Decimal("300"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )

    summary = await get_finance_summary(db_session)
    assert summary.shipped_revenue == Decimal("600")
    assert summary.shipped_cost == Decimal("240")
    assert summary.shipped_margin == Decimal("360")
    assert summary.shipped_margin_percent == Decimal("60.00")


@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_warehouse_stock_value(db_session):
    admin, _company, product = await _seed_company_product(
        db_session,
        stock=7,
        cost_price=Decimal("50"),
    )

    summary = await get_finance_summary(db_session)
    assert summary.warehouse_stock_value == Decimal("350")

    await correct_inventory(db_session, product.id, 5, "adjust", admin.id)
    summary = await get_finance_summary(db_session)
    assert summary.warehouse_stock_value == Decimal("250")


@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_warehouse_excludes_null_cost(db_session):
    admin = await _seed_admin(db_session)
    warehouse = Warehouse(code="MAIN", name="Main", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()
    known = await create_product(
        db_session,
        ProductInput(
            name="Known cost",
            sale_price=Decimal("100"),
            cost_price=Decimal("50"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    unknown = await create_product(
        db_session,
        ProductInput(
            name="Unknown cost",
            sale_price=Decimal("100"),
            cost_price=None,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, known.id, 4, "seed", admin.id)
    await correct_inventory(db_session, unknown.id, 10, "seed", admin.id)

    summary = await get_finance_summary(db_session)
    assert summary.warehouse_stock_value == Decimal("200")


@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_paid_without_ship_is_not_shipped_revenue(db_session):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
        idempotency_key="finance-paid-only",
    )
    await confirm_payment(db_session, invoice.id, admin.id)
    summary = await get_finance_summary(db_session, "all")
    assert summary.paid_count == 1
    assert summary.paid_total == Decimal("400")
    assert summary.shipped_count == 0
    assert summary.shipped_revenue == Decimal("0")
    assert summary.unpaid_count == 0


@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_period_excludes_old_shipments(db_session):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
        idempotency_key="finance-old-ship",
    )
    await confirm_payment(db_session, invoice.id, admin.id)
    await ship_invoice(db_session, invoice.id, admin.id)
    row = await db_session.get(Invoice, invoice.id)
    assert row is not None
    old = datetime.now(UTC) - timedelta(days=45)
    row.paid_at = old
    row.shipped_at = old
    await db_session.commit()
    recent = await get_finance_summary(db_session, "30d")
    assert recent.paid_total == Decimal("0")
    assert recent.shipped_revenue == Decimal("0")
    everything = await get_finance_summary(db_session, "all")
    assert everything.paid_total == Decimal("200")
    assert everything.shipped_revenue == Decimal("200")


@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_breakdown_by_company_and_product(db_session):
    admin, company_a, product = await _seed_company_product(db_session, stock=20)
    entity_row = await db_session.get(Company, company_a.company_id)
    assert entity_row is not None
    company_b = await create_company(
        db_session,
        CompanyInput(
            name="Finance Co B",
            inn="7703333333",
            contact_email="b-finance@example.com",
            billing_entity_id=entity_row.billing_entity_id,
        ),
        admin.id,
    )
    second = await create_product(
        db_session,
        ProductInput(
            name="Second Product",
            sale_price=Decimal("50"),
            cost_price=Decimal("10"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, second.id, 20, "seed", admin.id)
    settings = Settings()
    await upsert_cart_item(db_session, company_a.company_id, product.id, 2)
    first = await create_invoice_from_cart(
        db_session, company_a.company_id, admin.id, settings, idempotency_key="fin-a"
    )
    await confirm_payment(db_session, first.id, admin.id)
    await ship_invoice(db_session, first.id, admin.id)
    await upsert_cart_item(db_session, company_b.company_id, second.id, 3)
    other = await create_invoice_from_cart(
        db_session, company_b.company_id, admin.id, settings, idempotency_key="fin-b"
    )
    await confirm_payment(db_session, other.id, admin.id)
    await ship_invoice(db_session, other.id, admin.id)
    summary = await get_finance_summary(db_session, "all")
    by_company = {row.name: row for row in summary.by_company}
    assert by_company["Finance Co"].revenue == Decimal("400")
    assert by_company["Finance Co B"].revenue == Decimal("150")
    by_product = {row.name: row for row in summary.by_product}
    assert by_product["Finance Product"].quantity == 2
    assert by_product["Finance Product"].revenue == Decimal("400")
    assert by_product["Second Product"].quantity == 3
    assert by_product["Second Product"].revenue == Decimal("150")
    assert by_product["Second Product"].margin == Decimal("120")
async def _login(client, login: str, password: str):
    return await client.post(
        "/login",
        data={"login": login, "password": password},
        follow_redirects=False,
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_dashboard_hides_unpaid_section(db_session, client):
    admin, company, product = await _seed_company_product(db_session)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
        idempotency_key="finance-http-unpaid",
    )
    await _login(client, "finance-admin", "admin-pass")
    page = await client.get("/admin/finance")
    assert page.status_code == 200
    assert "Неоплаченные счета" not in page.text
    assert f"/admin/invoices/{invoice.id}" not in page.text
    summary = await get_finance_summary(db_session)
    assert summary.unpaid_count == 1
    assert summary.unpaid_total == Decimal("400")


@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_dashboard_paid_metrics_unchanged(db_session, client):
    admin, company, product = await _seed_company_product(db_session)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
        idempotency_key="finance-http-paid",
    )
    await confirm_payment(db_session, invoice.id, admin.id)
    await ship_invoice(db_session, invoice.id, admin.id)
    await _login(client, "finance-admin", "admin-pass")
    page = await client.get("/admin/finance")
    assert page.status_code == 200
    assert "Оплачено" in page.text
    assert "Выручка" in page.text
    assert "Чистая прибыль" not in page.text
    summary = await get_finance_summary(db_session, "all")
    assert summary.paid_count == 1
    assert summary.shipped_count == 1
    assert summary.shipped_revenue == Decimal("200")

@pytest.mark.db
@pytest.mark.asyncio
async def test_finance_dashboard_money_format_russian(db_session, client):
    await _seed_company_product(
        db_session,
        stock=10,
        cost_price=Decimal("4600"),
    )
    await _login(client, "finance-admin", "admin-pass")
    page = await client.get("/admin/finance")
    assert page.status_code == 200
    assert "on_hand" not in page.text
    assert "46 000 ₽" in page.text
