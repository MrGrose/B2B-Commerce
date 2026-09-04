import pytest

from b2b_commerce.audit.labels import format_audit_log
from b2b_commerce.audit.service import AUDIT_PAGE_SIZE, count_audit_logs, list_audit_logs
from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.companies.service import CompanyInput, create_company


# Создаёт админа для тестов.
async def _seed_admin(db_session):
    admin = AdminUser(
        login="audit-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin



@pytest.mark.db
@pytest.mark.asyncio
async def test_company_create_writes_audit_log(db_session):
    admin = await _seed_admin(db_session)
    await create_company(db_session, CompanyInput(name="Audit Co"), admin.id)
    logs = await list_audit_logs(db_session)
    assert len(logs) >= 1
    assert logs[0].action == "company.create"
    assert logs[0].entity_type == "company"
    assert logs[0].actor_type == "admin"


@pytest.mark.db
@pytest.mark.asyncio
async def test_list_audit_logs_respects_limit(db_session):
    admin = await _seed_admin(db_session)
    await create_company(db_session, CompanyInput(name="One"), admin.id)
    await create_company(db_session, CompanyInput(name="Two"), admin.id)
    logs = await list_audit_logs(db_session, limit=1)
    assert len(logs) == 1


@pytest.mark.db
@pytest.mark.asyncio
async def test_format_audit_log_russian_labels(db_session):
    admin = await _seed_admin(db_session)
    await create_company(db_session, CompanyInput(name="Label Co"), admin.id)
    row = (await list_audit_logs(db_session, limit=1))[0]
    view = format_audit_log(row)
    assert view.action_label == "Создана компания"
    assert view.entity_label == "Компания"
    assert view.actor_label == "Администратор"


@pytest.mark.db
@pytest.mark.asyncio
async def test_list_audit_logs_pagination(db_session):
    admin = await _seed_admin(db_session)
    for index in range(AUDIT_PAGE_SIZE + 2):
        await create_company(db_session, CompanyInput(name=f"Pag Co {index}"), admin.id)
    total = await count_audit_logs(db_session)
    assert total >= AUDIT_PAGE_SIZE + 2
    page1 = await list_audit_logs(db_session, limit=AUDIT_PAGE_SIZE, offset=0)
    page2 = await list_audit_logs(db_session, limit=AUDIT_PAGE_SIZE, offset=AUDIT_PAGE_SIZE)
    assert len(page1) == AUDIT_PAGE_SIZE
    assert len(page2) >= 2
    assert page1[0].id != page2[0].id


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_audit_page_pagination_http(db_session, client):
    admin = await _seed_admin(db_session)
    for index in range(AUDIT_PAGE_SIZE + 1):
        await create_company(db_session, CompanyInput(name=f"HTTP Pag {index}"), admin.id)
    login = await client.post(
        "/login",
        data={"login": admin.login, "password": "admin-pass"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    page1 = await client.get("/admin/audit")
    assert page1.status_code == 200
    assert "pagination" in page1.text
    page2 = await client.get("/admin/audit?page=2")
    assert page2.status_code == 200
    assert 'aria-current="page">2</span>' in page2.text


def test_settings_audit_labels_russian():
    from b2b_commerce.audit.labels import (
        action_label,
        entity_type_label,
        format_audit_payload,
    )

    assert action_label("brand.create") == "Создан бренд"
    assert action_label("category.margin.update") == "Изменена маржа категории"
    assert action_label("admin.create") == "Добавлен администратор"
    assert entity_type_label("brand") == "Бренд"
    assert entity_type_label("admin_user") == "Администратор"
    details = format_audit_payload(
        "category.margin.update",
        {
            "name": "Ракетки",
            "old_margin_percent": "25",
            "new_margin_percent": "35",
        },
    )
    assert "Ракетки" in details
    assert "35" in details
