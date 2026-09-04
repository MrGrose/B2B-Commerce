from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.deps import AuthContext, require_admin, require_admin_api, require_company
from b2b_commerce.db import get_session
from b2b_commerce.enums import SessionSubjectType, SupportTicketStatus
from b2b_commerce.http import templates
from b2b_commerce.support.service import (
    SUPPORT_PAGE_SIZE,
    add_message,
    close_ticket,
    close_ticket_by_customer,
    create_ticket,
    get_company_ticket,
    get_ticket,
    list_all_tickets,
    list_company_tickets,
)

html = APIRouter()
api = APIRouter()


# Вычисляет фильтр статуса тикета для списка.
def _support_list_status(status: str | None) -> str | None:
    normalized = (status or SupportTicketStatus.OPEN.value).strip().lower()
    if normalized == "all":
        return None
    if normalized in {SupportTicketStatus.OPEN.value, SupportTicketStatus.CLOSED.value}:
        return normalized
    raise HTTPException(status_code=400, detail="Недопустимый статус тикета")


# Рассчитывает количество страниц для списка тикетов.
def _support_total_pages(total: int) -> int:
    return max(1, (total + SUPPORT_PAGE_SIZE - 1) // SUPPORT_PAGE_SIZE)


class CreateTicketBody(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


class ReplyBody(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


# Сериализует сообщение для JSON.
def _message_json(message) -> dict:
    return {
        "id": str(message.id),
        "author_type": message.author_type,
        "author_id": str(message.author_id),
        "body": message.body,
        "created_at": message.created_at.isoformat(),
    }


# Сериализует тикет для JSON.
def _ticket_json(ticket) -> dict:
    return {
        "id": str(ticket.id),
        "company_id": str(ticket.company_id),
        "company_name": ticket.company_name,
        "status": ticket.status,
        "subject": ticket.subject,
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "messages": [_message_json(message) for message in ticket.messages],
    }


# Список тикетов клиента.
@html.get("/support")
async def support_list(
    request: Request,
    status: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    status_filter = _support_list_status(status or None)
    display_status = (status or SupportTicketStatus.OPEN.value).strip().lower()
    tickets, total = await list_company_tickets(
        db, auth.company_id, page=page, status=status_filter
    )
    total_pages = _support_total_pages(total)
    if page > total_pages:
        page = total_pages
        tickets, total = await list_company_tickets(
            db, auth.company_id, page=page, status=status_filter
        )
    return templates.TemplateResponse(
        request,
        "support/list.html",
        {
            "tickets": tickets,
            "page": page,
            "total_pages": total_pages,
            "status_filter": display_status,
        },
    )


# Форма нового тикета.
@html.get("/support/new")
async def support_new_form(
    request: Request,
    _auth: AuthContext = Depends(require_company),
):
    return templates.TemplateResponse(
        request,
        "support/new.html",
        {"error": None, "form": {}},
    )


# Создаёт тикет клиентом.
@html.post("/support")
async def support_create_submit(
    request: Request,
    subject: str = Form(),
    body: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    subject = subject.strip()
    body = body.strip()
    if not subject or not body:
        return templates.TemplateResponse(
            request,
            "support/new.html",
            {
                "error": "Заполните тему и сообщение",
                "form": {"subject": subject, "body": body},
            },
            status_code=400,
        )
    ticket = await create_ticket(db, auth.company_id, auth.subject_id, subject, body)
    return RedirectResponse(f"/support/{ticket.id}", status_code=303)


# Карточка тикета клиента.
@html.get("/support/{ticket_id}")
async def support_detail(
    request: Request,
    ticket_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    ticket = await get_company_ticket(db, auth.company_id, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return templates.TemplateResponse(
        request,
        "support/detail.html",
        {"ticket": ticket, "error": None},
    )


# Ответ клиента в тикете.
@html.post("/support/{ticket_id}/reply")
async def support_reply_submit(
    request: Request,
    ticket_id: UUID,
    body: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    try:
        ticket = await add_message(
            db,
            ticket_id,
            SessionSubjectType.COMPANY,
            auth.subject_id,
            body,
            company_id=auth.company_id,
        )
    except ValueError as exc:
        ticket = await get_company_ticket(db, auth.company_id, ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="Тикет не найден") from exc
        return templates.TemplateResponse(
            request,
            "support/detail.html",
            {"ticket": ticket, "error": str(exc)},
            status_code=400,
        )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return RedirectResponse(f"/support/{ticket_id}", status_code=303)


# Закрывает тикет клиентом.
@html.post("/support/{ticket_id}/close")
async def support_close_submit(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    ticket = await close_ticket_by_customer(
        db,
        auth.company_id,
        ticket_id,
        auth.subject_id,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return RedirectResponse(f"/support/{ticket_id}", status_code=303)


# Список тикетов в админке.
@html.get("/admin/support")
async def admin_support_list(
    request: Request,
    status: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    status_filter = _support_list_status(status or None)
    display_status = (status or SupportTicketStatus.OPEN.value).strip().lower()
    tickets, total = await list_all_tickets(db, page=page, status=status_filter)
    total_pages = _support_total_pages(total)
    if page > total_pages:
        page = total_pages
        tickets, total = await list_all_tickets(db, page=page, status=status_filter)
    return templates.TemplateResponse(
        request,
        "support/admin/list.html",
        {
            "tickets": tickets,
            "page": page,
            "total_pages": total_pages,
            "status_filter": display_status,
        },
    )


# Карточка тикета в админке.
@html.get("/admin/support/{ticket_id}")
async def admin_support_detail(
    request: Request,
    ticket_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    ticket = await get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return templates.TemplateResponse(
        request,
        "support/admin/detail.html",
        {"ticket": ticket, "error": None, "success": None},
    )


# Ответ админа в тикете.
@html.post("/admin/support/{ticket_id}/reply")
async def admin_support_reply_submit(
    request: Request,
    ticket_id: UUID,
    body: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        ticket = await add_message(
            db,
            ticket_id,
            SessionSubjectType.ADMIN,
            auth.subject_id,
            body,
        )
    except ValueError as exc:
        ticket = await get_ticket(db, ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="Тикет не найден") from exc
        return templates.TemplateResponse(
            request,
            "support/admin/detail.html",
            {"ticket": ticket, "error": str(exc), "success": None},
            status_code=400,
        )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return RedirectResponse(f"/admin/support/{ticket_id}", status_code=303)


# Закрывает тикет из админки.
@html.post("/admin/support/{ticket_id}/close")
async def admin_support_close_submit(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    ticket = await close_ticket(db, ticket_id, auth.subject_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return RedirectResponse(f"/admin/support/{ticket_id}", status_code=303)


# JSON: тикеты клиента.
@api.get("/support")
async def api_support_list(
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    tickets, total = await list_company_tickets(db, auth.company_id, page=page)
    return {
        "items": [_ticket_json(ticket) for ticket in tickets],
        "total": total,
        "page": page,
        "page_size": SUPPORT_PAGE_SIZE,
    }


# JSON: создать тикет.
@api.post("/support")
async def api_create_ticket(
    body: CreateTicketBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    ticket = await create_ticket(
        db,
        auth.company_id,
        auth.subject_id,
        body.subject,
        body.body,
    )
    return _ticket_json(ticket)


# JSON: карточка тикета клиента.
@api.get("/support/{ticket_id}")
async def api_support_detail(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    ticket = await get_company_ticket(db, auth.company_id, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return _ticket_json(ticket)


# JSON: ответ в тикете.
@api.post("/support/{ticket_id}/reply")
async def api_support_reply(
    ticket_id: UUID,
    body: ReplyBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    try:
        ticket = await add_message(
            db,
            ticket_id,
            SessionSubjectType.COMPANY,
            auth.subject_id,
            body.body,
            company_id=auth.company_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return _ticket_json(ticket)


# JSON: закрыть свой тикет.
@api.post("/support/{ticket_id}/close")
async def api_support_close(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    ticket = await close_ticket_by_customer(
        db,
        auth.company_id,
        ticket_id,
        auth.subject_id,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return _ticket_json(ticket)


# JSON: все тикеты (админ).
@api.get("/admin/support")
async def api_admin_support_list(
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    tickets, total = await list_all_tickets(db, page=page)
    return {
        "items": [_ticket_json(ticket) for ticket in tickets],
        "total": total,
        "page": page,
        "page_size": SUPPORT_PAGE_SIZE,
    }


# JSON: карточка тикета (админ).
@api.get("/admin/support/{ticket_id}")
async def api_admin_support_detail(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    ticket = await get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return _ticket_json(ticket)


# JSON: ответ админа.
@api.post("/admin/support/{ticket_id}/reply")
async def api_admin_support_reply(
    ticket_id: UUID,
    body: ReplyBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    try:
        ticket = await add_message(
            db,
            ticket_id,
            SessionSubjectType.ADMIN,
            auth.subject_id,
            body.body,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return _ticket_json(ticket)


# JSON: закрыть тикет.
@api.post("/admin/support/{ticket_id}/close")
async def api_admin_support_close(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    ticket = await close_ticket(db, ticket_id, auth.subject_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return _ticket_json(ticket)
