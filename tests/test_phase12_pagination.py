from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.companies.models import CompanyAccount
from b2b_commerce.companies.service import (
    BILLING_ENTITIES_PAGE_SIZE,
    BillingEntityInput,
    CompanyInput,
    create_billing_entity,
    create_company,
)
from b2b_commerce.enums import InvoiceStatus
from b2b_commerce.http import admin_invoices_url, customer_invoices_url
from b2b_commerce.invoices.models import Invoice
from b2b_commerce.invoices.service import INVOICES_PAGE_SIZE
from b2b_commerce.support.models import SupportTicket
from b2b_commerce.support.service import SUPPORT_PAGE_SIZE


async def _seed_admin(db_session):
    admin = AdminUser(
        login="phase12-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


async def _seed_active_company(db_session):
    admin = await _seed_admin(db_session)
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name="Seller",
            legal_name="ИП Seller",
            inn="7701234567",
            kpp="770101001",
            legal_address="Москва",
            bank_name="Банк",
            bik="044525225",
            bank_account="40702810100000000001",
            corr_account="30101810400000000225",
        ),
        admin.id,
    )
    company = await create_company(
        db_session,
        CompanyInput(
            name="Phase12 Co",
            legal_name="ООО Phase12",
            inn="7707654321",
            contact_email="buyer@example.com",
            login="phase12-buyer",
            billing_entity_id=entity.id,
        ),
        admin.id,
    )
    account = await db_session.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == company.company_id)
    )
    assert account is not None
    account.must_change_password = False
    await db_session.commit()
    return admin, company


async def _login_admin(client: AsyncClient):
    return await client.post(
        "/login",
        data={"login": "phase12-admin", "password": "admin-pass"},
        follow_redirects=False,
    )


async def _login_buyer(client: AsyncClient, login: str, password: str):
    return await client.post(
        "/login",
        data={"login": login, "password": password},
        follow_redirects=False,
    )


async def _seed_invoices(db_session, company_id, count: int, prefix: str = "INV-P12"):
    now = datetime.now(UTC)
    for index in range(count):
        db_session.add(
            Invoice(
                company_id=company_id,
                number=f"{prefix}-{index:03d}",
                status=InvoiceStatus.AWAITING_PAYMENT.value,
                subtotal=Decimal("100.00"),
                total=Decimal("100.00"),
                created_at=now,
                expires_at=now,
            )
        )
    await db_session.commit()


async def _seed_tickets(db_session, company_id, count: int, prefix: str = "Ticket"):
    now = datetime.now(UTC)
    for index in range(count):
        db_session.add(
            SupportTicket(
                company_id=company_id,
                subject=f"{prefix} {index:03d}",
                status="open",
                created_at=now,
                updated_at=now,
            )
        )
    await db_session.commit()


def test_invoice_url_helpers():
    assert customer_invoices_url() == "/invoices"
    assert customer_invoices_url(3) == "/invoices?page=3"
    assert admin_invoices_url("paid") == "/admin/invoices?status=paid"
    assert admin_invoices_url("paid", 2) == "/admin/invoices?status=paid&page=2"


