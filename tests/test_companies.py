from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import Request
from httpx import AsyncClient
from sqlalchemy import select

from b2b_commerce.auth.deps import AuthContext, Forbidden, require_admin, require_admin_api
from b2b_commerce.auth.models import AdminUser, Session
from b2b_commerce.auth.service import authenticate, hash_password
from b2b_commerce.companies.models import Company, CompanyAccount
from b2b_commerce.companies.service import (
    COMPANIES_PAGE_SIZE,
    BillingEntityInput,
    CompanyInput,
    CompanyProfileInput,
    RegistrationInput,
    approve_company,
    create_billing_entity,
    create_company,
    deactivate_company,
    ensure_default_billing_entity,
    get_billing_entity,
    get_company,
    list_companies,
    register_company,
    reset_company_password,
    update_billing_entity,
    update_company_admin,
)
from b2b_commerce.config import Settings
from b2b_commerce.enums import CompanyStatus, InvoiceStatus, SessionSubjectType
from b2b_commerce.invoices.models import Invoice
from b2b_commerce.invoices.service import list_company_invoices


# Создаёт админа для тестов.
async def _seed_admin(db_session):
    admin = AdminUser(
        login="test-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


# Назначает billing_entity и одобряет pending-компанию.
async def _approve_with_billing(db_session, admin_id, company_id, company_name, inn_suffix):
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name=f"ИП {inn_suffix}",
            legal_name=f"ИП {inn_suffix}",
            inn=f"770555{inn_suffix}",
        ),
        admin_id,
    )
    await update_company_admin(
        db_session,
        company_id,
        CompanyProfileInput(name=company_name),
        admin_id,
        billing_entity_id=entity.id,
    )
    return await approve_company(db_session, company_id, admin_id)


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_company_hashes_password(db_session):
    admin = await _seed_admin(db_session)
    created = await create_company(
        db_session,
        CompanyInput(name="Test Shop"),
        admin.id,
    )
    account = await db_session.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == created.company_id)
    )
    assert account is not None
    assert account.login == created.login
    assert account.password_hash != created.temporary_password
    assert account.must_change_password is True


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_company_unique_login(db_session):
    admin = await _seed_admin(db_session)
    first = await create_company(db_session, CompanyInput(name="Same Name"), admin.id)
    second = await create_company(db_session, CompanyInput(name="Same Name"), admin.id)
    assert first.login != second.login


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_company_explicit_login(db_session):
    admin = await _seed_admin(db_session)
    created = await create_company(
        db_session,
        CompanyInput(name="Offline", login="offline-shop"),
        admin.id,
    )
    assert created.login == "offline-shop"


@pytest.mark.db
@pytest.mark.asyncio
async def test_reset_password_revokes_sessions(db_session):
    admin = await _seed_admin(db_session)
    created = await create_company(db_session, CompanyInput(name="Reset Co"), admin.id)
    account = await db_session.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == created.company_id)
    )
    settings = Settings()
    login_result = await authenticate(
        db_session,
        settings,
        created.login,
        created.temporary_password,
        "test-client",
    )
    assert login_result is not None
    sessions_before = (
        await db_session.scalars(
            select(Session).where(
                Session.subject_id == account.id,
                Session.revoked_at.is_(None),
            )
        )
    ).all()
    assert len(sessions_before) == 1

    await reset_company_password(db_session, created.company_id, admin.id)
    sessions_after = (
        await db_session.scalars(
            select(Session).where(
                Session.subject_id == account.id,
                Session.revoked_at.is_(None),
            )
        )
    ).all()
    assert sessions_after == []


