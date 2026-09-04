import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from b2b_commerce.admin.router import api as admin_api
from b2b_commerce.admin.router import html as admin_html
from b2b_commerce.audit.router import api as audit_api
from b2b_commerce.audit.router import html as audit_html
from b2b_commerce.auth.deps import ApprovalRequired, Forbidden, LoginRequired, MustChangePassword
from b2b_commerce.auth.router import api as auth_api
from b2b_commerce.auth.router import html as auth_html
from b2b_commerce.cart.router import api as cart_api
from b2b_commerce.cart.router import html as cart_html
from b2b_commerce.catalog.router import api as catalog_api
from b2b_commerce.catalog.router import html as catalog_html
from b2b_commerce.companies.router import api as companies_api
from b2b_commerce.companies.router import html as companies_html
from b2b_commerce.config import get_settings, validate_prod_settings
from b2b_commerce.finance.router import api as finance_api
from b2b_commerce.finance.router import html as finance_html
from b2b_commerce.http import enrich_layout_context, templates
from b2b_commerce.infra.health import router as health_router
from b2b_commerce.infra.security import (
    attach_csrf_cookie,
    ensure_csrf_token,
    is_docs_path,
    is_unsafe_method,
    security_headers,
    validate_csrf,
    validate_origin,
)
from b2b_commerce.inventory.router import api as inventory_api
from b2b_commerce.inventory.router import html as inventory_html
from b2b_commerce.invoices.router import api as invoices_api
from b2b_commerce.invoices.router import html as invoices_html
from b2b_commerce.legal.router import html as legal_html
from b2b_commerce.rapira.router import api as rapira_api
from b2b_commerce.rapira.router import html as rapira_html
from b2b_commerce.support.router import api as support_api
from b2b_commerce.support.router import html as support_html
from b2b_commerce.tables import load_models

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_CACHE_MAX_AGE = 3600

# Поднимает приложение и регистрирует модели.
@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    validate_prod_settings(get_settings())
    load_models()
    logger.info("B2B Commerce запущен")
    yield


_settings = get_settings()
_app_kwargs: dict = {"title": "B2B Commerce", "lifespan": lifespan}
if _settings.is_prod:
    _app_kwargs.update(docs_url=None, redoc_url=None, openapi_url=None)
app = FastAPI(**_app_kwargs)

if _settings.forwarded_allow_ips_list:
    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=_settings.forwarded_allow_ips_list,
    )
if _settings.is_prod and _settings.allowed_host_list:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_settings.allowed_host_list)


class CachedStaticFiles(StaticFiles):
    # Отдаёт /static с коротким browser cache для не-hashed assets.

    # Добавляет Cache-Control к успешным ответам /static.
    async def get_response(self, path: str, scope):
        response: Response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.setdefault(
                "Cache-Control",
                f"public, max-age={STATIC_CACHE_MAX_AGE}",
            )
        return response


app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")


# Возвращает 403 для HTML или JSON.
def _forbidden_response(request: Request, detail: str = "Запрос отклонён") -> JSONResponse:
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": detail}, status_code=403)
    return templates.TemplateResponse(
        request,
        "forbidden.html",
        {},
        status_code=403,
    )


# Подставляет логин и компанию в шапку HTML-страниц.
@app.middleware("http")
async def layout_context_middleware(request: Request, call_next):
    await enrich_layout_context(request)
    return await call_next(request)


# CSRF, Origin, docs guard и security headers.
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    settings = get_settings()
    ensure_csrf_token(request, settings)

    if settings.is_prod and is_docs_path(request.url.path):
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)

    if is_unsafe_method(request.method):
        if not validate_origin(request, settings):
            return _forbidden_response(request)
        if not await validate_csrf(request, settings):
            return _forbidden_response(request)

    response = await call_next(request)
    attach_csrf_cookie(response, request, settings)
    for name, value in security_headers(settings).items():
        response.headers.setdefault(name, value)
    return response


app.include_router(auth_html)
app.include_router(admin_html)
app.include_router(companies_html)
app.include_router(catalog_html)
app.include_router(inventory_html)
app.include_router(cart_html)
app.include_router(invoices_html)
app.include_router(finance_html)
app.include_router(support_html)
app.include_router(legal_html)
app.include_router(rapira_html)
app.include_router(audit_html)

app.include_router(health_router)

app.include_router(auth_api, prefix="/api")
app.include_router(admin_api, prefix="/api")
app.include_router(companies_api, prefix="/api")
app.include_router(catalog_api, prefix="/api")
app.include_router(inventory_api, prefix="/api")
app.include_router(cart_api, prefix="/api")
app.include_router(invoices_api, prefix="/api")
app.include_router(finance_api, prefix="/api")
app.include_router(support_api, prefix="/api")
app.include_router(rapira_api, prefix="/api")
app.include_router(audit_api, prefix="/api")


# Живой процесс.
@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Корень: на логин.
@app.get("/")
async def root():
    return RedirectResponse("/login", status_code=303)


# Редирект на /login для HTML, 401 для JSON.
@app.exception_handler(LoginRequired)
async def login_required_handler(request: Request, exc: LoginRequired):
    if exc.json_mode:
        return JSONResponse({"detail": "Нужна авторизация"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# Редирект клиента на смену пароля.
@app.exception_handler(MustChangePassword)
async def must_change_handler(request: Request, exc: MustChangePassword):
    del request
    if exc.json_mode:
        return JSONResponse({"detail": "Нужно сменить пароль"}, status_code=403)
    return RedirectResponse("/profile", status_code=303)


# Нет доступа к чужому кабинету.
@app.exception_handler(Forbidden)
async def forbidden_handler(request: Request, exc: Forbidden):
    if exc.json_mode:
        return JSONResponse({"detail": "Нет доступа"}, status_code=403)
    return templates.TemplateResponse(
        request,
        "forbidden.html",
        {},
        status_code=403,
    )


# Pending/rejected клиент не видит каталог, корзину и счета.
@app.exception_handler(ApprovalRequired)
async def approval_required_handler(request: Request, exc: ApprovalRequired):
    if exc.json_mode:
        detail = (
            "Заявка отклонена"
            if exc.company_status == "rejected"
            else "Заявка на рассмотрении"
        )
        return JSONResponse({"detail": detail}, status_code=403)
    if exc.company_status == "rejected":
        return RedirectResponse("/rejected", status_code=303)
    return RedirectResponse("/pending", status_code=303)


# HTML 404, JSON как есть.
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api") or exc.status_code == 501:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 404:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    if exc.status_code == 400:
        return templates.TemplateResponse(request, "bad_request.html", {}, status_code=400)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# HTML 500 для страниц, JSON для API; без stack trace в ответе.
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Необработанная ошибка: %s", exc)
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": "Внутренняя ошибка сервера"}, status_code=500)
    return templates.TemplateResponse(request, "server_error.html", {}, status_code=500)
