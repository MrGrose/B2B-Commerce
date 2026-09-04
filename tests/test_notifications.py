import logging

import pytest
from sqlalchemy import select

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.companies.models import Company, CompanyAccount
from b2b_commerce.companies.service import CompanyInput, create_company
from b2b_commerce.enums import CompanyStatus, NotificationKind, SessionSubjectType
from b2b_commerce.invoices.models import Invoice
from b2b_commerce.invoices.service import expire_due_invoices
from b2b_commerce.support.service import add_message, count_company_support_alerts, create_ticket
from test_invoices import (
    _due_invoice,
    _login,
    _seed_company_product,
    confirm_payment,
    ship_invoice,
)
from test_support import _account_id


async def _seed_admin(db_session, login: str = "notify-admin"):
    admin = AdminUser(
        login=login,
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_ticket_logs_admin_notification(db_session, caplog):
    caplog.set_level(logging.INFO, logger="b2b_commerce.notifications.service")
    admin = await _seed_admin(db_session)
    second_admin = await _seed_admin(db_session, login="notify-admin-2")
    company = await create_company(db_session, CompanyInput(name="Notify Co"), admin.id)
    account_id = await _account_id(db_session, company.company_id)
    await create_ticket(
        db_session,
        company.company_id,
        account_id,
        "Нужна помощь",
        "Текст обращения",
    )
    records = [
        r
        for r in caplog.records
        if r.name == "b2b_commerce.notifications.service"
        and NotificationKind.SUPPORT_NEW_TICKET.value in r.message
    ]
    assert len(records) == 2
    assert str(admin.id) in records[0].message or str(admin.id) in records[1].message
    assert str(second_admin.id) in records[0].message or str(second_admin.id) in records[1].message


@pytest.mark.db
@pytest.mark.asyncio
async def test_expire_invoice_logs_company_notification(db_session, caplog):
    caplog.set_level(logging.INFO, logger="b2b_commerce.notifications.service")
    _, invoice_id = await _due_invoice(db_session)
    invoice = await db_session.get(Invoice, invoice_id)
    assert invoice is not None
    expired_count = await expire_due_invoices(db_session)
    await db_session.commit()
    assert expired_count == 1
    records = [
        r
        for r in caplog.records
        if r.name == "b2b_commerce.notifications.service"
        and NotificationKind.INVOICE_EXPIRED.value in r.message
    ]
    assert len(records) == 1
    assert invoice.number in records[0].message


@pytest.mark.db
@pytest.mark.asyncio
async def test_support_admin_reply_increases_customer_badge(db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Reply Co"), admin.id)
    account_id = await _account_id(db_session, company.company_id)
    ticket = await create_ticket(
        db_session,
        company.company_id,
        account_id,
        "Вопрос по доставке",
        "Когда отправите?",
    )
    assert await count_company_support_alerts(db_session, company.company_id) == 0
    await add_message(
        db_session,
        ticket.id,
        SessionSubjectType.ADMIN,
        admin.id,
        "Отправим завтра",
    )
    assert await count_company_support_alerts(db_session, company.company_id) == 1


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_layout_shows_support_open_count(client, db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Badge Co"), admin.id)
    account_id = await _account_id(db_session, company.company_id)
    await create_ticket(db_session, company.company_id, account_id, "Тема", "Текст")
    await _login(client, admin.login, "admin-pass")
    page = await client.get("/admin/support", follow_redirects=True)
    assert page.status_code == 200
    assert "data-notification-toggle" not in page.text
    assert 'href="/admin/support"' in page.text
    assert "nav-count" in page.text
    assert "alert-count" in page.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_layout_shows_support_alert_count(client, db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Client Badge"), admin.id)
    account_id = await _account_id(db_session, company.company_id)
    company_row = await db_session.get(Company, company.company_id)
    assert company_row is not None
    company_row.status = CompanyStatus.ACTIVE.value
    account = await db_session.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == company.company_id)
    )
    assert account is not None
    account.must_change_password = False
    await db_session.commit()
    ticket = await create_ticket(
        db_session, company.company_id, account_id, "Доставка", "Вопрос"
    )
    await add_message(
        db_session,
        ticket.id,
        SessionSubjectType.ADMIN,
        admin.id,
        "Ответ поддержки",
    )
    await _login(client, company.login, company.temporary_password)
    page = await client.get("/catalog", follow_redirects=True)
    assert page.status_code == 200
    assert "data-notification-toggle" not in page.text
    assert 'href="/support"' in page.text
    assert "alert-count" in page.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_confirm_payment_logs_company_notification(db_session, caplog):
    caplog.set_level(logging.INFO, logger="b2b_commerce.notifications.service")
    admin, company, product = await _seed_company_product(db_session)
    from b2b_commerce.cart.service import upsert_cart_item
    from b2b_commerce.config import Settings
    from b2b_commerce.invoices.service import create_invoice_from_cart

    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    await confirm_payment(db_session, invoice.id, admin.id)
    records = [
        r
        for r in caplog.records
        if r.name == "b2b_commerce.notifications.service"
        and NotificationKind.INVOICE_PAID.value in r.message
    ]
    assert len(records) == 1
    assert invoice.number in records[0].message


@pytest.mark.db
@pytest.mark.asyncio
async def test_ship_invoice_logs_company_notification(db_session, caplog):
    caplog.set_level(logging.INFO, logger="b2b_commerce.notifications.service")
    admin, company, product = await _seed_company_product(db_session)
    from b2b_commerce.cart.service import upsert_cart_item
    from b2b_commerce.config import Settings
    from b2b_commerce.invoices.service import create_invoice_from_cart

    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    await confirm_payment(db_session, invoice.id, admin.id)
    await ship_invoice(db_session, invoice.id, admin.id)
    records = [
        r
        for r in caplog.records
        if r.name == "b2b_commerce.notifications.service"
        and NotificationKind.INVOICE_SHIPPED.value in r.message
    ]
    assert len(records) == 1
    assert "отгружен" in records[0].message.lower()