@pytest.mark.asyncio
async def test_company_client_cannot_access_admin_catalog():
    auth = AuthContext(
        subject_type=SessionSubjectType.COMPANY,
        subject_id=uuid4(),
        session_id=uuid4(),
        company_id=uuid4(),
        must_change_password=True,
    )
    with pytest.raises(Forbidden) as html_exc:
        await require_admin(auth)
    assert html_exc.value.json_mode is False

    api_request = Request(
        {"type": "http", "path": "/api/admin/products", "method": "GET", "headers": []}
    )
    with pytest.raises(Forbidden) as api_exc:
        await require_admin_api(api_request, auth)
    assert api_exc.value.json_mode is True


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_company_duplicate_inn_raises(db_session):
    admin = await _seed_admin(db_session)
    await create_company(
        db_session,
        CompanyInput(name="First Inn", inn="7701234567"),
        admin.id,
    )
    with pytest.raises(ValueError, match="ИНН"):
        await create_company(
            db_session,
            CompanyInput(name="Second Inn", inn="7701234567"),
            admin.id,
        )


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_company_duplicate_email_raises(db_session):
    admin = await _seed_admin(db_session)
    await create_company(
        db_session,
        CompanyInput(name="First Mail", contact_email="same@example.com"),
        admin.id,
    )
    with pytest.raises(ValueError, match="email"):
        await create_company(
            db_session,
            CompanyInput(name="Second Mail", contact_email="Same@example.com"),
            admin.id,
        )


@pytest.mark.db
@pytest.mark.asyncio
async def test_list_companies_search_and_pagination(db_session):
    for index in range(COMPANIES_PAGE_SIZE + 1):
        db_session.add(
            Company(
                name=f"Paged {index:02d}",
                inn=f"77000000{index:02d}",
                status=CompanyStatus.ACTIVE.value,
            )
        )
    db_session.add(
        Company(name="Unique Needle", inn="7709999999", status=CompanyStatus.ACTIVE.value)
    )
    await db_session.commit()

    page1, total = await list_companies(db_session, page=1)
    assert total == COMPANIES_PAGE_SIZE + 2
    assert len(page1) == COMPANIES_PAGE_SIZE
    page2, _ = await list_companies(db_session, page=2)
    assert len(page2) == 2

    found, found_total = await list_companies(db_session, q="Needle")
    assert found_total == 1
    assert found[0].name == "Unique Needle"


@pytest.mark.db
@pytest.mark.asyncio
async def test_update_company_assigns_billing_entity(db_session):
    admin = await _seed_admin(db_session)
    created = await create_company(db_session, CompanyInput(name="Bill Me"), admin.id)
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name="ИП Основной",
            legal_name='ИП "Основной"',
            inn="7705555555",
        ),
        admin.id,
    )
    updated = await update_company_admin(
        db_session,
        created.company_id,
        CompanyProfileInput(name="Bill Me Ltd", inn="7706666666"),
        admin.id,
        billing_entity_id=entity.id,
    )
    assert updated is not None
    assert updated.name == "Bill Me Ltd"
    assert updated.inn == "7706666666"
    assert updated.billing_entity_id == entity.id
    assert updated.billing_entity_name == "ИП Основной"


@pytest.mark.db
@pytest.mark.asyncio
async def test_billing_entity_unique_inn(db_session):
    admin = await _seed_admin(db_session)
    await create_billing_entity(
        db_session,
        BillingEntityInput(name="First", legal_name="First LLC", inn="7707777777"),
        admin.id,
    )
    with pytest.raises(ValueError, match="ИНН"):
        await create_billing_entity(
            db_session,
            BillingEntityInput(name="Second", legal_name="Second LLC", inn="7707777777"),
            admin.id,
        )




@pytest.mark.db
@pytest.mark.asyncio
async def test_update_billing_entity_persists_bank_details(db_session):
    admin = await _seed_admin(db_session)
    created = await create_billing_entity(
        db_session,
        BillingEntityInput(name="First", legal_name="First LLC", inn="7708888888"),
        admin.id,
    )
    updated = await update_billing_entity(
        db_session,
        created.id,
        BillingEntityInput(
            name="ИП Обновлённый",
            legal_name="ИП Обновлённый",
            inn="7708888888",
            kpp="770101002",
            legal_address="Москва, новая",
            bank_name="Новый Банк",
            bik="044525999",
            bank_account="40702810100000000999",
            corr_account="30101810400000000999",
        ),
        admin.id,
    )
    assert updated is not None
    assert updated.bank_name == "Новый Банк"
    assert updated.bik == "044525999"
    assert updated.bank_account == "40702810100000000999"
    assert updated.corr_account == "30101810400000000999"
    assert updated.kpp == "770101002"
    loaded = await get_billing_entity(db_session, created.id)
    assert loaded is not None
    assert loaded.legal_name == "ИП Обновлённый"
    assert loaded.bank_account == "40702810100000000999"


