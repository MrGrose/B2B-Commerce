import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup
from sqlalchemy import select

from b2b_commerce.auth.models import AdminUser, Session
from b2b_commerce.auth.service import hash_token
from b2b_commerce.cart.service import get_cart_items_count
from b2b_commerce.companies.models import Company, CompanyAccount
from b2b_commerce.config import get_settings
from b2b_commerce.db import SessionLocal
from b2b_commerce.enums import SessionSubjectType
from b2b_commerce.invoices.service import count_invoices_created_since
from b2b_commerce.support.service import count_company_support_alerts, count_open_tickets

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_PRODUCT_TONES = ("lime", "blue", "orange", "yellow", "navy", "green")


# Вычисляет индекс цветового тона для карточки товара по id.
def _product_tone_index(product_id: object) -> int:
    if product_id is None:
        key = 0
    elif isinstance(product_id, bool):
        key = int(product_id)
    elif isinstance(product_id, int):
        key = product_id
    else:
        text = str(product_id).replace("-", "")
        try:
            key = int(text, 16)
        except ValueError:
            key = abs(hash(str(product_id)))
    return key % len(_PRODUCT_TONES)


# Возвращает цветовой тон карточки товара по id (int или UUID).
def product_tone(product_id: object = None) -> str:
    return _PRODUCT_TONES[_product_tone_index(product_id)]


def format_money(value: Decimal | int | float | str | None) -> str:
    if value is None or value == "":
        return "—"
    amount = Decimal(value)
    return f"{amount:,.0f}".replace(",", " ") + " ₽"


# Форматирует курс (USD/RUB): до 2 знаков после запятой, без округления вверх.
def format_rate(value: Decimal | int | float | str | None) -> str:
    if value is None or value == "":
        return "—"
    amount = Decimal(value)
    truncated = amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if truncated == truncated.to_integral_value():
        text = f"{int(truncated):,}".replace(",", " ")
    else:
        integer, fraction = f"{truncated:.2f}".split(".")
        text = f"{int(integer):,}".replace(",", " ") + "," + fraction
    return text + " ₽"


