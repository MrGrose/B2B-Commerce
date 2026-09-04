from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from b2b_commerce.audit.models import AuditLog
from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import authenticate, hash_password
from b2b_commerce.cart.models import Cart
from b2b_commerce.cart.service import upsert_cart_item
from b2b_commerce.catalog.service import ProductInput, create_product
from b2b_commerce.companies.models import BillingEntity, Company, CompanyAccount
from b2b_commerce.companies.service import (
    BillingEntityInput,
    CompanyInput,
    CompanyProfileInput,
    RegistrationInput,
    approve_company,
    create_billing_entity,
    create_company,
    deactivate_company,
    get_company,
    register_company,
    reject_company,
    update_company_admin,
)
from b2b_commerce.config import Settings
from b2b_commerce.enums import CompanyStatus, ProductStatus
from b2b_commerce.inventory.models import Warehouse
from b2b_commerce.inventory.service import correct_inventory
from b2b_commerce.invoices.service import create_invoice_from_cart

SETTINGS = Settings()


# Назначает юрлицо поставщика pending-компании для approve.
async def _assign_billing_entity(db_session, admin_id, company_id):
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name="ИП Тест",
            legal_name="ИП Тестовый",
            inn="7701234560",
            bank_name="Тест Банк",
            bik="044525225",
            bank_account="40702810100000000001",
            corr_account="30101810400000000225",
        ),
        admin_id,
    )
    company = await get_company(db_session, company_id)
    assert company is not None
    return await update_company_admin(
        db_session,
        company_id,
        CompanyProfileInput(name=company.name),
        admin_id,
        billing_entity_id=entity.id,
    )


# Собирает валидные данные самостоятельной регистрации.
def _reg(**overrides) -> RegistrationInput:
    data = dict(
        login="buyer-one",
        password="password1",
        name="Buyer One",
        legal_name='ООО "Байер"',
        inn="7701234567",
        contact_email="buyer@example.com",
        contact_phone="+79991234567",
    )
    data.update(overrides)
    return RegistrationInput(**data)


# Создаёт админа для тестов.
async def _seed_admin(db_session):
    admin = AdminUser(
        login="reg-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


# HTTP-клиент на той же test-БД и том же engine, что и layout middleware.

# Логинит через HTML и ставит cookie.
async def _login(client: AsyncClient, login: str, password: str):
    return await client.post(
        "/login",
        data={"login": login, "password": password},
        follow_redirects=False,
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_register_creates_pending_company_account_and_cart(db_session):
    company = await register_company(db_session, _reg())
    assert company is not None
    assert company.status == CompanyStatus.PENDING.value
    assert company.account is not None
    assert company.account.must_change_password is False
    assert company.account.is_active is True
    row = await db_session.get(CompanyAccount, company.account.id)
    assert row is not None
    assert row.password_hash != "password1"
    assert "password1" not in row.password_hash
    cart = await db_session.scalar(select(Cart).where(Cart.company_id == company.id))
    assert cart is not None
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "company.register")
    )
    assert audit is not None
    assert audit.entity_id == company.id


@pytest.mark.db
@pytest.mark.asyncio
async def test_register_allows_login(db_session):
    await register_company(db_session, _reg())
    result = await authenticate(db_session, SETTINGS, "buyer-one", "password1", "t1")
    assert result is not None
    _token, hit = result
    assert hit.company_status == CompanyStatus.PENDING.value


@pytest.mark.db
@pytest.mark.asyncio
async def test_register_rejects_duplicate_login(db_session):
    await register_company(db_session, _reg())
    with pytest.raises(ValueError, match="логин"):
        await register_company(
            db_session,
            _reg(login="buyer-one", inn="7701234568", contact_email="other@example.com"),
        )


@pytest.mark.db
@pytest.mark.asyncio
async def test_register_rejects_duplicate_email(db_session):
    await register_company(db_session, _reg())
    with pytest.raises(ValueError, match="email"):
        await register_company(
            db_session,
            _reg(login="buyer-two", inn="7701234568", contact_email="buyer@example.com"),
        )


@pytest.mark.db
@pytest.mark.asyncio
async def test_register_rejects_duplicate_inn(db_session):
    await register_company(db_session, _reg())
    with pytest.raises(ValueError, match="ИНН"):
        await register_company(
            db_session,
            _reg(login="buyer-two", inn="7701234567", contact_email="other@example.com"),
        )


@pytest.mark.db
@pytest.mark.asyncio
async def test_register_rejects_invalid_data(db_session):
    with pytest.raises(ValueError, match="Пароль"):
        await register_company(db_session, _reg(password="short"))
    with pytest.raises(ValueError, match="ИНН"):
        await register_company(db_session, _reg(inn="123"))
    with pytest.raises(ValueError, match="email"):
        await register_company(db_session, _reg(contact_email="not-an-email"))