@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_invoices_page_beyond_last_clamps(client, db_session):
    _, company = await _seed_active_company(db_session)
    await _seed_invoices(db_session, company.company_id, INVOICES_PAGE_SIZE + 1)
    await _login_buyer(client, company.login, company.temporary_password)

    response = await client.get("/invoices", params={"page": 99}, follow_redirects=False)
    assert response.status_code == 200
    assert 'aria-current="page">2</span>' in response.text
    assert "INV-P12-030" in response.text
    assert "INV-P12-000" not in response.text
    assert 'href="/invoices/' in response.text
    assert "Открыть" not in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_invoices_page_beyond_last_clamps(client, db_session):
    _, company = await _seed_active_company(db_session)
    await _seed_invoices(db_session, company.company_id, INVOICES_PAGE_SIZE + 1)
    await _login_admin(client)

    response = await client.get(
        "/admin/invoices",
        params={"page": 99},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'aria-current="page">2</span>' in response.text
    assert "INV-P12-030" in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_support_page_beyond_last_clamps(client, db_session):
    _, company = await _seed_active_company(db_session)
    await _seed_tickets(db_session, company.company_id, SUPPORT_PAGE_SIZE + 1)
    await _login_buyer(client, company.login, company.temporary_password)

    response = await client.get("/support", params={"page": 99}, follow_redirects=False)
    assert response.status_code == 200
    assert 'aria-current="page">2</span>' in response.text
    assert "Ticket 030" in response.text
    assert "Открыть" not in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_billing_entities_page_beyond_last_clamps(client, db_session):
    admin = await _seed_admin(db_session)
    for index in range(BILLING_ENTITIES_PAGE_SIZE + 1):
        await create_billing_entity(
            db_session,
            BillingEntityInput(
                name=f"Entity {index:03d}",
                legal_name=f"ИП {index:03d}",
                inn=f"770100{index:04d}"[:10],
                kpp="770101001",
                legal_address="Москва",
                bank_name="Банк",
                bik="044525225",
                bank_account="40702810100000000001",
                corr_account="30101810400000000225",
            ),
            admin.id,
        )
    await _login_admin(client)

    response = await client.get(
        "/admin/billing-entities",
        params={"page": 99},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'aria-current="page">2</span>' in response.text

def test_support_thread_macro_defined() -> None:
    from pathlib import Path

    ui_macros = (
        Path(__file__).resolve().parents[1] / "src/b2b_commerce/templates/macros/ui.html"
    )
    html = ui_macros.read_text(encoding="utf-8")
    assert "macro support_message_thread" in html
    assert "support-bubble--outgoing" in html


def test_site_footer_macro_defined() -> None:
    from pathlib import Path

    ui_macros = (
        Path(__file__).resolve().parents[1] / "src/b2b_commerce/templates/macros/ui.html"
    )
    html = ui_macros.read_text(encoding="utf-8")
    assert "macro site_footer" in html
    assert "/legal/privacy" in html
    assert "черновики" not in html


@pytest.mark.db
@pytest.mark.asyncio
async def test_legal_pages_render(client):
    index = await client.get("/legal")
    assert index.status_code == 200
    assert "Политика конфиденциальности" in index.text
    page = await client.get("/legal/privacy")
    assert page.status_code == 200
    assert "Черновик" in page.text
    missing = await client.get("/legal/unknown")
    assert missing.status_code == 404


@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_support_close_route(client, db_session):
    from sqlalchemy import select

    from b2b_commerce.companies.models import CompanyAccount
    from b2b_commerce.companies.service import (
        BillingEntityInput,
        CompanyInput,
        create_billing_entity,
        create_company,
    )
    from b2b_commerce.support.service import create_ticket

    admin = await _seed_admin(db_session)
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name="Seller",
            legal_name="ИП Seller",
            inn="7701234568",
            kpp="770101001",
            legal_address="Москва",
            bank_name="Банк",
            bik="044525225",
            bank_account="40702810100000000001",
            corr_account="30101810400000000225",
        ),
        admin.id,
    )
    company = await create_company(
        db_session,
        CompanyInput(
            name="Support Close Co",
            login="close-buyer",
            billing_entity_id=entity.id,
        ),
        admin.id,
    )
    account_id = await db_session.scalar(
        select(CompanyAccount.id).where(CompanyAccount.company_id == company.company_id)
    )
    account = await db_session.get(CompanyAccount, account_id)
    assert account is not None
    account.must_change_password = False
    await db_session.commit()
    ticket = await create_ticket(
        db_session,
        company.company_id,
        account_id,
        "Закрыть из UI",
        "Текст",
    )
    await _login_buyer(client, company.login, company.temporary_password)
    response = await client.post(
        f"/support/{ticket.id}/close",
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = await client.get(f"/support/{ticket.id}")
    assert detail.status_code == 200
    assert "support-bubble" in detail.text
    assert "Тикет закрыт" in detail.text

