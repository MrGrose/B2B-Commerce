import pytest
from sqlalchemy import select

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.companies.models import CompanyAccount
from b2b_commerce.companies.service import CompanyInput, create_company
from b2b_commerce.enums import SessionSubjectType, SupportTicketStatus
from b2b_commerce.support.service import (
    add_message,
    close_ticket,
    count_open_tickets,
    create_ticket,
    get_company_ticket,
    list_all_tickets,
)


# Создаёт админа для тестов.
async def _seed_admin(db_session):
    admin = AdminUser(
        login="support-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


# Возвращает учётку компании.
async def _account_id(db_session, company_id):
    return (
        await db_session.scalar(
            select(CompanyAccount.id).where(CompanyAccount.company_id == company_id)
        )
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_ticket_stores_message(db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Support Co"), admin.id)
    ticket = await create_ticket(
        db_session,
        company.company_id,
        await _account_id(db_session, company.company_id),
        "Проблема с заказом",
        "Не могу оформить счёт",
    )
    assert ticket.status == SupportTicketStatus.OPEN.value
    assert ticket.company_name == "Support Co"
    assert len(ticket.messages) == 1
    assert ticket.messages[0].body == "Не могу оформить счёт"


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_ticket_rejects_empty_subject(db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Empty Subject Co"), admin.id)
    account_id = await _account_id(db_session, company.company_id)
    with pytest.raises(ValueError, match="Тема не может быть пустой"):
        await create_ticket(db_session, company.company_id, account_id, "   ", "Текст обращения")


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_ticket_rejects_empty_body(db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Empty Body Co"), admin.id)
    account_id = await _account_id(db_session, company.company_id)
    with pytest.raises(ValueError, match="Сообщение не может быть пустым"):
        await create_ticket(db_session, company.company_id, account_id, "Тема", "  ")


@pytest.mark.db
@pytest.mark.asyncio
async def test_company_cannot_see_other_ticket(db_session):
    admin = await _seed_admin(db_session)
    first = await create_company(db_session, CompanyInput(name="Company A"), admin.id)
    second = await create_company(db_session, CompanyInput(name="Company B"), admin.id)
    account_a = await db_session.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == first.company_id)
    )
    ticket = await create_ticket(
        db_session,
        first.company_id,
        account_a.id,
        "Только A",
        "Секрет",
    )
    other_view = await get_company_ticket(db_session, second.company_id, ticket.id)
    assert other_view is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_reply_and_close_ticket(db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Reply Co"), admin.id)
    account = await db_session.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == company.company_id)
    )
    ticket = await create_ticket(
        db_session,
        company.company_id,
        account.id,
        "Вопрос",
        "Нужна помощь",
    )
    replied = await add_message(
        db_session,
        ticket.id,
        SessionSubjectType.ADMIN,
        admin.id,
        "Мы разбираемся",
    )
    assert replied is not None
    assert len(replied.messages) == 2

    closed = await close_ticket(db_session, ticket.id, admin.id)
    assert closed is not None
    assert closed.status == SupportTicketStatus.CLOSED.value

    with pytest.raises(ValueError, match="закрыт"):
        await add_message(
            db_session,
            ticket.id,
            SessionSubjectType.COMPANY,
            account.id,
            "Ещё вопрос",
            company_id=company.company_id,
        )


@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_sees_all_tickets(db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Visible Co"), admin.id)
    account = await db_session.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == company.company_id)
    )
    await create_ticket(
        db_session,
        company.company_id,
        account.id,
        "Видимый",
        "Текст",
    )
    tickets, _ = await list_all_tickets(db_session, page_size=None)
    assert len(tickets) == 1
    assert tickets[0].company_name == "Visible Co"

@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_can_close_own_open_ticket(db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Close Co"), admin.id)
    account_id = await _account_id(db_session, company.company_id)
    ticket = await create_ticket(
        db_session,
        company.company_id,
        account_id,
        "Закрыть",
        "Проверка",
    )
    from b2b_commerce.support.service import close_ticket_by_customer

    closed = await close_ticket_by_customer(
        db_session,
        company.company_id,
        ticket.id,
        account_id,
    )
    assert closed is not None
    assert closed.status == SupportTicketStatus.CLOSED.value


@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_cannot_close_foreign_ticket(db_session):
    admin = await _seed_admin(db_session)
    owner = await create_company(db_session, CompanyInput(name="Owner Co"), admin.id)
    other = await create_company(db_session, CompanyInput(name="Other Co"), admin.id)
    owner_account = await _account_id(db_session, owner.company_id)
    ticket = await create_ticket(
        db_session,
        owner.company_id,
        owner_account,
        "Чужой",
        "Текст",
    )
    from b2b_commerce.support.service import close_ticket_by_customer

    result = await close_ticket_by_customer(
        db_session,
        other.company_id,
        ticket.id,
        await _account_id(db_session, other.company_id),
    )
    assert result is None



@pytest.mark.db
@pytest.mark.asyncio
async def test_count_open_tickets_excludes_closed(db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="Open Co"), admin.id)
    account_id = await _account_id(db_session, company.company_id)
    open_ticket = await create_ticket(
        db_session,
        company.company_id,
        account_id,
        "Открыт",
        "Текст",
    )
    closed_ticket = await create_ticket(
        db_session,
        company.company_id,
        account_id,
        "Закрыт",
        "Текст",
    )
    await close_ticket(db_session, closed_ticket.id, admin.id)
    assert await count_open_tickets(db_session) == 1
    await close_ticket(db_session, open_ticket.id, admin.id)
    assert await count_open_tickets(db_session) == 0
