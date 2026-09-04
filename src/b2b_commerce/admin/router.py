from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.admin.service import LOW_STOCK_THRESHOLD, get_admin_dashboard
from b2b_commerce.auth.deps import AuthContext, require_admin
from b2b_commerce.auth.service import (
    create_admin_user,
    list_admin_users,
    set_admin_active,
)
from b2b_commerce.catalog.service import (
    enqueue_product_repricing,
    list_brand_rows,
    list_category_rows,
    update_category_margin,
)
from b2b_commerce.db import get_session
from b2b_commerce.http import admin_settings_url, templates

html = APIRouter()
api = APIRouter()

SETTINGS_TABS = frozenset({"catalog", "repricing", "admins"})


# Дашборд админки.
@html.get("/admin")
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    dashboard_view = await get_admin_dashboard(db)
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "dashboard": dashboard_view,
            "low_stock_threshold": LOW_STOCK_THRESHOLD,
        },
    )


# Настройки админки.
@html.get("/admin/settings")
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
    tab: str = Query(default="catalog"),
    open: str = Query(default=""),
    success: str = Query(default=""),
    error: str = Query(default=""),
):
    active_tab = tab if tab in SETTINGS_TABS else "catalog"
    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {
            "tab": active_tab,
            "open": open,
            "categories": await list_category_rows(db),
            "brands": await list_brand_rows(db),
            "admins": await list_admin_users(db),
            "current_admin_id": auth.subject_id,
            "success": success,
            "error": error,
        },
    )


# Обрабатывает запрос на изменение маржи категории.
@html.post("/admin/settings/categories/{category_id}/margin")
async def settings_category_margin_submit(
    category_id: UUID,
    margin_percent: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    parsed: Decimal | None
    raw = margin_percent.strip().replace(",", ".")
    if not raw:
        parsed = None
    else:
        try:
            parsed = Decimal(raw)
        except InvalidOperation:
            return RedirectResponse(
                admin_settings_url(
                    "catalog",
                    open_section="categories",
                    error="Некорректная маржа",
                ),
                status_code=303,
            )
    category = await update_category_margin(db, category_id, parsed, auth.subject_id)
    if category is None:
        return RedirectResponse(
            admin_settings_url("catalog", open_section="categories", error="Категория не найдена"),
            status_code=303,
        )
    return RedirectResponse(
        admin_settings_url("catalog", open_section="categories"),
        status_code=303,
    )


# Обрабатывает запрос на переоценку товаров.
@html.post("/admin/settings/reprice")
async def settings_reprice_submit(
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    await enqueue_product_repricing(auth.subject_id)
    return RedirectResponse(
        admin_settings_url("repricing", success="Переоценка поставлена в очередь"),
        status_code=303,
    )

# Обрабатывает запрос на создание администратора.
@html.post("/admin/settings/admins")
async def settings_admin_create_submit(
    login: str = Form(),
    password: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        await create_admin_user(db, login, password, auth.subject_id)
    except ValueError as exc:
        return RedirectResponse(
            admin_settings_url("admins", error=str(exc)),
            status_code=303,
        )
    return RedirectResponse(
        admin_settings_url("admins", success="Администратор добавлен"),
        status_code=303,
    )


# Обрабатывает запрос на отключение администратора.
@html.post("/admin/settings/admins/{admin_id}/deactivate")
async def settings_admin_deactivate_submit(
    admin_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        await set_admin_active(db, admin_id, is_active=False, actor_id=auth.subject_id)
    except ValueError as exc:
        return RedirectResponse(
            admin_settings_url("admins", error=str(exc)),
            status_code=303,
        )
    return RedirectResponse(
        admin_settings_url("admins", success="Администратор отключён"),
        status_code=303,
    )
