import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.audit.service import write_audit
from b2b_commerce.companies.models import Company
from b2b_commerce.enums import SessionSubjectType, SupportTicketStatus
from b2b_commerce.notifications.service import (
    emit_support_client_message,
    emit_support_new_ticket,
    emit_support_reply,
)
from b2b_commerce.support.models import SupportMessage, SupportTicket

SUPPORT_PAGE_SIZE = 30
logger = logging.getLogger(__name__)


@dataclass
class SupportMessageView:
    id: UUID
    author_type: str
    author_id: UUID
    body: str
    created_at: datetime


@dataclass
class SupportTicketView:
    id: UUID
    company_id: UUID
    company_name: str
    status: str
    subject: str
    created_at: datetime
    updated_at: datetime
    messages: list[SupportMessageView]


# Собирает карточку тикета для списка без сообщений.
def _ticket_list_view(ticket: SupportTicket, company_name: str) -> SupportTicketView:
    return SupportTicketView(
        id=ticket.id,
        company_id=ticket.company_id,
        company_name=company_name,
        status=ticket.status,
        subject=ticket.subject,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        messages=[],
    )


# Собирает список тикетов без загрузки сообщений.
async def _ticket_list_views(
    db: AsyncSession,
    tickets: list[SupportTicket],
) -> list[SupportTicketView]:
    if not tickets:
        return []
    company_ids = {ticket.company_id for ticket in tickets}
    company_rows = await db.execute(
        select(Company.id, Company.name).where(Company.id.in_(company_ids))
    )
    company_names = {row[0]: row[1] for row in company_rows.all()}
    return [
        _ticket_list_view(
            ticket,
            company_names.get(ticket.company_id, "—"),
        )
        for ticket in tickets
    ]


# Собирает карточку тикета с сообщениями.
async def _ticket_view(db: AsyncSession, ticket: SupportTicket) -> SupportTicketView:
    company = await db.get(Company, ticket.company_id)
    messages = (
        await db.scalars(
            select(SupportMessage)
            .where(SupportMessage.ticket_id == ticket.id)
            .order_by(SupportMessage.created_at)
        )
    ).all()
    view = _ticket_list_view(
        ticket,
        company.name if company else "—",
    )
    view.messages = [
        SupportMessageView(
            id=message.id,
            author_type=message.author_type,
            author_id=message.author_id,
            body=message.body,
            created_at=message.created_at,
        )
        for message in messages
    ]
    return view


# Возвращает тикет компании или None.
async def get_company_ticket(
    db: AsyncSession,
    company_id: UUID,
    ticket_id: UUID,
) -> SupportTicketView | None:
    ticket = await db.scalar(
        select(SupportTicket).where(
            SupportTicket.id == ticket_id,
            SupportTicket.company_id == company_id,
        )
    )
    if ticket is None:
        return None
    return await _ticket_view(db, ticket)


# Возвращает тикет для админки или None.
async def get_ticket(db: AsyncSession, ticket_id: UUID) -> SupportTicketView | None:
    ticket = await db.get(SupportTicket, ticket_id)
    if ticket is None:
        return None
    return await _ticket_view(db, ticket)


# Считает тикеты компании.
async def count_company_tickets(
    db: AsyncSession,
    company_id: UUID,
    status: str | None = None,
) -> int:
    stmt = (
        select(func.count())
        .select_from(SupportTicket)
        .where(SupportTicket.company_id == company_id)
    )
    if status is not None:
        stmt = stmt.where(SupportTicket.status == status)
    return int(await db.scalar(stmt) or 0)


# Список тикетов компании (page_size=None — все строки).
async def list_company_tickets(
    db: AsyncSession,
    company_id: UUID,
    page: int = 1,
    page_size: int | None = SUPPORT_PAGE_SIZE,
    status: str | None = None,
) -> tuple[list[SupportTicketView], int]:
    total = await count_company_tickets(db, company_id, status=status)
    page = max(1, page)
    stmt = (
        select(SupportTicket)
        .where(SupportTicket.company_id == company_id)
        .order_by(SupportTicket.updated_at.desc())
    )
    if status is not None:
        stmt = stmt.where(SupportTicket.status == status)
    if page_size is not None:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    tickets = (await db.scalars(stmt)).all()
    return await _ticket_list_views(db, list(tickets)), total


# Считает все тикеты.
async def count_all_tickets(db: AsyncSession, status: str | None = None) -> int:
    stmt = select(func.count()).select_from(SupportTicket)
    if status is not None:
        stmt = stmt.where(SupportTicket.status == status)
    return int(await db.scalar(stmt) or 0)


# Считает открытые тикеты для бейджа в админ-меню.
async def count_open_tickets(db: AsyncSession) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(SupportTicket)
            .where(SupportTicket.status == SupportTicketStatus.OPEN.value)
        )
        or 0
    )


# Считает тикеты клиента, где последнее сообщение от админа.
async def count_company_support_alerts(db: AsyncSession, company_id: UUID) -> int:
    latest = (
        select(
            SupportMessage.ticket_id,
            func.max(SupportMessage.created_at).label("latest_at"),
        )
        .group_by(SupportMessage.ticket_id)
        .subquery()
    )
    return int(
        await db.scalar(
            select(func.count())
            .select_from(SupportTicket)
            .join(latest, SupportTicket.id == latest.c.ticket_id)
            .join(
                SupportMessage,
                (SupportMessage.ticket_id == latest.c.ticket_id)
                & (SupportMessage.created_at == latest.c.latest_at),
            )
            .where(
                SupportTicket.company_id == company_id,
                SupportTicket.status == SupportTicketStatus.OPEN.value,
                SupportMessage.author_type == SessionSubjectType.ADMIN.value,
            )
        )
        or 0
    )