@pytest.mark.db
@pytest.mark.asyncio
async def test_register_rollback_on_failure(db_session, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("audit fail")

    monkeypatch.setattr("b2b_commerce.companies.service.write_audit", boom)
    with pytest.raises(RuntimeError, match="audit fail"):
        await register_company(db_session, _reg(inn="7709999999", login="rollback-user"))
    count = await db_session.scalar(
        select(func.count()).select_from(Company).where(Company.inn == "7709999999")
    )
    assert int(count or 0) == 0
    accounts = await db_session.scalar(
        select(func.count()).select_from(CompanyAccount).where(
            CompanyAccount.login == "rollback-user"
        )
    )
    assert int(accounts or 0) == 0


@pytest.mark.db
@pytest.mark.asyncio
async def test_approve_pending_grants_catalog_and_writes_audit(db_session, client):
    admin = await _seed_admin(db_session)
    company = await register_company(db_session, _reg())
    await _assign_billing_entity(db_session, admin.id, company.id)
    approved = await approve_company(db_session, company.id, admin.id)
    assert approved is not None
    assert approved.status == CompanyStatus.ACTIVE.value
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "company.approve")
    )
    assert audit is not None
    login_response = await _login(client, "buyer-one", "password1")
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/catalog"
    catalog = await client.get("/catalog", follow_redirects=False)
    assert catalog.status_code == 200
    api = await client.get("/api/catalog/products")
    assert api.status_code == 200


@pytest.mark.db
@pytest.mark.asyncio
async def test_approve_non_pending_rejected(db_session):
    admin = await _seed_admin(db_session)
    created = await create_company(db_session, CompanyInput(name="Already Active"), admin.id)
    with pytest.raises(ValueError, match="на рассмотрении"):
        await approve_company(db_session, created.company_id, admin.id)


@pytest.mark.db
@pytest.mark.asyncio
async def test_reject_pending_blocks_catalog(db_session, client):
    admin = await _seed_admin(db_session)
    company = await register_company(db_session, _reg())
    rejected = await reject_company(db_session, company.id, admin.id, "Неполный пакет")
    assert rejected is not None
    assert rejected.status == CompanyStatus.REJECTED.value
    assert rejected.rejection_reason == "Неполный пакет"
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "company.reject")
    )
    assert audit is not None
    login_response = await _login(client, "buyer-one", "password1")
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/rejected"
    catalog = await client.get("/catalog", follow_redirects=False)
    assert catalog.status_code == 303
    assert catalog.headers["location"] == "/rejected"
    api = await client.get("/api/catalog/products")
    assert api.status_code == 403
    rejected_page = await client.get("/rejected")
    assert rejected_page.status_code == 200
    assert "Неполный пакет" in rejected_page.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_pending_html_and_api_forbidden(db_session, client):
    await register_company(db_session, _reg())
    login_response = await _login(client, "buyer-one", "password1")
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/pending"
    catalog = await client.get("/catalog", follow_redirects=False)
    assert catalog.status_code == 303
    assert catalog.headers["location"] == "/pending"
    product = await client.get(f"/catalog/products/{uuid4()}", follow_redirects=False)
    assert product.status_code == 303
    assert (await client.get("/api/catalog/products")).status_code == 403
    cart = await client.get("/cart", follow_redirects=False)
    assert cart.status_code == 303
    assert cart.headers["location"] == "/pending"
    assert (
        await client.put(
            "/api/cart/items",
            json={"product_id": str(uuid4()), "quantity": 1},
        )
    ).status_code == 403
    invoices = await client.get("/invoices", follow_redirects=False)
    assert invoices.status_code == 303
    assert (await client.get("/api/invoices")).status_code == 403
    assert (await client.post("/api/invoices", json={})).status_code == 403
    profile = await client.get("/profile")
    assert profile.status_code == 200
    pending = await client.get("/pending")
    assert pending.status_code == 200
    assert "Заявка на рассмотрении" in pending.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_active_customer_catalog_cart_invoice_routes(db_session, client):
    admin = await _seed_admin(db_session)
    created = await create_company(db_session, CompanyInput(name="Live Shop"), admin.id)
    login_response = await _login(client, created.login, created.temporary_password)
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/profile"
    await client.post(
        "/change-password",
        data={"new_password": "newpass12"},
        follow_redirects=False,
    )
    catalog = await client.get("/catalog", follow_redirects=False)
    assert catalog.status_code == 200
    assert (await client.get("/api/catalog/products")).status_code == 200
    assert (await client.get("/cart")).status_code == 200
    assert (await client.get("/api/cart")).status_code == 200
    assert (await client.get("/invoices")).status_code == 200
    assert (await client.get("/api/invoices")).status_code == 200


