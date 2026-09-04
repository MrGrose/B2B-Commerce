from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.deps import AuthContext, current_auth, require_company
from b2b_commerce.auth.service import (
    LoginHit,
    RateLimitUnavailable,
    authenticate,
    change_company_password,
    create_session,
    is_login_rate_limited,
    is_register_rate_limited,
    record_register_attempt,
    revoke_token,
    touch_last_login,
)
from b2b_commerce.companies.service import (
    CompanyProfileInput,
    RegistrationInput,
    get_company,
    get_company_profile_metrics,
    register_company,
    update_company_profile,
)
from b2b_commerce.config import Settings, get_settings
from b2b_commerce.db import get_session
from b2b_commerce.enums import CompanyStatus, SessionSubjectType
from b2b_commerce.http import templates

html = APIRouter()
api = APIRouter()


class LoginBody(BaseModel):
    login: str
    password: str


class PasswordBody(BaseModel):
    new_password: str


class RegisterBody(BaseModel):
    login: str
    password: str
    name: str
    legal_name: str
    inn: str
    contact_email: str
    contact_phone: str
    legal_address: str | None = None
    contact_person: str | None = None
    kpp: str | None = None
    delivery_address: str | None = None
    delivery_contact: str | None = None


# Ставит cookie сессии на ответ.
def _set_session_cookie(response, token: str, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.is_prod,
        path="/",
        max_age=settings.session_ttl_hours * 3600,
    )


# HTML-ответ при недоступном Redis rate limit.
def _rate_limit_unavailable_html(request: Request):
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {"error": "Вход временно недоступен. Попробуйте позже."},
        status_code=503,
    )


# Ключ клиента для rate limit по IP.
def _client_key(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


# Куда вести после успешного входа.
def _after_login_path(hit) -> str:
    if hit.subject_type is SessionSubjectType.ADMIN:
        return "/admin"
    if hit.must_change_password:
        return "/profile"
    if hit.company_status == CompanyStatus.PENDING.value:
        return "/pending"
    if hit.company_status == CompanyStatus.REJECTED.value:
        return "/rejected"
    return "/catalog"


# Куда вести клиента по статусу компании.
def _company_home(status: str | None) -> str:
    if status == CompanyStatus.PENDING.value:
        return "/pending"
    if status == CompanyStatus.REJECTED.value:
        return "/rejected"
    return "/catalog"


# Значения формы регистрации для повторного показа.
def _register_form(**fields: str) -> dict[str, str]:
    return fields


# Показывает форму входа.
@html.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "auth/login.html", {"error": None})


# Логинит админа или клиента и ставит cookie сессии.
@html.post("/login")
async def login_submit(
    request: Request,
    login: str = Form(),
    password: str = Form(),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    client_key = _client_key(request)
    try:
        if await is_login_rate_limited(settings, client_key):
            return templates.TemplateResponse(
                request,
                "auth/login.html",
                {"error": "Слишком много попыток. Попробуйте позже."},
                status_code=429,
            )
    except RateLimitUnavailable:
        return _rate_limit_unavailable_html(request)
    result = await authenticate(db, settings, login, password, client_key)
    if result is None:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"error": "Неверный логин или пароль"},
            status_code=401,
        )
    token, hit = result
    response = RedirectResponse(_after_login_path(hit), status_code=303)
    _set_session_cookie(response, token, settings)
    return response


# Показывает форму самостоятельной регистрации.
@html.get("/register")
async def register_page(request: Request):
    return templates.TemplateResponse(
        request,
        "auth/register.html",
        {"error": None, "form": {}},
    )


