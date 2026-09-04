from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.deps import (
    AuthContext,
    require_admin,
    require_admin_api,
    require_approved_company,
)
from b2b_commerce.cart.service import get_cart_view
from b2b_commerce.config import Settings, get_settings
from b2b_commerce.db import get_session
from b2b_commerce.enums import InvoiceStatus
from b2b_commerce.http import templates
from b2b_commerce.invoices.pdf import invoice_download_filename, render_invoice_pdf_async
from b2b_commerce.invoices.service import (
    INVOICES_PAGE_SIZE,
    cancel_invoice,
    confirm_payment,
    count_all_invoices,
    count_invoices_by_status,
    create_invoice_from_cart,
    get_company_invoice,
    get_invoice,
    list_addable_invoice_products,
    list_all_invoices,
    list_company_invoices,
    ship_invoice,
    update_invoice_items,
    update_invoice_items_by_company,
)

html = APIRouter()
api = APIRouter()

# Вычисляет количество страниц для списка счетов.
def _invoice_total_pages(total: int) -> int:
    return max(1, (total + INVOICES_PAGE_SIZE - 1) // INVOICES_PAGE_SIZE)


# Генерирует ответ с PDF счёта.
async def _invoice_pdf_response(invoice, *, inline: bool = False) -> Response:
    content = await render_invoice_pdf_async(invoice)
    filename = invoice_download_filename(invoice.number, "pdf")
    disposition = "inline" if inline else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    if inline:
        headers["X-Frame-Options"] = "SAMEORIGIN"
        headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    return Response(
        content=content,
        media_type="application/pdf",
        headers=headers,
    )


# Парсит форму с позициями счёта.
def _parse_invoice_items_form(form) -> tuple[dict[UUID, int], dict[UUID, int]]:
    quantities: dict[UUID, int] = {}
    for key, value in form.items():
        key_str = str(key)
        if not key_str.startswith("qty-"):
            continue
        item_id = UUID(key_str.removeprefix("qty-"))
        quantities[item_id] = int(str(value))
    additions: dict[UUID, int] = {}
    add_product_id = form.get("add_product_id")
    if add_product_id is not None and str(add_product_id).strip():
        additions[UUID(str(add_product_id))] = 1
    return quantities, additions


# Собирает контекст карточки счёта для HTML.
def _htmx_invoice_request(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# Отправляет ответ на запрос изменения позиций счёта.
async def _invoice_items_submit_response(
    request: Request,
    db: AsyncSession,
    invoice,
    page_template: str,
    status_code: int = 200,
    **extra,
):
    context = await _invoice_detail_page_context(db, invoice, **extra)
    if page_template == "invoices/admin/detail.html":
        context["form_action"] = f"/admin/invoices/{invoice.id}/items"
    else:
        context["form_action"] = f"/invoices/{invoice.id}/items"
    if _htmx_invoice_request(request):
        context["sync_summary_total"] = True
        return templates.TemplateResponse(
            request,
            "invoices/_items_edit_slot.html",
            context,
            status_code=200 if status_code == 400 else status_code,
        )
    return templates.TemplateResponse(
        request,
        page_template,
        context,
        status_code=status_code,
    )


# Собирает контекст карточки счёта для HTML.
async def _invoice_detail_page_context(db: AsyncSession, invoice, **extra):
    addable_products: list = []
    if invoice.status == InvoiceStatus.AWAITING_PAYMENT.value:
        addable_products = await list_addable_invoice_products(db)
    return {
        "invoice": invoice,
        "addable_products": addable_products,
        "error": None,
        "success": None,
        **extra,
    }


# Тело запроса для создания счёта.
class CreateInvoiceBody(BaseModel):
    notes: str | None = None


# Сериализует счёт для JSON.
def _invoice_json(view) -> dict:
    return {
        "id": str(view.id),
        "company_id": str(view.company_id),
        "company_name": view.company_name,
        "number": view.number,
        "status": view.status,
        "subtotal": str(view.subtotal),
        "total": str(view.total),
        "notes": view.notes,
        "payment_instructions": view.payment_instructions,
        "created_at": view.created_at.isoformat(),
        "expires_at": view.expires_at.isoformat() if view.expires_at else None,
        "paid_at": view.paid_at.isoformat() if view.paid_at else None,
        "shipped_at": view.shipped_at.isoformat() if view.shipped_at else None,
        "canceled_at": view.canceled_at.isoformat() if view.canceled_at else None,
        "items": [
            {
                "id": str(item.id),
                "product_id": str(item.product_id),
                "product_name": item.product_name,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "line_total": str(item.line_total),
            }
            for item in view.items
        ],
    }


# Список счетов клиента.
@html.get("/invoices")
async def invoices_list(
    request: Request,
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    invoices, total = await list_company_invoices(db, auth.company_id, page=page)
    total_pages = _invoice_total_pages(total)
    if page > total_pages:
        page = total_pages
        invoices, total = await list_company_invoices(db, auth.company_id, page=page)
    return templates.TemplateResponse(
        request,
        "invoices/list.html",
        {
            "invoices": invoices,
            "page": page,
            "total_pages": total_pages,
        },
    )


# Карточка счёта клиента.
@html.get("/invoices/{invoice_id}")
async def invoice_detail(
    request: Request,
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    invoice = await get_company_invoice(db, auth.company_id, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return templates.TemplateResponse(
        request,
        "invoices/detail.html",
        await _invoice_detail_page_context(db, invoice),
    )


# Создаёт счёт из корзины.
@html.post("/invoices")
async def invoice_create_submit(
    request: Request,
    notes: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
    settings: Settings = Depends(get_settings),
):
    try:
        invoice = await create_invoice_from_cart(
            db,
            auth.company_id,
            auth.subject_id,
            settings,
            notes=notes or None,
        )
    except ValueError as exc:
        cart = await get_cart_view(db, auth.company_id)
        return templates.TemplateResponse(
            request,
            "cart/view.html",
            {"cart": cart, "error": str(exc), "success": None},
            status_code=400,
        )
    return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)


# Отменяет счёт клиентом.
@html.post("/invoices/{invoice_id}/cancel")
async def invoice_cancel_submit(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    try:
        invoice = await cancel_invoice(
            db,
            invoice_id,
            "company",
            auth.subject_id,
            company_id=auth.company_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


# Правит позиции awaiting_payment клиентом.
@html.post("/invoices/{invoice_id}/items")
async def invoice_update_items_submit(
    request: Request,
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    form = await request.form()
    try:
        quantities, additions = _parse_invoice_items_form(form)
    except (ValueError, TypeError) as exc:
        invoice = await get_company_invoice(db, auth.company_id, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Счёт не найден") from exc
        return await _invoice_items_submit_response(
            request,
            db,
            invoice,
            page_template="invoices/detail.html",
            status_code=400,
            error="Некорректные данные позиций",
        )
    try:
        invoice = await update_invoice_items_by_company(
            db,
            invoice_id,
            auth.company_id,
            auth.subject_id,
            quantities=quantities or None,
            additions=additions or None,
        )
    except ValueError as exc:
        await db.rollback()
        invoice = await get_company_invoice(db, auth.company_id, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Счёт не найден") from exc
        return await _invoice_items_submit_response(
            request,
            db,
            invoice,
            page_template="invoices/detail.html",
            status_code=400,
            error=str(exc),
        )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return await _invoice_items_submit_response(
        request,
        db,
        invoice,
        page_template="invoices/detail.html",
        success="Счёт обновлён",
    )


# Вычисляет дату создания счёта для фильтрации.
def _invoice_created_since(created: str) -> datetime | None:
    if created.strip() == "24h":
        return datetime.now(UTC) - timedelta(hours=24)
    if created.strip():
        raise HTTPException(status_code=400, detail="Недопустимый фильтр по дате создания")
    return None

_INVOICE_LIST_STATUSES = frozenset(
    {
        InvoiceStatus.AWAITING_PAYMENT.value,
        InvoiceStatus.PAID.value,
        InvoiceStatus.SHIPPED.value,
        InvoiceStatus.CANCELED.value,
        InvoiceStatus.EXPIRED.value,
    }
)


@html.get("/admin/invoices")
async def admin_invoices(
    request: Request,
    status: str = Query(default=""),
    created: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    status_filter = status.strip() or None
    if status_filter is not None and status_filter not in _INVOICE_LIST_STATUSES:
        raise HTTPException(status_code=400, detail="Недопустимый статус счёта")
    created_since = _invoice_created_since(created)
    invoices, total = await list_all_invoices(
        db,
        status=status_filter,
        page=page,
        created_since=created_since,
    )
    total_pages = _invoice_total_pages(total)
    if page > total_pages:
        page = total_pages
        invoices, total = await list_all_invoices(
            db,
            status=status_filter,
            page=page,
            created_since=created_since,
        )
    return templates.TemplateResponse(
        request,
        "invoices/admin/list.html",
        {
            "invoices": invoices,
            "status_filter": status_filter or "",
            "created_filter": created.strip(),
            "page": page,
            "total_pages": total_pages,
            "status_counts": await count_invoices_by_status(
                db, created_since=created_since
            ),
            "last_24h_count": await count_all_invoices(
                db, created_since=datetime.now(UTC) - timedelta(hours=24)
            ),
        },
    )


# Карточка счёта в админке.
@html.get("/admin/invoices/{invoice_id}")
async def admin_invoice_detail(
    request: Request,
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    invoice = await get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return templates.TemplateResponse(
        request,
        "invoices/admin/detail.html",
        await _invoice_detail_page_context(db, invoice),
    )



# Правит позиции awaiting_payment из админки.
@html.post("/admin/invoices/{invoice_id}/items")
async def admin_update_items_submit(
    request: Request,
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    form = await request.form()
    try:
        quantities, additions = _parse_invoice_items_form(form)
    except (ValueError, TypeError) as exc:
        invoice = await get_invoice(db, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Счёт не найден") from exc
        return await _invoice_items_submit_response(
            request,
            db,
            invoice,
            page_template="invoices/admin/detail.html",
            status_code=400,
            error="Некорректные данные позиций",
        )
    try:
        invoice = await update_invoice_items(
            db,
            invoice_id,
            auth.subject_id,
            quantities,
            additions=additions or None,
        )
    except ValueError as exc:
        await db.rollback()
        invoice = await get_invoice(db, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Счёт не найден") from exc
        return await _invoice_items_submit_response(
            request,
            db,
            invoice,
            page_template="invoices/admin/detail.html",
            status_code=400,
            error=str(exc),
        )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return await _invoice_items_submit_response(
        request,
        db,
        invoice,
        page_template="invoices/admin/detail.html",
        success="Позиции обновлены",
    )


# Подтверждает оплату из админки.
@html.post("/admin/invoices/{invoice_id}/confirm-payment")
async def admin_confirm_payment_submit(
    request: Request,
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        invoice = await confirm_payment(db, invoice_id, auth.subject_id)
    except ValueError as exc:
        invoice = await get_invoice(db, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Счёт не найден") from exc
        return templates.TemplateResponse(
            request,
            "invoices/admin/detail.html",
            await _invoice_detail_page_context(db, invoice, error=str(exc)),
            status_code=400,
        )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return templates.TemplateResponse(
        request,
        "invoices/admin/detail.html",
        {"invoice": invoice, "error": None, "success": "Оплата подтверждена"},
    )


# Отгружает счёт из админки.
@html.post("/admin/invoices/{invoice_id}/ship")
async def admin_ship_submit(
    request: Request,
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        invoice = await ship_invoice(db, invoice_id, auth.subject_id)
    except ValueError as exc:
        invoice = await get_invoice(db, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Счёт не найден") from exc
        return templates.TemplateResponse(
            request,
            "invoices/admin/detail.html",
            await _invoice_detail_page_context(db, invoice, error=str(exc)),
            status_code=400,
        )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return templates.TemplateResponse(
        request,
        "invoices/admin/detail.html",
        await _invoice_detail_page_context(
            db,
            invoice,
            success="Отгрузка выполнена",
        ),
    )


# Отменяет счёт из админки.
@html.post("/admin/invoices/{invoice_id}/cancel")
async def admin_cancel_submit(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        invoice = await cancel_invoice(db, invoice_id, "admin", auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return RedirectResponse(f"/admin/invoices/{invoice_id}", status_code=303)



@html.get("/invoices/{invoice_id}/preview.pdf")
async def invoice_preview_pdf(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    invoice = await get_company_invoice(db, auth.company_id, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return await _invoice_pdf_response(invoice, inline=True)


@html.get("/invoices/{invoice_id}/download.pdf")
async def invoice_download_pdf(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    invoice = await get_company_invoice(db, auth.company_id, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return await _invoice_pdf_response(invoice)


# Отдаёт PDF счёта админу.
@html.get("/admin/invoices/{invoice_id}/preview.pdf")
async def admin_invoice_preview_pdf(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    invoice = await get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return await _invoice_pdf_response(invoice, inline=True)


@html.get("/admin/invoices/{invoice_id}/download.pdf")
async def admin_invoice_download_pdf(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    invoice = await get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return await _invoice_pdf_response(invoice)


# JSON: список счетов клиента.
@api.get("/invoices")
async def api_invoices(
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    invoices, total = await list_company_invoices(db, auth.company_id, page=page)
    return {
        "items": [_invoice_json(invoice) for invoice in invoices],
        "total": total,
        "page": page,
        "page_size": INVOICES_PAGE_SIZE,
    }


# JSON: карточка счёта клиента.
@api.get("/invoices/{invoice_id}")
async def api_invoice(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    invoice = await get_company_invoice(db, auth.company_id, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return _invoice_json(invoice)


# JSON: создать счёт из корзины.
@api.post("/invoices")
async def api_create_invoice(
    body: CreateInvoiceBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
    settings: Settings = Depends(get_settings),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        invoice = await create_invoice_from_cart(
            db,
            auth.company_id,
            auth.subject_id,
            settings,
            idempotency_key=idempotency_key,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _invoice_json(invoice)


# JSON: отмена счёта клиентом.
@api.post("/invoices/{invoice_id}/cancel")
async def api_cancel_invoice(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    try:
        invoice = await cancel_invoice(
            db,
            invoice_id,
            "company",
            auth.subject_id,
            company_id=auth.company_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return _invoice_json(invoice)


# JSON: список счетов админа.
@api.get("/admin/invoices")
async def api_admin_invoices(
    status: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    status_filter = status.strip() or None
    if status_filter is not None and status_filter not in _INVOICE_LIST_STATUSES:
        raise HTTPException(status_code=400, detail="Недопустимый статус счёта")
    invoices, total = await list_all_invoices(db, status=status_filter, page=page)
    return {
        "items": [_invoice_json(invoice) for invoice in invoices],
        "total": total,
        "page": page,
        "page_size": INVOICES_PAGE_SIZE,
    }


# JSON: подтверждение оплаты.
@api.post("/admin/invoices/{invoice_id}/confirm-payment")
async def api_confirm_payment(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        invoice = await confirm_payment(
            db,
            invoice_id,
            auth.subject_id,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return _invoice_json(invoice)


# JSON: отгрузка.
@api.post("/admin/invoices/{invoice_id}/ship")
async def api_ship(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    try:
        invoice = await ship_invoice(db, invoice_id, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return _invoice_json(invoice)


# JSON: отмена счёта админом.
@api.post("/admin/invoices/{invoice_id}/cancel")
async def api_admin_cancel(
    invoice_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    try:
        invoice = await cancel_invoice(db, invoice_id, "admin", auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if invoice is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return _invoice_json(invoice)