@pytest.mark.db
@pytest.mark.asyncio
async def test_update_billing_entity_rejects_invalid_inn_and_duplicate(db_session):
    admin = await _seed_admin(db_session)
    first = await create_billing_entity(
        db_session,
        BillingEntityInput(name="A", legal_name="A LLC", inn="7707777777"),
        admin.id,
    )
    second = await create_billing_entity(
        db_session,
        BillingEntityInput(name="B", legal_name="B LLC", inn="7706666666"),
        admin.id,
    )
    with pytest.raises(ValueError, match="ИНН"):
        await update_billing_entity(
            db_session,
            second.id,
            BillingEntityInput(name="B", legal_name="B LLC", inn="123"),
            admin.id,
        )
    with pytest.raises(ValueError, match="ИНН"):
        await update_billing_entity(
            db_session,
            second.id,
            BillingEntityInput(name="B", legal_name="B LLC", inn=first.inn),
            admin.id,
        )
    with pytest.raises(ValueError, match="наименование"):
        await update_billing_entity(
            db_session,
            second.id,
            BillingEntityInput(name="  ", legal_name="B LLC", inn="7706666666"),
            admin.id,
        )


@pytest.mark.db
@pytest.mark.asyncio
async def test_update_billing_entity_html_and_api(db_session, client):
    admin = await _seed_admin(db_session)
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name="HTML IE",
            legal_name="ИП HTML",
            inn="7705555555",
            bank_name="Старый Банк",
            bank_account="40702810100000000011",
        ),
        admin.id,
    )
    await _login(client, "test-admin", "admin-pass")
    form = await client.get(f"/admin/billing-entities/{entity.id}/edit")
    assert form.status_code == 200
    assert "Расчётный счёт" in form.text
    assert "Корреспондентский счёт" in form.text
    saved = await client.post(
        f"/admin/billing-entities/{entity.id}/edit",
        data={
            "name": "HTML IE",
            "legal_name": "ИП HTML",
            "inn": "7705555555",
            "kpp": "770101003",
            "legal_address": "Адрес HTML",
            "bank_name": "Банк HTML",
            "bik": "044525111",
            "bank_account": "40702810100000000022",
            "corr_account": "30101810400000000022",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    db_session.expire_all()
    after_html = await get_billing_entity(db_session, entity.id)
    assert after_html is not None
    assert after_html.bank_account == "40702810100000000022"
    api = await client.put(
        f"/api/admin/billing-entities/{entity.id}",
        json={
            "name": "HTML IE",
            "legal_name": "ИП HTML",
            "inn": "7705555555",
            "kpp": "770101003",
            "legal_address": "Адрес HTML",
            "bank_name": "Банк API",
            "bik": "044525111",
            "bank_account": "40702810100000000033",
            "corr_account": "30101810400000000022",
        },
    )
    assert api.status_code == 200
    assert api.json()["bank_account"] == "40702810100000000033"
    assert api.json()["bank_name"] == "Банк API"

@pytest.mark.db
@pytest.mark.asyncio
async def test_ensure_default_billing_entity_idempotent(db_session):
    settings = Settings()
    first = await ensure_default_billing_entity(db_session, settings)
    second = await ensure_default_billing_entity(db_session, settings)
    assert first.id == second.id
    assert first.inn == settings.supplier_inn


# HTTP-клиент на той же test-БД, что и layout middleware.

# Логинит через HTML и ставит cookie.
async def _login(client: AsyncClient, login: str, password: str):
    return await client.post(
        "/login",
        data={"login": login, "password": password},
        follow_redirects=False,
    )


# Собирает POST-поля формы edit.
def _edit_form(**overrides) -> dict:
    data = {
        "name": "Edited Co",
        "legal_name": "",
        "inn": "",
        "contact_email": "",
        "contact_phone": "",
        "legal_address": "",
        "contact_person": "",
        "kpp": "",
        "delivery_address": "",
        "delivery_contact": "",
        "billing_entity_id": "",
    }
    data.update(overrides)
    return data


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_search_clear_drops_q_keeps_status(db_session, client):
    admin = await _seed_admin(db_session)
    await create_company(db_session, CompanyInput(name="Arena Search Co"), admin.id)
    await _login(client, "test-admin", "admin-pass")
    filtered = await client.get("/admin/companies", params={"q": "Arena", "status": "active"})
    assert filtered.status_code == 200
    assert "Arena Search Co" in filtered.text
    assert 'id="companies-filters"' in filtered.text
    assert "data-clear-url" in filtered.text
    assert "status=active" in filtered.text
    cleared = await client.get("/admin/companies", params={"status": "active"})
    assert cleared.status_code == 200
    assert "q=" not in str(cleared.url) or "q=&" not in str(cleared.url)
    empty_q = await client.get("/admin/companies", params={"q": "", "status": "active"})
    assert empty_q.status_code == 200
    assert "Arena Search Co" in empty_q.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_list_companies_status_and_search_pagination(db_session):
    for index in range(COMPANIES_PAGE_SIZE + 2):
        db_session.add(
            Company(
                name=f"Pending Page {index:02d}",
                inn=f"77110000{index:02d}",
                status=CompanyStatus.PENDING.value,
            )
        )
    db_session.add(
        Company(
            name="Active Needle",
            inn="7711999999",
            status=CompanyStatus.ACTIVE.value,
        )
    )
    await db_session.commit()
    page1, total = await list_companies(db_session, status="pending", page=1)
    assert total == COMPANIES_PAGE_SIZE + 2
    assert len(page1) == COMPANIES_PAGE_SIZE
    page2, _ = await list_companies(db_session, status="pending", page=2)
    assert len(page2) == 2
    found, found_total = await list_companies(db_session, status="active", q="Needle")
    assert found_total == 1
    assert found[0].name == "Active Needle"
    none, none_total = await list_companies(db_session, status="pending", q="Needle")
    assert none_total == 0
    assert none == []


@pytest.mark.db
@pytest.mark.asyncio
async def test_edit_duplicate_inn_and_email_html_and_api(db_session, client):
    admin = await _seed_admin(db_session)
    first = await create_company(
        db_session,
        CompanyInput(name="Alpha Co", inn="7701111111", contact_email="alpha@example.com"),
        admin.id,
    )
    second = await create_company(
        db_session,
        CompanyInput(name="Beta Co", inn="7702222222", contact_email="beta@example.com"),
        admin.id,
    )
    await _login(client, "test-admin", "admin-pass")
    html_inn = await client.post(
        f"/admin/companies/{second.company_id}/edit",
        data=_edit_form(name="Beta Co", inn="7701111111"),
    )
    assert html_inn.status_code == 400
    assert "ИНН" in html_inn.text
    html_email = await client.post(
        f"/admin/companies/{second.company_id}/edit",
        data=_edit_form(name="Beta Co", contact_email="alpha@example.com"),
    )
    assert html_email.status_code == 400
    assert "email" in html_email.text.lower()
    api_inn = await client.patch(
        f"/api/admin/companies/{second.company_id}",
        json={"name": "Beta Co", "inn": "7701111111"},
    )
    assert api_inn.status_code == 400
    assert "ИНН" in api_inn.json()["detail"]
    api_email = await client.patch(
        f"/api/admin/companies/{second.company_id}",
        json={"name": "Beta Co", "contact_email": "alpha@example.com"},
    )
    assert api_email.status_code == 400
    db_session.expire_all()
    still = await get_company(db_session, second.company_id)
    assert still is not None
    assert still.inn == "7702222222"
    assert first.login != second.login


@pytest.mark.db
@pytest.mark.asyncio
async def test_edit_cannot_change_status_html_or_api(db_session, client):
    admin = await _seed_admin(db_session)
    admin_id = admin.id
    pending = await register_company(
        db_session,
        RegistrationInput(
            login="qa-pending",
            password="password1",
            name="Pending Edit",
            legal_name="ООО Pending",
            inn="7703333333",
            contact_email="pending-edit@example.com",
            contact_phone="+79990001122",
        ),
    )
    await _login(client, "test-admin", "admin-pass")
    html = await client.post(
        f"/admin/companies/{pending.id}/edit",
        data=_edit_form(name="Pending Edit", status="active"),
    )
    assert html.status_code == 303
    db_session.expire_all()
    after_html = await get_company(db_session, pending.id)
    assert after_html is not None
    assert after_html.status == CompanyStatus.PENDING.value
    api = await client.patch(
        f"/api/admin/companies/{pending.id}",
        json={"name": "Pending Edit", "status": "active"},
    )
    assert api.status_code == 200
    assert api.json()["status"] == CompanyStatus.PENDING.value
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(name="ИП QA", legal_name="ИП QA", inn="7703333340"),
        admin_id,
    )
    await update_company_admin(
        db_session,
        pending.id,
        CompanyProfileInput(name="Pending Edit"),
        admin_id,
        billing_entity_id=entity.id,
    )
    db_session.expire_all()
    approved = await approve_company(db_session, pending.id, admin_id)
    assert approved is not None
    assert approved.status == CompanyStatus.ACTIVE.value
    await deactivate_company(db_session, pending.id, admin_id)
    stopped = await get_company(db_session, pending.id)
    assert stopped is not None
    assert stopped.status == CompanyStatus.SUSPENDED.value


@pytest.mark.db
@pytest.mark.asyncio
async def test_approve_requires_billing_entity_before_catalog(db_session, client):
    await register_company(
        db_session,
        RegistrationInput(
            login="no-bill",
            password="password1",
            name="No Billing Co",
            legal_name="ООО NoBill",
            inn="7704444444",
            contact_email="nobill@example.com",
            contact_phone="+79990002233",
        ),
    )
    admin = await _seed_admin(db_session)
    company = await db_session.scalar(select(Company).where(Company.name == "No Billing Co"))
    assert company is not None
    assert company.billing_entity_id is None
    with pytest.raises(ValueError, match="юрлицо поставщика"):
        await approve_company(db_session, company.id, admin.id)
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(name="ИП Каталог", legal_name="ИП Каталог", inn="7704444440"),
        admin.id,
    )
    await update_company_admin(
        db_session,
        company.id,
        CompanyProfileInput(name="No Billing Co"),
        admin.id,
        billing_entity_id=entity.id,
    )
    await approve_company(db_session, company.id, admin.id)
    await _login(client, "no-bill", "password1")
    catalog = await client.get("/catalog", follow_redirects=False)
    assert catalog.status_code == 200
    api = await client.get("/api/catalog/products")
    assert api.status_code == 200
    cart = await client.get("/cart", follow_redirects=False)
    assert cart.status_code == 200


