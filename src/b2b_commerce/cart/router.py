from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.deps import AuthContext, require_approved_company
from b2b_commerce.cart.redirects import redirect_with_message, safe_catalog_redirect
from b2b_commerce.cart.service import (
    add_cart_item_delta,
    get_cart_quantities,
    get_cart_view,
    remove_cart_item,
    upsert_cart_item,
)
from b2b_commerce.db import get_session
from b2b_commerce.http import templates
from b2b_commerce.inventory.service import get_availability

html = APIRouter()
api = APIRouter()


# Генерирует фрагмент HTML для корзины.
async def _htmx_catalog_cart_slot(
    request: Request,
    db: AsyncSession,
    company_id: UUID,
    product_id: UUID,
    next_url: str,
):
    quantities = await get_cart_quantities(db, company_id)
    return templates.TemplateResponse(
        request,
        "catalog/cart_controls_fragment.html",
        {
            "product_id": product_id,
            "quantity": quantities.get(product_id, 0),
            "availability": await get_availability(db, product_id),
            "next_url": next_url,
            "cart_items_count": sum(quantities.values()),
        },
    )


# Проверяет, является ли запрос HTMX.
def _htmx_cart_request(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


class CartItemBody(BaseModel):
    product_id: UUID
    quantity: int = Field(gt=0)


# Корзина компании.
@html.get("/cart")
async def cart_page(
    request: Request,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    cart = await get_cart_view(db, auth.company_id)
    return templates.TemplateResponse(
        request,
        "cart/view.html",
        {"cart": cart, "error": None, "success": None},
    )


# Добавляет или обновляет позицию из HTML-формы.
@html.post("/cart/items")
async def cart_upsert_submit(
    request: Request,
    product_id: UUID = Form(),
    quantity: int = Form(),
    mode: str = Form(default="set"),
    next_url: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    back = safe_catalog_redirect(next_url, request.headers.get("referer"))
    try:
        if mode == "add":
            await add_cart_item_delta(db, auth.company_id, product_id, quantity)
        else:
            await upsert_cart_item(db, auth.company_id, product_id, quantity)
    except ValueError as exc:
        if _htmx_cart_request(request):
            resolved = back or next_url.strip() or "/catalog"
            response = await _htmx_catalog_cart_slot(
                request, db, auth.company_id, product_id, resolved
            )
            response.status_code = 422
            return response
        if back:
            return RedirectResponse(
                redirect_with_message(back, "cart_error", str(exc)),
                status_code=303,
            )
        cart = await get_cart_view(db, auth.company_id)
        return templates.TemplateResponse(
            request,
            "cart/view.html",
            {"cart": cart, "error": str(exc), "success": None},
            status_code=400,
        )
    if _htmx_cart_request(request):
        resolved = back or next_url.strip() or "/catalog"
        return await _htmx_catalog_cart_slot(
            request, db, auth.company_id, product_id, resolved
        )
    if back:
        return RedirectResponse(back, status_code=303)
    cart = await get_cart_view(db, auth.company_id)
    return templates.TemplateResponse(
        request,
        "cart/view.html",
        {"cart": cart, "error": None, "success": "Корзина обновлена"},
    )


# Удаляет позицию из корзины.
@html.post("/cart/items/{product_id}/remove")
async def cart_remove_submit(
    request: Request,
    product_id: UUID,
    next_url: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    await remove_cart_item(db, auth.company_id, product_id)
    back = safe_catalog_redirect(next_url, request.headers.get("referer"))
    if _htmx_cart_request(request):
        resolved = back or next_url.strip() or "/catalog"
        return await _htmx_catalog_cart_slot(
            request, db, auth.company_id, product_id, resolved
        )
    if back:
        return RedirectResponse(back, status_code=303)
    return RedirectResponse("/cart", status_code=303)


# JSON: состав корзины.
@api.get("/cart")
async def api_cart(
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    cart = await get_cart_view(db, auth.company_id)
    return {
        "items": [
            {
                "product_id": str(item.product_id),
                "name": item.name,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "line_total": str(item.line_total),
                "availability": item.availability,
            }
            for item in cart.items
        ],
        "subtotal": str(cart.subtotal),
    }


# JSON: добавить/изменить позицию.
@api.put("/cart/items")
async def api_put_item(
    body: CartItemBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    try:
        cart = await upsert_cart_item(
            db,
            auth.company_id,
            body.product_id,
            body.quantity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "items": [
            {
                "product_id": str(item.product_id),
                "quantity": item.quantity,
            }
            for item in cart.items
        ],
        "subtotal": str(cart.subtotal),
    }


# JSON: убрать позицию.
@api.delete("/cart/items/{product_id}")
async def api_delete_item(
    product_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_approved_company),
):
    cart = await remove_cart_item(db, auth.company_id, product_id)
    return {"items": len(cart.items), "subtotal": str(cart.subtotal)}