# Регистрирует компанию в статусе pending и сразу логинит.
@html.post("/register")
async def register_submit(
    request: Request,
    login: str = Form(),
    password: str = Form(),
    name: str = Form(),
    legal_name: str = Form(),
    inn: str = Form(),
    contact_email: str = Form(),
    contact_phone: str = Form(),
    legal_address: str = Form(default=""),
    contact_person: str = Form(default=""),
    kpp: str = Form(default=""),
    delivery_address: str = Form(default=""),
    delivery_contact: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    client_key = _client_key(request)
    try:
        if await is_register_rate_limited(settings, client_key):
            return templates.TemplateResponse(
                request,
                "auth/register.html",
                {"error": "Слишком много попыток. Попробуйте позже.", "form": {}},
                status_code=429,
            )
    except RateLimitUnavailable:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {"error": "Регистрация временно недоступна. Попробуйте позже.", "form": {}},
            status_code=503,
        )
    try:
        await record_register_attempt(settings, client_key)
    except RateLimitUnavailable:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {"error": "Регистрация временно недоступна. Попробуйте позже.", "form": {}},
            status_code=503,
        )
    form = _register_form(
        login=login,
        name=name,
        legal_name=legal_name,
        inn=inn,
        contact_email=contact_email,
        contact_phone=contact_phone,
        legal_address=legal_address,
        contact_person=contact_person,
        kpp=kpp,
        delivery_address=delivery_address,
        delivery_contact=delivery_contact,
    )
    try:
        company = await register_company(
            db,
            RegistrationInput(
                login=login,
                password=password,
                name=name,
                legal_name=legal_name,
                inn=inn,
                contact_email=contact_email,
                contact_phone=contact_phone,
                legal_address=legal_address,
                contact_person=contact_person,
                kpp=kpp,
                delivery_address=delivery_address,
                delivery_contact=delivery_contact,
            ),
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {"error": str(exc), "form": form},
            status_code=400,
        )
    if company is None or company.account is None:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {"error": "Не удалось зарегистрировать компанию", "form": form},
            status_code=500,
        )
    token = await create_session(
        db, settings, SessionSubjectType.COMPANY, company.account.id
    )
    await touch_last_login(
        db,
        LoginHit(SessionSubjectType.COMPANY, company.account.id, "", False, company.status),
    )
    await db.commit()
    response = RedirectResponse("/pending", status_code=303)
    _set_session_cookie(response, token, settings)
    return response


# Экран заявки на рассмотрении.
@html.get("/pending")
async def pending_page(
    request: Request,
    auth: AuthContext = Depends(require_company),
    db: AsyncSession = Depends(get_session),
):
    if auth.company_status == CompanyStatus.ACTIVE.value:
        return RedirectResponse("/catalog", status_code=303)
    if auth.company_status == CompanyStatus.REJECTED.value:
        return RedirectResponse("/rejected", status_code=303)
    company = await get_company(db, auth.company_id)
    return templates.TemplateResponse(
        request,
        "auth/pending.html",
        {"company": company},
    )


# Экран отклонённой заявки.
@html.get("/rejected")
async def rejected_page(
    request: Request,
    auth: AuthContext = Depends(require_company),
    db: AsyncSession = Depends(get_session),
):
    if auth.company_status == CompanyStatus.ACTIVE.value:
        return RedirectResponse("/catalog", status_code=303)
    if auth.company_status == CompanyStatus.PENDING.value:
        return RedirectResponse("/pending", status_code=303)
    company = await get_company(db, auth.company_id)
    return templates.TemplateResponse(
        request,
        "auth/rejected.html",
        {"company": company},
    )


# Редирект со старой ссылки выхода без отзыва сессии.
@html.get("/logout")
async def logout_get():
    return RedirectResponse("/login", status_code=303)