@pytest.mark.db
@pytest.mark.asyncio
async def test_reassign_billing_entity(db_session):
    admin = await _seed_admin(db_session)
    created = await create_company(db_session, CompanyInput(name="Switch Bill"), admin.id)
    first = await create_billing_entity(
        db_session,
        BillingEntityInput(name="ИП А", legal_name="ИП А", inn="7708888888"),
        admin.id,
    )
    second = await create_billing_entity(
        db_session,
        BillingEntityInput(name="ИП Б", legal_name="ИП Б", inn="7708888889"),
        admin.id,
    )
    await update_company_admin(
        db_session,
        created.company_id,
        CompanyProfileInput(name="Switch Bill"),
        admin.id,
        billing_entity_id=first.id,
    )
    updated = await update_company_admin(
        db_session,
        created.company_id,
        CompanyProfileInput(name="Switch Bill"),
        admin.id,
        billing_entity_id=second.id,
    )
    assert updated is not None
    assert updated.billing_entity_id == second.id
    assert updated.billing_entity_name == "ИП Б"
    row = await db_session.get(Company, created.company_id)
    assert row is not None
    assert row.billing_entity_id == second.id


@pytest.mark.db
@pytest.mark.asyncio
async def test_company_invoice_history_is_isolated(db_session, client):
    admin = await _seed_admin(db_session)
    owner = await create_company(db_session, CompanyInput(name="Hist Owner"), admin.id)
    other = await create_company(db_session, CompanyInput(name="Hist Other"), admin.id)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            Invoice(
                company_id=owner.company_id,
                number="QA-OWN-AWAIT",
                status=InvoiceStatus.AWAITING_PAYMENT.value,
                subtotal=Decimal("1000.00"),
                total=Decimal("1000.00"),
                created_at=now - timedelta(days=2),
                expires_at=now + timedelta(days=3),
            ),
            Invoice(
                company_id=owner.company_id,
                number="QA-OWN-PAID",
                status=InvoiceStatus.PAID.value,
                subtotal=Decimal("2000.00"),
                total=Decimal("2000.00"),
                created_at=now - timedelta(days=1),
                paid_at=now,
            ),
            Invoice(
                company_id=owner.company_id,
                number="QA-OWN-EXPIRED",
                status=InvoiceStatus.EXPIRED.value,
                subtotal=Decimal("3000.00"),
                total=Decimal("3000.00"),
                created_at=now - timedelta(days=8),
                expires_at=now - timedelta(days=3),
            ),
            Invoice(
                company_id=other.company_id,
                number="QA-FOREIGN",
                status=InvoiceStatus.PAID.value,
                subtotal=Decimal("999.00"),
                total=Decimal("999.00"),
                created_at=now,
                paid_at=now,
            ),
        ]
    )
    await db_session.commit()
    history, _ = await list_company_invoices(db_session, owner.company_id, page_size=None)
    numbers = [item.number for item in history]
    assert numbers == ["QA-OWN-PAID", "QA-OWN-AWAIT", "QA-OWN-EXPIRED"]
    assert "QA-FOREIGN" not in numbers
    assert {item.status for item in history} == {
        InvoiceStatus.AWAITING_PAYMENT.value,
        InvoiceStatus.PAID.value,
        InvoiceStatus.EXPIRED.value,
    }
    await _login(client, "test-admin", "admin-pass")
    detail = await client.get(f"/admin/companies/{owner.company_id}")
    assert detail.status_code == 200
    assert "QA-OWN-PAID" in detail.text
    assert "QA-OWN-AWAIT" in detail.text
    assert "QA-OWN-EXPIRED" in detail.text
    assert "QA-FOREIGN" not in detail.text
    assert "/admin/invoices/" in detail.text
    assert "2 000" in detail.text or "2000" in detail.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_cannot_see_other_company(db_session, client):
    admin = await _seed_admin(db_session)
    first = await register_company(
        db_session,
        RegistrationInput(
            login="iso-a",
            password="password1",
            name="Iso Alpha",
            legal_name="ООО IsoA",
            inn="7705550001",
            contact_email="iso-a@example.com",
            contact_phone="+79990003344",
        ),
    )
    second = await register_company(
        db_session,
        RegistrationInput(
            login="iso-b",
            password="password1",
            name="Iso Beta",
            legal_name="ООО IsoB",
            inn="7705550002",
            contact_email="iso-b@example.com",
            contact_phone="+79990003355",
        ),
    )
    await _approve_with_billing(db_session, admin.id, first.id, "Iso Alpha", "0001")
    await _approve_with_billing(db_session, admin.id, second.id, "Iso Beta", "0002")
    await update_company_admin(
        db_session,
        first.id,
        CompanyProfileInput(name="Iso Alpha Edited", inn="7705550001"),
        admin.id,
    )
    await _login(client, "iso-a", "password1")
    profile = await client.get("/profile")
    assert profile.status_code == 200
    assert "Iso Alpha Edited" in profile.text
    assert "Iso Beta" not in profile.text
    admin_html = await client.get(f"/admin/companies/{second.id}", follow_redirects=False)
    assert admin_html.status_code in {303, 403}
    admin_api = await client.get(f"/api/admin/companies/{second.id}")
    assert admin_api.status_code == 403
    missing = await client.get("/companies/" + str(second.id), follow_redirects=False)
    assert missing.status_code in {404, 303, 403}
    api_missing = await client.get("/api/companies/" + str(second.id))
    assert api_missing.status_code in {404, 403, 405}