@pytest.mark.db
@pytest.mark.asyncio
async def test_suspended_customer_cannot_login(db_session):
    admin = await _seed_admin(db_session)
    created = await create_company(db_session, CompanyInput(name="Stop Shop"), admin.id)
    await deactivate_company(db_session, created.company_id, admin.id)
    result = await authenticate(
        db_session, SETTINGS, created.login, created.temporary_password, "t-susp"
    )
    assert result is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_http_register_and_register_page(client):
    page = await client.get("/register")
    assert page.status_code == 200
    assert "Регистрация" in page.text
    response = await client.post(
        "/api/auth/register",
        json={
            "login": "api-buyer",
            "password": "password1",
            "name": "API Buyer",
            "legal_name": "ООО АПИ",
            "inn": "7701111111",
            "contact_email": "api-buyer@example.com",
            "contact_phone": "+79991112233",
        },
        follow_redirects=False,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == CompanyStatus.PENDING.value
    catalog = await client.get("/api/catalog/products")
    assert catalog.status_code == 403


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_create_duplicate_inn_returns_400(db_session, client):
    await _seed_admin(db_session)
    await register_company(db_session, _reg())
    login_response = await _login(client, "reg-admin", "admin-pass")
    assert login_response.status_code == 303
    response = await client.post(
        "/api/admin/companies",
        json={"name": "Clone Co", "inn": "7701234567"},
    )
    assert response.status_code == 400
    assert "ИНН" in response.json()["detail"]

@pytest.mark.db
@pytest.mark.asyncio
async def test_approve_pending_without_billing_entity_fails(db_session):
    admin = await _seed_admin(db_session)
    company = await register_company(db_session, _reg())
    with pytest.raises(ValueError, match="юрлицо поставщика"):
        await approve_company(db_session, company.id, admin.id)
    db_session.expire_all()
    row = await db_session.get(Company, company.id)
    assert row is not None
    assert row.status == CompanyStatus.PENDING.value
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "company.approve")
    )
    assert audit is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_approve_pending_with_invalid_billing_entity_fails(db_session, monkeypatch):
    admin = await _seed_admin(db_session)
    company = await register_company(db_session, _reg())
    await _assign_billing_entity(db_session, admin.id, company.id)
    original_get = db_session.get

    async def fake_get(model, ident):
        if model is BillingEntity:
            return None
        return await original_get(model, ident)

    monkeypatch.setattr(db_session, "get", fake_get)
    with pytest.raises(ValueError, match="не найдено"):
        await approve_company(db_session, company.id, admin.id)
    db_session.expire_all()
    row = await db_session.get(Company, company.id)
    assert row is not None
    assert row.status == CompanyStatus.PENDING.value


@pytest.mark.db
@pytest.mark.asyncio
async def test_approve_pending_with_billing_entity_succeeds(db_session):
    admin = await _seed_admin(db_session)
    company = await register_company(db_session, _reg())
    await _assign_billing_entity(db_session, admin.id, company.id)
    approved = await approve_company(db_session, company.id, admin.id)
    assert approved is not None
    assert approved.status == CompanyStatus.ACTIVE.value
    assert approved.billing_entity_id is not None


@pytest.mark.db
@pytest.mark.asyncio
async def test_approve_api_rejects_without_billing_entity(db_session, client):
    await _seed_admin(db_session)
    company = await register_company(db_session, _reg())
    await _login(client, "reg-admin", "admin-pass")
    response = await client.post(f"/api/admin/companies/{company.id}/approve")
    assert response.status_code == 400
    assert "юрлицо поставщика" in response.json()["detail"]
    db_session.expire_all()
    row = await db_session.get(Company, company.id)
    assert row is not None
    assert row.status == CompanyStatus.PENDING.value


@pytest.mark.db
@pytest.mark.asyncio
async def test_approve_html_shows_error_without_billing_entity(db_session, client):
    await _seed_admin(db_session)
    company = await register_company(db_session, _reg())
    await _login(client, "reg-admin", "admin-pass")
    response = await client.post(f"/admin/companies/{company.id}/approve")
    assert response.status_code == 400
    assert "юрлицо поставщика" in response.text
    assert "disabled" not in response.text.lower() or "Одобрить" in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_invoice_after_approval_uses_billing_entity_snapshot(db_session):
    from decimal import Decimal

    admin = await _seed_admin(db_session)
    company = await register_company(db_session, _reg(login="invoice-buyer"))
    assigned = await _assign_billing_entity(db_session, admin.id, company.id)
    await approve_company(db_session, company.id, admin.id)
    warehouse = Warehouse(code="MAIN", name="Main", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()
    product = await create_product(
        db_session,
        ProductInput(
            name="After Approve Product",
            sale_price=Decimal("150"),
            cost_price=Decimal("50"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, 5, "seed", admin.id)
    await upsert_cart_item(db_session, company.id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session, company.id, admin.id, Settings()
    )
    assert invoice.seller.legal_name == "ИП Тестовый"
    assert invoice.seller.inn == "7701234560"
    assert assigned is not None
    assert assigned.billing_entity_name == "ИП Тест"