# Завершает сессию и удаляет cookie.
@html.post("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    await revoke_token(db, request.cookies.get(settings.session_cookie))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie, path="/")
    return response


# Собирает контекст страницы профиля клиента.
def _profile_context(
    company,
    error: str | None = None,
    saved: bool = False,
    credential_flash: str | None = None,
    metrics=None,
) -> dict:
    return {
        "company": company,
        "metrics": metrics,
        "error": error,
        "saved": saved,
        "credential_flash": credential_flash,
    }


# Отдаёт profile.html с опциональным Cache-Control: no-store.
async def _profile_response(
    request: Request,
    company,
    db: AsyncSession,
    company_id,
    status_code: int = 200,
    no_store: bool = False,
    **context,
):
    metrics = await get_company_profile_metrics(db, company_id)
    response = templates.TemplateResponse(
        request,
        "auth/profile.html",
        _profile_context(company, metrics=metrics, **context),
        status_code=status_code,
    )
    if no_store:
        response.headers["Cache-Control"] = "no-store"
    return response


# Сохраняет данные компании из профиля клиента.
@html.post("/profile")
async def profile_submit(
    request: Request,
    name: str = Form(),
    legal_name: str = Form(default=""),
    inn: str = Form(default=""),
    contact_email: str = Form(default=""),
    contact_phone: str = Form(default=""),
    legal_address: str = Form(default=""),
    contact_person: str = Form(default=""),
    kpp: str = Form(default=""),
    delivery_address: str = Form(default=""),
    delivery_contact: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    try:
        company = await update_company_profile(
            db,
            auth.company_id,
            auth.subject_id,
            CompanyProfileInput(
                name=name,
                legal_name=legal_name,
                inn=inn,
                contact_email=contact_email,
                contact_phone=contact_phone,
                legal_address=legal_address,
                contact_person=contact_person,
                kpp=kpp,
                delivery_address=delivery_address,
                delivery_contact=delivery_contact,
            ),
        )
    except ValueError as exc:
        company = await get_company(db, auth.company_id)
        if company is None:
            return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
        return await _profile_response(
            request,
            company,
            db,
            auth.company_id,
            error=str(exc),
            status_code=400,
        )
    if company is None:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return RedirectResponse("/profile?saved=1", status_code=303)


# Показывает профиль клиента: компания, логин и смена пароля.
@html.get("/profile")
async def profile_page(
    request: Request,
    auth: AuthContext = Depends(require_company),
    db: AsyncSession = Depends(get_session),
):
    company = await get_company(db, auth.company_id)
    if company is None:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    saved = request.query_params.get("saved") == "1"
    return await _profile_response(request, company, db, auth.company_id, saved=saved)

# Редирект со старого URL на профиль.
@html.get("/change-password")
async def change_password_page(
    _request: Request,
    _auth: AuthContext = Depends(require_company),
):
    return RedirectResponse("/profile", status_code=303)


# Меняет пароль клиента и снимает must_change_password.
@html.post("/change-password")
async def change_password_submit(
    request: Request,
    new_password: str = Form(),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    try:
        await change_company_password(
            db,
            auth.subject_id,
            auth.session_id,
            new_password,
            auth.company_status or "",
        )
    except ValueError as exc:
        company = await get_company(db, auth.company_id)
        if company is None:
            return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
        return await _profile_response(
            request, company, db, auth.company_id, error=str(exc), status_code=400
        )
    company = await get_company(db, auth.company_id)
    if company is None:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return await _profile_response(
        request,
        company,
        db,
        auth.company_id,
        credential_flash=new_password,
        no_store=True,
    )


# JSON-логин для Mini App.
@api.post("/auth/login")
async def api_login(
    request: Request,
    body: LoginBody,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    client_key = _client_key(request)
    try:
        if await is_login_rate_limited(settings, client_key):
            return JSONResponse({"detail": "Слишком много попыток"}, status_code=429)
    except RateLimitUnavailable:
        return JSONResponse({"detail": "Вход временно недоступен"}, status_code=503)
    result = await authenticate(db, settings, body.login, body.password, client_key)
    if result is None:
        return JSONResponse({"detail": "Неверный логин или пароль"}, status_code=401)
    token, hit = result
    response = JSONResponse(
        {
            "subject_type": hit.subject_type.value,
            "must_change_password": hit.must_change_password,
            "company_status": hit.company_status,
        }
    )
    _set_session_cookie(response, token, settings)
    return response


# JSON-регистрация клиента.
@api.post("/auth/register")
async def api_register(
    request: Request,
    body: RegisterBody,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    client_key = _client_key(request)
    try:
        if await is_register_rate_limited(settings, client_key):
            return JSONResponse({"detail": "Слишком много попыток"}, status_code=429)
        await record_register_attempt(settings, client_key)
    except RateLimitUnavailable:
        return JSONResponse({"detail": "Регистрация временно недоступна"}, status_code=503)
    try:
        company = await register_company(
            db,
            RegistrationInput(
                login=body.login,
                password=body.password,
                name=body.name,
                legal_name=body.legal_name,
                inn=body.inn,
                contact_email=body.contact_email,
                contact_phone=body.contact_phone,
                legal_address=body.legal_address,
                contact_person=body.contact_person,
                kpp=body.kpp,
                delivery_address=body.delivery_address,
                delivery_contact=body.delivery_contact,
            ),
        )
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    if company is None or company.account is None:
        return JSONResponse({"detail": "Не удалось зарегистрировать компанию"}, status_code=500)
    token = await create_session(
        db, settings, SessionSubjectType.COMPANY, company.account.id
    )
    await touch_last_login(
        db,
        LoginHit(SessionSubjectType.COMPANY, company.account.id, "", False, company.status),
    )
    await db.commit()
    response = JSONResponse(
        {
            "company_id": str(company.id),
            "status": company.status,
            "login": company.account.login,
        },
        status_code=201,
    )
    _set_session_cookie(response, token, settings)
    return response


# JSON-выход.
@api.post("/auth/logout")
async def api_logout(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    await revoke_token(db, request.cookies.get(settings.session_cookie))
    response = JSONResponse({"ok": True})
    response.delete_cookie(settings.session_cookie, path="/")
    return response


# Текущий субъект сессии.
@api.get("/auth/me")
async def api_me(auth: AuthContext = Depends(current_auth)):
    return {
        "subject_type": auth.subject_type.value,
        "subject_id": str(auth.subject_id),
        "company_id": str(auth.company_id) if auth.company_id else None,
        "must_change_password": auth.must_change_password,
        "company_status": auth.company_status,
    }


# JSON-смена пароля клиента.
@api.post("/auth/change-password")
async def api_change_password(
    body: PasswordBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_company),
):
    try:
        await change_company_password(
            db,
            auth.subject_id,
            auth.session_id,
            body.new_password,
            auth.company_status or "",
        )
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"ok": True}