# Приводит datetime к ISO-строке для атрибута <time datetime>.
def datetime_iso(value: datetime | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.isoformat()
    return str(value).strip()


# Рендерит <time> с UTC в атрибуте; текст подставит JS в TZ браузера.
def local_datetime(value: datetime | str | None, date_only: bool = False) -> Markup:
    if value is None or value == "":
        return Markup("—")
    iso = datetime_iso(value)
    css = "local-datetime local-date" if date_only else "local-datetime"
    return Markup(f'<time class="{css}" datetime="{iso}">{iso}</time>')


templates.env.filters["product_tone"] = product_tone
templates.env.filters["money"] = format_money
templates.env.filters["rate"] = format_rate
templates.env.filters["local_datetime"] = local_datetime


# Форматирует количество для множественного числа на русском языке.
def plural_ru(n: int, one: str, few: str, many: str) -> str:
    value = abs(int(n)) % 100
    if 10 < value < 20:
        return many
    value %= 10
    if value == 1:
        return one
    if value in (2, 3, 4):
        return few
    return many


# Форматирует количество для множественного числа на русском языке.
def plural_count(n: int, one: str, few: str, many: str) -> str:
    return f"{int(n)} {plural_ru(n, one, few, many)}"


templates.env.filters["plural_ru"] = plural_ru
templates.env.filters["plural_count"] = plural_count


# Рендерит hidden input с CSRF-токеном.
@pass_context
def csrf_input_field(context) -> Markup:
    request = context.get("request")
    token = ""
    if request is not None:
        token = getattr(request.state, "csrf_token", "")
    return Markup(f'<input type="hidden" name="csrf_token" value="{token}">')


templates.env.globals["csrf_input"] = csrf_input_field


# Собирает URL каталога с поиском, фильтрами и страницей.
def catalog_url(
    q: str = "",
    category_id: UUID | str = "",
    brand_id: UUID | str = "",
    model_year: str | int = "",
    sort: str = "",
    page: int | None = None,
) -> str:
    params: dict[str, str | int] = {}
    if q:
        params["q"] = q
    if category_id:
        params["category_id"] = str(category_id)
    if brand_id:
        params["brand_id"] = str(brand_id)
    if model_year:
        params["model_year"] = str(model_year)
    if sort:
        params["sort"] = sort
    if page and page > 1:
        params["page"] = page
    query = urlencode(params)
    return f"/catalog?{query}" if query else "/catalog"


templates.env.globals["catalog_url"] = catalog_url


# Собирает URL списка товаров в админке с фильтрами и страницей.
def admin_products_url(
    q: str = "",
    brand_id: str = "",
    category_id: str = "",
    model_year: str = "",
    status: str = "",
    sort: str = "date_desc",
    page: int | None = None,
) -> str:
    params: dict[str, str | int] = {}
    if q:
        params["q"] = q
    if brand_id:
        params["brand_id"] = brand_id
    if category_id:
        params["category_id"] = category_id
    if model_year:
        params["model_year"] = model_year
    if status:
        params["status"] = status
    if sort and sort != "date_desc":
        params["sort"] = sort
    if page and page > 1:
        params["page"] = page
    query = urlencode(params)
    return f"/admin/products?{query}" if query else "/admin/products"


templates.env.globals["admin_products_url"] = admin_products_url


# Собирает URL списка компаний в админке с фильтрами и страницей.
def admin_companies_url(status: str = "", q: str = "", page: int | None = None) -> str:
    params: dict[str, str] = {}
    if status:
        params["status"] = status
    if q:
        params["q"] = q
    if page and page > 1:
        params["page"] = str(page)
    query = urlencode(params)
    return f"/admin/companies?{query}" if query else "/admin/companies"


templates.env.globals["admin_companies_url"] = admin_companies_url


# Собирает URL списка счетов клиента.
def customer_invoices_url(page: int | None = None) -> str:
    if page and page > 1:
        return f"/invoices?page={page}"
    return "/invoices"


templates.env.globals["customer_invoices_url"] = customer_invoices_url


# Собирает URL списка счетов в админке.
def admin_invoices_url(
    status: str = "",
    page: int | None = None,
    created: str = "",
) -> str:
    params: dict[str, str] = {}
    if status:
        params["status"] = status
    if created:
        params["created"] = created
    if page and page > 1:
        params["page"] = str(page)
    query = urlencode(params)
    return f"/admin/invoices?{query}" if query else "/admin/invoices"


templates.env.globals["admin_invoices_url"] = admin_invoices_url


# Собирает URL списка тикетов клиента.
def customer_support_url(page: int | None = None, status: str = "") -> str:
    params: dict[str, str] = {}
    if status:
        params["status"] = status
    if page and page > 1:
        params["page"] = str(page)
    query = urlencode(params)
    return f"/support?{query}" if query else "/support"


templates.env.globals["customer_support_url"] = customer_support_url


# Собирает URL списка тикетов в админке.
def admin_support_url(page: int | None = None, status: str = "") -> str:
    params: dict[str, str] = {}
    if status:
        params["status"] = status
    if page and page > 1:
        params["page"] = str(page)
    query = urlencode(params)
    return f"/admin/support?{query}" if query else "/admin/support"


templates.env.globals["admin_support_url"] = admin_support_url


# Собирает URL справочника юрлиц поставщика.
def admin_billing_entities_url(page: int | None = None) -> str:
    if page and page > 1:
        return f"/admin/billing-entities?page={page}"
    return "/admin/billing-entities"


templates.env.globals["admin_billing_entities_url"] = admin_billing_entities_url

# Собирает URL журнала событий в админке.
def admin_audit_url(page: int | None = None) -> str:
    if page and page > 1:
        return f"/admin/audit?page={page}"
    return "/admin/audit"


templates.env.globals["admin_audit_url"] = admin_audit_url


# Собирает URL страницы настроек админки.
def admin_settings_url(
    tab: str = "catalog",
    *,
    open_section: str = "",
    success: str = "",
    error: str = "",
) -> str:
    params: dict[str, str] = {"tab": tab}
    if open_section:
        params["open"] = open_section
    if success:
        params["success"] = success
    if error:
        params["error"] = error
    return f"/admin/settings?{urlencode(params)}"


templates.env.globals["admin_settings_url"] = admin_settings_url


# Возвращает инициалы для аватара из логина.
def user_initials(login: str) -> str:
    login = login.strip()
    if not login:
        return "?"
    parts = [part for part in re.split(r"[^a-zA-Z0-9]+", login) if part]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return login[:2].upper()


# Подставляет в request.state данные для шапки клиента или админа.
async def enrich_layout_context(request: Request) -> None:
    request.state.layout_user_login = None
    request.state.layout_company_name = None
    request.state.layout_company_legal_name = None
    request.state.layout_user_initials = None
    request.state.layout_is_admin = False
    request.state.layout_cart_items_count = 0
    request.state.layout_company_status = None
    request.state.layout_support_alert_count = 0
    request.state.layout_support_open_count = 0
    request.state.layout_invoice_recent_count = 0

    path = request.url.path
    if path.startswith(("/static", "/api", "/media")) or path in {"/login", "/register", "/logout"}:
        return

    settings = get_settings()
    token = request.cookies.get(settings.session_cookie)
    if not token:
        return

    async with SessionLocal() as db:
        row = await db.scalar(
            select(Session).where(
                Session.token_hash == hash_token(token),
                Session.revoked_at.is_(None),
                Session.expires_at > datetime.now(UTC),
            )
        )
        if row is None:
            return

        subject_type = SessionSubjectType(row.subject_type)
        if subject_type is SessionSubjectType.COMPANY:
            account = await db.get(CompanyAccount, row.subject_id)
            if account is None:
                return
            company = await db.get(Company, account.company_id)
            request.state.layout_user_login = account.login
            request.state.layout_company_name = company.name if company else None
            request.state.layout_company_legal_name = company.legal_name if company else None
            request.state.layout_company_status = company.status if company else None
            request.state.layout_user_initials = user_initials(account.login)
            request.state.layout_cart_items_count = await get_cart_items_count(
                db, account.company_id
            )
            request.state.layout_support_alert_count = await count_company_support_alerts(
                db, account.company_id
            )
            return

        if subject_type is SessionSubjectType.ADMIN:
            admin = await db.get(AdminUser, row.subject_id)
            if admin is None:
                return
            request.state.layout_user_login = admin.login
            request.state.layout_company_name = "Администратор"
            request.state.layout_user_initials = user_initials(admin.login)
            request.state.layout_is_admin = True
            request.state.layout_support_open_count = await count_open_tickets(db)
            request.state.layout_invoice_recent_count = await count_invoices_created_since(
                db, datetime.now(UTC) - timedelta(hours=24)
            )


# Отдаёт HTML-заглушку раздела.
def stub_page(request: Request, title: str, layout: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "stub.html",
        {"title": title, "layout": layout},
    )


# Ответ для ещё не собранных JSON-методов.
def not_implemented() -> None:
    raise HTTPException(status_code=501, detail="Ещё не реализовано")