# Список всех тикетов для админки (page_size=None — все строки).
async def list_all_tickets(
    db: AsyncSession,
    page: int = 1,
    page_size: int | None = SUPPORT_PAGE_SIZE,
    status: str | None = None,
) -> tuple[list[SupportTicketView], int]:
    total = await count_all_tickets(db, status=status)
    page = max(1, page)
    stmt = select(SupportTicket).order_by(SupportTicket.updated_at.desc())
    if status is not None:
        stmt = stmt.where(SupportTicket.status == status)
    if page_size is not None:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    tickets = (await db.scalars(stmt)).all()
    return await _ticket_list_views(db, list(tickets)), total


# Создаёт тикет с первым сообщением.
async def create_ticket(
    db: AsyncSession,
    company_id: UUID,
    account_id: UUID,
    subject: str,
    body: str,
) -> SupportTicketView:
    subject = subject.strip()
    body = body.strip()
    if not subject:
        raise ValueError("Тема не может быть пустой")
    if not body:
        raise ValueError("Сообщение не может быть пустым")
    ticket = SupportTicket(
        company_id=company_id,
        status=SupportTicketStatus.OPEN.value,
        subject=subject,
    )
    db.add(ticket)
    await db.flush()
    db.add(
        SupportMessage(
            ticket_id=ticket.id,
            author_type=SessionSubjectType.COMPANY.value,
            author_id=account_id,
            body=body,
        )
    )
    await write_audit(
        db,
        SessionSubjectType.COMPANY.value,
        account_id,
        "support.create",
        "support_ticket",
        ticket.id,
        {"subject": subject},
    )
    await emit_support_new_ticket(db, ticket)
    await db.commit()
    logger.info("Создан тикет %s для компании %s", ticket.id, company_id)
    return await _ticket_view(db, ticket)


# Добавляет сообщение в тикет.
async def add_message(
    db: AsyncSession,
    ticket_id: UUID,
    author_type: SessionSubjectType,
    author_id: UUID,
    body: str,
    company_id: UUID | None = None,
) -> SupportTicketView | None:
    body = body.strip()
    if not body:
        raise ValueError("Сообщение не может быть пустым")
    query = select(SupportTicket).where(SupportTicket.id == ticket_id)
    if company_id is not None:
        query = query.where(SupportTicket.company_id == company_id)
    ticket = await db.scalar(query)
    if ticket is None:
        return None
    if ticket.status == SupportTicketStatus.CLOSED.value:
        raise ValueError("Тикет закрыт")
    now = datetime.now(UTC)
    db.add(
        SupportMessage(
            ticket_id=ticket.id,
            author_type=author_type.value,
            author_id=author_id,
            body=body,
        )
    )
    ticket.updated_at = now
    await write_audit(
        db,
        author_type.value,
        author_id,
        "support.reply",
        "support_ticket",
        ticket.id,
        None,
    )
    if author_type is SessionSubjectType.ADMIN:
        await emit_support_reply(db, ticket, preview=body)
    elif author_type is SessionSubjectType.COMPANY:
        await emit_support_client_message(db, ticket, preview=body)
    await db.commit()
    return await _ticket_view(db, ticket)


# Закрывает тикет (только админ).
async def close_ticket(
    db: AsyncSession,
    ticket_id: UUID,
    admin_id: UUID,
) -> SupportTicketView | None:
    ticket = await db.get(SupportTicket, ticket_id)
    if ticket is None:
        return None
    if ticket.status == SupportTicketStatus.CLOSED.value:
        return await _ticket_view(db, ticket)
    now = datetime.now(UTC)
    ticket.status = SupportTicketStatus.CLOSED.value
    ticket.updated_at = now
    await write_audit(
        db,
        SessionSubjectType.ADMIN.value,
        admin_id,
        "support.close",
        "support_ticket",
        ticket.id,
        None,
    )
    await db.commit()
    logger.info("Закрыт тикет %s", ticket_id)
    return await _ticket_view(db, ticket)

# Закрывает тикет клиентом (только свой OPEN-тикет).
async def close_ticket_by_customer(
    db: AsyncSession,
    company_id: UUID,
    ticket_id: UUID,
    account_id: UUID,
) -> SupportTicketView | None:
    ticket = await db.scalar(
        select(SupportTicket).where(
            SupportTicket.id == ticket_id,
            SupportTicket.company_id == company_id,
        )
    )
    if ticket is None:
        return None
    if ticket.status == SupportTicketStatus.CLOSED.value:
        return await _ticket_view(db, ticket)
    now = datetime.now(UTC)
    ticket.status = SupportTicketStatus.CLOSED.value
    ticket.updated_at = now
    await write_audit(
        db,
        SessionSubjectType.COMPANY.value,
        account_id,
        "support.close",
        "support_ticket",
        ticket.id,
        None,
    )
    await db.commit()
    logger.info("Клиент закрыл тикет %s", ticket_id)
    return await _ticket_view(db, ticket)

