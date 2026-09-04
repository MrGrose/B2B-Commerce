from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.catalog.router import get_storage
from b2b_commerce.companies.models import CompanyAccount
from b2b_commerce.companies.service import (
    BillingEntityInput,
    CompanyInput,
    CompanyProfileInput,
    create_billing_entity,
    create_company,
    update_company_admin,
)
from b2b_commerce.enums import InvoiceStatus
from b2b_commerce.invoices.models import Invoice
from b2b_commerce.main import app
from b2b_commerce.support.service import create_ticket


async def _seed_admin(db_session):
    admin = AdminUser(
        login="authz-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


async def _approve_company(db_session, admin_id, company_id, name, inn_suffix):
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name=f"ИП {inn_suffix}",
            legal_name=f"ИП {inn_suffix}",
            inn=f"770900{inn_suffix}",
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


async def _seed_two_companies(db_session):
    admin = await _seed_admin(db_session)
    first = await create_company(db_session, CompanyInput(name="Authz Alpha"), admin.id)
    second = await create_company(db_session, CompanyInput(name="Authz Beta"), admin.id)
    await _approve_company(db_session, admin.id, first.company_id, "Authz Alpha", "0001")
    await _approve_company(db_session, admin.id, second.company_id, "Authz Beta", "0002")
    return admin, first, second


async def _account_id(db_session, company_id):
    return await db_session.scalar(
        select(CompanyAccount.id).where(CompanyAccount.company_id == company_id)
    )


async def _login(client: AsyncClient, login: str, password: str):
    response = await client.post(
        "/login",
        data={"login": login, "password": password},
        follow_redirects=False,
    )
    if response.status_code == 303 and response.headers.get("location") == "/profile":
        await client.post(
            "/change-password",
            data={"new_password": "authz-pass12"},
            follow_redirects=False,
        )
    return response



@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_cannot_access_foreign_invoice(db_session, client):
    admin, first, second = await _seed_two_companies(db_session)
    invoice = Invoice(
        company_id=second.company_id,
        number="AUTHZ-FOREIGN",
        status=InvoiceStatus.AWAITING_PAYMENT.value,
        subtotal=Decimal("1000.00"),
        total=Decimal("1000.00"),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=2),
    )
    db_session.add(invoice)
    await db_session.commit()
    await db_session.refresh(invoice)

    await _login(client, first.login, first.temporary_password)
    html = await client.get(f"/invoices/{invoice.id}", follow_redirects=False)
    assert html.status_code == 404
    api = await client.get(f"/api/invoices/{invoice.id}")
    assert api.status_code == 404
    pdf = await client.get(f"/invoices/{invoice.id}/download.pdf", follow_redirects=False)
    assert pdf.status_code == 404

    await _login(client, admin.login, "admin-pass")
    admin_html = await client.get(f"/admin/invoices/{invoice.id}")
    assert admin_html.status_code == 200
    assert "AUTHZ-FOREIGN" in admin_html.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_cannot_access_foreign_support_ticket(db_session, client):
    _, first, second = await _seed_two_companies(db_session)
    account_b = await _account_id(db_session, second.company_id)
    ticket = await create_ticket(
        db_session,
        second.company_id,
        account_b,
        "Секрет Beta",
        "Только для Beta",
    )
    await _login(client, first.login, first.temporary_password)
    html = await client.get(f"/support/{ticket.id}", follow_redirects=False)
    assert html.status_code == 404
    api = await client.get(f"/api/support/{ticket.id}")
    assert api.status_code == 404


@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_cannot_access_admin_routes(db_session, client):
    admin, first, second = await _seed_two_companies(db_session)
    invoice = Invoice(
        company_id=second.company_id,
        number="AUTHZ-ADMIN",
        status=InvoiceStatus.PAID.value,
        subtotal=Decimal("500.00"),
        total=Decimal("500.00"),
        created_at=datetime.now(UTC),
        paid_at=datetime.now(UTC),
    )
    db_session.add(invoice)
    await db_session.commit()
    await db_session.refresh(invoice)

    await _login(client, first.login, first.temporary_password)
    for path in (
        f"/admin/invoices/{invoice.id}",
        f"/admin/companies/{second.company_id}",
        "/admin/support",
    ):
        response = await client.get(path, follow_redirects=False)
        assert response.status_code in {303, 403}, path
    api = await client.get(f"/api/admin/companies/{second.company_id}")
    assert api.status_code == 403
    confirm = await client.post(f"/api/admin/invoices/{invoice.id}/confirm-payment")
    assert confirm.status_code == 403


@pytest.mark.db
@pytest.mark.asyncio
async def test_anonymous_private_routes_redirect_or_401(client):
    paths_html = ("/invoices", "/cart", "/support", "/admin/invoices")
    for path in paths_html:
        response = await client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers.get("location") == "/login"
    for path in ("/api/invoices", "/api/cart", "/api/support", "/api/auth/me"):
        response = await client.get(path)
        assert response.status_code == 401, path


@pytest.mark.db
@pytest.mark.asyncio
async def test_media_requires_session_and_allowlist(db_session, client, monkeypatch):
    _, first, _ = await _seed_two_companies(db_session)
    storage = AsyncMock()
    storage.get_object.return_value = (b"png-bytes", "image/png")
    app.dependency_overrides[get_storage] = lambda: storage

    anonymous = await client.get("/media/products/demo/cover.png", follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers.get("location") == "/login"
    assert storage.get_object.call_count == 0

    await _login(client, first.login, first.temporary_password)
    bad_key = await client.get("/media/invoices/secret.pdf", follow_redirects=False)
    assert bad_key.status_code == 404
    traversal = await client.get("/media/products/../secret.png", follow_redirects=False)
    assert traversal.status_code == 404

    ok = await client.get("/media/products/demo/cover.png")
    assert ok.status_code == 200
    assert ok.content == b"png-bytes"
    assert ok.headers.get("cache-control") == "private, max-age=604800"
    storage.get_object.assert_awaited_once_with("products/demo/cover.png")
    app.dependency_overrides.pop(get_storage, None)


@pytest.mark.db
@pytest.mark.asyncio
async def test_media_visible_across_companies_not_idor(db_session, client, monkeypatch):
    """Общий каталог: фото товара другой компании доступны авторизованному клиенту."""
    _, first, _ = await _seed_two_companies(db_session)
    storage = AsyncMock()
    storage.get_object.return_value = (b"shared", "image/jpeg")
    app.dependency_overrides[get_storage] = lambda: storage
    await _login(client, first.login, first.temporary_password)
    response = await client.get("/media/products/other-company/item.jpg")
    assert response.status_code == 200
    app.dependency_overrides.pop(get_storage, None)
