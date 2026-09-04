import logging
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from b2b_commerce.audit.models import AuditLog
from b2b_commerce.auth.models import AdminUser, Session
from b2b_commerce.auth.service import authenticate, hash_password
from b2b_commerce.companies.models import CompanyAccount
from b2b_commerce.companies.service import (
    CompanyInput,
    RegistrationInput,
    create_company,
    register_company,
    reject_company,
)
from b2b_commerce.config import Settings

SETTINGS = Settings()
NEW_PASSWORD = "newpass1234"
ROOT = Path(__file__).resolve().parents[1]


async def _seed_admin(db_session):
    admin = AdminUser(
        login="profile-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


def _registration(**overrides) -> RegistrationInput:
    suffix = uuid4().hex[:8]
    data = dict(
        login=f"buyer-{suffix}",
        password="password1",
        name="Buyer One",
        legal_name='ООО "Байер"',
        inn=f"77{int(suffix, 16) % 10**8:08d}",
        contact_email=f"buyer-{suffix}@example.com",
        contact_phone="+79991234567",
    )
    data.update(overrides)
    return RegistrationInput(**data)



async def _login(client: AsyncClient, login: str, password: str):
    return await client.post(
        "/login",
        data={"login": login, "password": password},
        follow_redirects=False,
    )


async def _active_company(db_session, admin_id, login: str | None = None):
    data = CompanyInput(name="Active Shop", login=login)
    return await create_company(db_session, data, admin_id)


async def _pending_company(db_session):
    reg = _registration()
    company = await register_company(db_session, reg)
    return company, reg.login, reg.password


async def _rejected_company(db_session, admin_id):
    company, login, password = await _pending_company(db_session)
    await reject_company(db_session, company.id, admin_id, reason="Не подходит")
    account = await db_session.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == company.id)
    )
    return company, login, password, account.login


async def _change_password(client: AsyncClient, new_password: str = NEW_PASSWORD):
    return await client.post(
        "/change-password",
        data={"new_password": new_password},
        follow_redirects=False,
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_active_password_change(db_session, client):
    admin = await _seed_admin(db_session)
    created = await _active_company(db_session, admin.id)
    await _login(client, created.login, created.temporary_password)

    response = await _change_password(client)
    assert response.status_code == 200
    assert NEW_PASSWORD in response.text
    assert "data-credential-flash" in response.text
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.db
@pytest.mark.asyncio
async def test_pending_password_change(db_session, client):
    _, login, password = await _pending_company(db_session)
    await _login(client, login, password)

    response = await _change_password(client, "pending-pass12")
    assert response.status_code == 200
    assert "pending-pass12" in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_rejected_cannot_change_password(db_session, client):
    admin = await _seed_admin(db_session)
    _, login, password, _ = await _rejected_company(db_session, admin.id)
    await _login(client, login, password)

    response = await _change_password(client, "reject-pass1")
    assert response.status_code == 400
    assert "отклонённой" in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_short_password(db_session, client):
    admin = await _seed_admin(db_session)
    created = await _active_company(db_session, admin.id)
    await _login(client, created.login, created.temporary_password)

    response = await _change_password(client, "short")
    assert response.status_code == 400
    assert "Пароль" in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_other_sessions_revoked_on_password_change(db_session, client):
    admin = await _seed_admin(db_session)
    created = await _active_company(db_session, admin.id)
    account = await db_session.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == created.company_id)
    )
    await authenticate(
        db_session, SETTINGS, created.login, created.temporary_password, "client-a"
    )
    login_b = await _login(client, created.login, created.temporary_password)
    assert login_b.status_code == 303

    await _change_password(client)
    active_sessions = (
        await db_session.scalars(
            select(Session).where(
                Session.subject_id == account.id,
                Session.revoked_at.is_(None),
            )
        )
    ).all()
    assert len(active_sessions) == 1


@pytest.mark.db
@pytest.mark.asyncio
async def test_password_only_in_post_template_response(db_session, client):
    admin = await _seed_admin(db_session)
    created = await _active_company(db_session, admin.id)
    await _login(client, created.login, created.temporary_password)

    response = await _change_password(client)
    assert NEW_PASSWORD in response.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_get_profile_after_password_change_has_no_password(db_session, client):
    admin = await _seed_admin(db_session)
    created = await _active_company(db_session, admin.id)
    await _login(client, created.login, created.temporary_password)
    await _change_password(client)

    profile = await client.get("/profile")
    assert profile.status_code == 200
    assert NEW_PASSWORD not in profile.text
    assert "data-credential-flash data-password" not in profile.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_second_get_profile_has_no_password(db_session, client):
    admin = await _seed_admin(db_session)
    created = await _active_company(db_session, admin.id)
    await _login(client, created.login, created.temporary_password)
    await _change_password(client)

    first = await client.get("/profile")
    second = await client.get("/profile")
    assert NEW_PASSWORD not in first.text
    assert NEW_PASSWORD not in second.text


@pytest.mark.db
@pytest.mark.asyncio
async def test_password_absent_from_audit_and_logs(db_session, client, caplog):
    admin = await _seed_admin(db_session)
    created = await _active_company(db_session, admin.id)
    await _login(client, created.login, created.temporary_password)

    with caplog.at_level(logging.INFO):
        await _change_password(client)

    logs = caplog.text + str([rec.message for rec in caplog.records])
    assert NEW_PASSWORD not in logs

    rows = (await db_session.scalars(select(AuditLog))).all()
    for row in rows:
        payload = str(row.payload or {})
        assert NEW_PASSWORD not in payload
        assert created.temporary_password not in payload


@pytest.mark.db
@pytest.mark.asyncio
async def test_must_change_password_flow(db_session, client):
    admin = await _seed_admin(db_session)
    created = await _active_company(db_session, admin.id)
    login_response = await _login(client, created.login, created.temporary_password)
    assert login_response.headers["location"] == "/profile"

    change = await _change_password(client)
    assert change.status_code == 200
    assert NEW_PASSWORD in change.text

    catalog = await client.get("/catalog", follow_redirects=False)
    assert catalog.status_code == 200


@pytest.mark.db
@pytest.mark.asyncio
async def test_api_change_password_returns_ok_without_plaintext(db_session, client):
    admin = await _seed_admin(db_session)
    created = await _active_company(db_session, admin.id)
    await _login(client, created.login, created.temporary_password)

    response = await client.post(
        "/api/auth/change-password",
        json={"new_password": NEW_PASSWORD},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert NEW_PASSWORD not in response.text


@pytest.mark.db
def test_dead_change_password_template_removed():
    template = ROOT / "src/b2b_commerce/templates/auth/change_password.html"
    assert not template.exists()
    for folder in ("src", "tests", "docs"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".html", ".md"}:
                if path.name == "test_profile_credentials.py":
                    continue
                assert "change_password.html" not in path.read_text(encoding="utf-8")
