from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.deps import AuthContext, require_admin, require_admin_api
from b2b_commerce.companies.service import (
    BILLING_ENTITIES_PAGE_SIZE,
    COMPANIES_PAGE_SIZE,
    BillingEntityInput,
    CompanyInput,
    CompanyProfileInput,
    activate_company,
    approve_company,
    count_companies,
    create_billing_entity,
    create_company,
    deactivate_company,
    get_billing_entity,
    get_company,
    list_billing_entities,
    list_companies,
    reject_company,
    reset_company_password,
    update_billing_entity,
    update_company_admin,
)
from b2b_commerce.db import get_session
from b2b_commerce.enums import CompanyStatus
from b2b_commerce.http import templates
from b2b_commerce.invoices.service import list_company_invoices

html = APIRouter()
api = APIRouter()


# Рассчитывает количество страниц для списка юрлиц.
def _billing_total_pages(total: int) -> int:
    return max(1, (total + BILLING_ENTITIES_PAGE_SIZE - 1) // BILLING_ENTITIES_PAGE_SIZE)



class CreateCompanyBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = None
    inn: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    login: str | None = None
    billing_entity_id: UUID | None = None


class UpdateCompanyBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = None
    inn: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    legal_address: str | None = None
    contact_person: str | None = None
    kpp: str | None = None
    delivery_address: str | None = None
    delivery_contact: str | None = None
    billing_entity_id: UUID | None = None


class CreateBillingEntityBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str = Field(min_length=1, max_length=200)
    inn: str = Field(min_length=10, max_length=12)
    kpp: str | None = None
    legal_address: str | None = None
    bank_name: str | None = None
    bik: str | None = None
    bank_account: str | None = None
    corr_account: str | None = None


class RejectCompanyBody(BaseModel):
    reason: str | None = None


# Сериализует учётку компании для JSON.
def _account_json(account) -> dict | None:
    if account is None:
        return None
    return {
        "id": str(account.id),
        "login": account.login,
        "is_active": account.is_active,
        "must_change_password": account.must_change_password,
        "last_login_at": account.last_login_at.isoformat() if account.last_login_at else None,
    }


# Сериализует компанию для JSON.
def _company_json(company) -> dict:
    return {
        "id": str(company.id),
        "name": company.name,
        "legal_name": company.legal_name,
        "inn": company.inn,
        "contact_email": company.contact_email,
        "contact_phone": company.contact_phone,
        "legal_address": company.legal_address,
        "contact_person": company.contact_person,
        "kpp": company.kpp,
        "delivery_address": company.delivery_address,
        "delivery_contact": company.delivery_contact,
        "rejection_reason": company.rejection_reason,
        "billing_entity_id": str(company.billing_entity_id) if company.billing_entity_id else None,
        "billing_entity_name": company.billing_entity_name,
        "status": company.status,
        "created_at": company.created_at.isoformat() if company.created_at else None,
        "account": _account_json(company.account),
    }


# Собирает BillingEntityInput из полей формы или API.
def _billing_input(
    name: str,
    legal_name: str,
    inn: str,
    kpp: str | None,
    legal_address: str | None,
    bank_name: str | None,
    bik: str | None,
    bank_account: str | None,
    corr_account: str | None,
) -> BillingEntityInput:
    return BillingEntityInput(
        name=name,
        legal_name=legal_name,
        inn=inn,
        kpp=kpp or None,
        legal_address=legal_address or None,
        bank_name=bank_name or None,
        bik=bik or None,
        bank_account=bank_account or None,
        corr_account=corr_account or None,
    )


# Сериализует юрлицо поставщика для JSON.
def _billing_json(entity) -> dict:
    return {
        "id": str(entity.id),
        "name": entity.name,
        "legal_name": entity.legal_name,
        "inn": entity.inn,
        "kpp": entity.kpp,
        "legal_address": entity.legal_address,
        "bank_name": entity.bank_name,
        "bik": entity.bik,
        "bank_account": entity.bank_account,
        "corr_account": entity.corr_account,
    }


# Нормализует фильтр статуса компаний.
def _status_filter(value: str | None) -> str | None:
    if not value:
        return None
    allowed = {item.value for item in CompanyStatus}
    if value not in allowed:
        return None
    return value


# Собирает URL списка компаний с фильтрами.
def _companies_list_href(status: str, q: str, page: int) -> str:
    params: dict[str, str] = {}
    if status:
        params["status"] = status
    if q:
        params["q"] = q
    if page > 1:
        params["page"] = str(page)
    query = urlencode(params)
    return f"/admin/companies?{query}" if query else "/admin/companies"


# Читает optional UUID из строки формы.
def _optional_uuid(value: str) -> UUID | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return UUID(text)
    except ValueError as exc:
        raise ValueError("Некорректный идентификатор юрлица") from exc


# Собирает профиль из полей формы.
def _profile_input(
    name: str,
    legal_name: str,
    inn: str,
    contact_email: str,
    contact_phone: str,
    legal_address: str,
    contact_person: str,
    kpp: str,
    delivery_address: str,
    delivery_contact: str,
) -> CompanyProfileInput:
    return CompanyProfileInput(
        name=name,
        legal_name=legal_name or None,
        inn=inn or None,
        contact_email=contact_email or None,
        contact_phone=contact_phone or None,
        legal_address=legal_address or None,
        contact_person=contact_person or None,
        kpp=kpp or None,
        delivery_address=delivery_address or None,
        delivery_contact=delivery_contact or None,
    )


# Список компаний в админке.
@html.get("/admin/companies")
async def companies_list(
    request: Request,
    status: str = Query(default=""),
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    status_value = _status_filter(status)
    query = q.strip()
    companies, total = await list_companies(db, status=status_value, q=query, page=page)
    total_pages = max(1, (total + COMPANIES_PAGE_SIZE - 1) // COMPANIES_PAGE_SIZE)
    if page > total_pages:
        page = total_pages
        companies, total = await list_companies(db, status=status_value, q=query, page=page)
    pending_count = await count_companies(db, status=CompanyStatus.PENDING.value)
    status_key = status_value or ""
    return templates.TemplateResponse(
        request,
        "companies/list.html",
        {
            "companies": companies,
            "pending_count": pending_count,
            "status_filter": status_key,
            "q": query,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "prev_href": _companies_list_href(status_key, query, page - 1),
            "next_href": _companies_list_href(status_key, query, page + 1),
            "clear_href": _companies_list_href(status_key, "", 1),
            "error": None,
        },
    )


# Форма создания компании.
@html.get("/admin/companies/new")
async def companies_new_form(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    return templates.TemplateResponse(
        request,
        "companies/new.html",
        {
            "error": None,
            "form": {},
            "billing_entities": (await list_billing_entities(db, page_size=None))[0],
        },
    )


# Создаёт компанию и показывает временные credentials.
@html.post("/admin/companies")
async def companies_create_submit(
    request: Request,
    name: str = Form(),
    legal_name: str = Form(default=""),
    inn: str = Form(default=""),
    contact_email: str = Form(default=""),
    contact_phone: str = Form(default=""),
    login: str = Form(default=""),
    billing_entity_id: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    form = {
        "name": name,
        "legal_name": legal_name,
        "inn": inn,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "login": login,
        "billing_entity_id": billing_entity_id,
    }
    billing_entities, _ = await list_billing_entities(db, page_size=None)
    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            request,
            "companies/new.html",
            {
                "error": "Укажите название компании",
                "form": form,
                "billing_entities": billing_entities,
            },
            status_code=400,
        )
    try:
        data = CompanyInput(
            name=name,
            legal_name=legal_name or None,
            inn=inn or None,
            contact_email=contact_email or None,
            contact_phone=contact_phone or None,
            login=login or None,
            billing_entity_id=_optional_uuid(billing_entity_id),
        )
        created = await create_company(db, data, auth.subject_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "companies/new.html",
            {"error": str(exc), "form": form, "billing_entities": billing_entities},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "companies/credentials.html",
        {
            "title": "Компания создана",
            "company_id": created.company_id,
            "login": created.login,
            "temporary_password": created.temporary_password,
        },
    )


# Форма редактирования компании.
@html.get("/admin/companies/{company_id}/edit")
async def company_edit_form(
    request: Request,
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    company = await get_company(db, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return templates.TemplateResponse(
        request,
        "companies/edit.html",
        {
            "company": company,
            "billing_entities": (await list_billing_entities(db, page_size=None))[0],
            "error": None,
        },
    )


# Сохраняет правки компании.
@html.post("/admin/companies/{company_id}/edit")
async def company_edit_submit(
    request: Request,
    company_id: UUID,
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
    billing_entity_id: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    company = await get_company(db, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    data = _profile_input(
        name,
        legal_name,
        inn,
        contact_email,
        contact_phone,
        legal_address,
        contact_person,
        kpp,
        delivery_address,
        delivery_contact,
    )
    try:
        updated = await update_company_admin(
            db,
            company_id,
            data,
            auth.subject_id,
            billing_entity_id=_optional_uuid(billing_entity_id),
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "companies/edit.html",
            {
                "company": company,
                "billing_entities": (await list_billing_entities(db, page_size=None))[0],
                "error": str(exc),
            },
            status_code=400,
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return RedirectResponse(f"/admin/companies/{company_id}", status_code=303)


# Карточка компании.
@html.get("/admin/companies/{company_id}")
async def company_detail(
    request: Request,
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    company = await get_company(db, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    invoices = (await list_company_invoices(db, company_id, page_size=None))[0]
    return templates.TemplateResponse(
        request,
        "companies/detail.html",
        {"company": company, "invoices": invoices, "credentials": None, "error": None},
    )


# Сбрасывает пароль и показывает новые credentials.
@html.post("/admin/companies/{company_id}/reset-password")
async def company_reset_password_submit(
    request: Request,
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    result = await reset_company_password(db, company_id, auth.subject_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    company = await get_company(db, company_id)
    invoices = (await list_company_invoices(db, company_id, page_size=None))[0]
    return templates.TemplateResponse(
        request,
        "companies/detail.html",
        {
            "company": company,
            "invoices": invoices,
            "credentials": {
                "login": result.login,
                "temporary_password": result.temporary_password,
            },
            "error": None,
        },
    )


# Деактивирует компанию.
@html.post("/admin/companies/{company_id}/deactivate")
async def company_deactivate_submit(
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        company = await deactivate_company(db, company_id, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return RedirectResponse(f"/admin/companies/{company_id}", status_code=303)


# Активирует компанию.
@html.post("/admin/companies/{company_id}/activate")
async def company_activate_submit(
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        company = await activate_company(db, company_id, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return RedirectResponse(f"/admin/companies/{company_id}", status_code=303)


# Одобряет pending-заявку.
@html.post("/admin/companies/{company_id}/approve")
async def company_approve_submit(
    request: Request,
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        company = await approve_company(db, company_id, auth.subject_id)
    except ValueError as exc:
        company = await get_company(db, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Компания не найдена") from exc
        invoices = (await list_company_invoices(db, company_id, page_size=None))[0]
        return templates.TemplateResponse(
            request,
            "companies/detail.html",
            {
                "company": company,
                "invoices": invoices,
                "credentials": None,
                "error": str(exc),
            },
            status_code=400,
        )
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return RedirectResponse(f"/admin/companies/{company_id}", status_code=303)


# Отклоняет pending-заявку.
@html.post("/admin/companies/{company_id}/reject")
async def company_reject_submit(
    company_id: UUID,
    reason: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    try:
        company = await reject_company(db, company_id, auth.subject_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return RedirectResponse(f"/admin/companies/{company_id}", status_code=303)


# Справочник юрлиц поставщика.
@html.get("/admin/billing-entities")
async def billing_entities_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    entities, total = await list_billing_entities(db, page=page)
    total_pages = _billing_total_pages(total)
    if page > total_pages:
        page = total_pages
        entities, total = await list_billing_entities(db, page=page)
    return templates.TemplateResponse(
        request,
        "companies/billing.html",
        {
            "entities": entities,
            "page": page,
            "total_pages": total_pages,
            "error": None,
            "form": {},
        },
    )


# Создаёт юрлицо поставщика.
@html.post("/admin/billing-entities")
async def billing_entities_create(
    request: Request,
    name: str = Form(),
    legal_name: str = Form(default=""),
    inn: str = Form(default=""),
    kpp: str = Form(default=""),
    legal_address: str = Form(default=""),
    bank_name: str = Form(default=""),
    bik: str = Form(default=""),
    bank_account: str = Form(default=""),
    corr_account: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    form = {
        "name": name,
        "legal_name": legal_name,
        "inn": inn,
        "kpp": kpp,
        "legal_address": legal_address,
        "bank_name": bank_name,
        "bik": bik,
        "bank_account": bank_account,
        "corr_account": corr_account,
    }
    data = _billing_input(
        name, legal_name, inn, kpp, legal_address, bank_name, bik, bank_account, corr_account
    )
    try:
        await create_billing_entity(db, data, auth.subject_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "companies/billing.html",
            {
                "entities": (await list_billing_entities(db, page=1))[0],
                "error": str(exc),
                "form": form,
            },
            status_code=400,
        )
    return RedirectResponse("/admin/billing-entities", status_code=303)


# Форма правки юрлица поставщика.
@html.get("/admin/billing-entities/{entity_id}/edit")
async def billing_entity_edit_form(
    request: Request,
    entity_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    entity = await get_billing_entity(db, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Юрлицо не найдено")
    return templates.TemplateResponse(
        request,
        "companies/billing_edit.html",
        {"entity": entity, "error": None},
    )


# Сохраняет правки юрлица поставщика.
@html.post("/admin/billing-entities/{entity_id}/edit")
async def billing_entity_edit_submit(
    request: Request,
    entity_id: UUID,
    name: str = Form(),
    legal_name: str = Form(default=""),
    inn: str = Form(default=""),
    kpp: str = Form(default=""),
    legal_address: str = Form(default=""),
    bank_name: str = Form(default=""),
    bik: str = Form(default=""),
    bank_account: str = Form(default=""),
    corr_account: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin),
):
    entity = await get_billing_entity(db, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Юрлицо не найдено")
    data = _billing_input(
        name, legal_name, inn, kpp, legal_address, bank_name, bik, bank_account, corr_account
    )
    try:
        updated = await update_billing_entity(db, entity_id, data, auth.subject_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "companies/billing_edit.html",
            {"entity": entity, "error": str(exc)},
            status_code=400,
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="Юрлицо не найдено")
    return RedirectResponse("/admin/billing-entities", status_code=303)


# JSON: список компаний.
@api.get("/admin/companies")
async def api_companies_list(
    status: str = Query(default=""),
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    companies, total = await list_companies(
        db, status=_status_filter(status), q=q.strip(), page=page
    )
    return {
        "items": [_company_json(company) for company in companies],
        "total": total,
        "page": page,
        "page_size": COMPANIES_PAGE_SIZE,
    }


# JSON: карточка компании.
@api.get("/admin/companies/{company_id}")
async def api_company_detail(
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    company = await get_company(db, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return _company_json(company)


# JSON: правка компании.
@api.patch("/admin/companies/{company_id}")
async def api_update_company(
    company_id: UUID,
    body: UpdateCompanyBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    data = CompanyProfileInput(
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
    )
    try:
        company = await update_company_admin(
            db,
            company_id,
            data,
            auth.subject_id,
            billing_entity_id=body.billing_entity_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return _company_json(company)


# JSON: создание компании.
@api.post("/admin/companies")
async def api_create_company(
    body: CreateCompanyBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    data = CompanyInput(
        name=body.name.strip(),
        legal_name=body.legal_name,
        inn=body.inn,
        contact_email=body.contact_email,
        contact_phone=body.contact_phone,
        login=body.login,
        billing_entity_id=body.billing_entity_id,
    )
    try:
        created = await create_company(db, data, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "company_id": str(created.company_id),
        "login": created.login,
        "temporary_password": created.temporary_password,
    }


# JSON: сброс пароля учётки.
@api.post("/admin/companies/{company_id}/reset-password")
async def api_reset_password(
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    result = await reset_company_password(db, company_id, auth.subject_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return {
        "company_id": str(result.company_id),
        "login": result.login,
        "temporary_password": result.temporary_password,
    }


# JSON: деактивация учётки.
@api.post("/admin/companies/{company_id}/deactivate")
async def api_deactivate_company(
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    try:
        company = await deactivate_company(db, company_id, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return _company_json(company)


# JSON: активация учётки.
@api.post("/admin/companies/{company_id}/activate")
async def api_activate_company(
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    try:
        company = await activate_company(db, company_id, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return _company_json(company)


# JSON: одобрение заявки.
@api.post("/admin/companies/{company_id}/approve")
async def api_approve_company(
    company_id: UUID,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    try:
        company = await approve_company(db, company_id, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return _company_json(company)


# JSON: отклонение заявки.
@api.post("/admin/companies/{company_id}/reject")
async def api_reject_company(
    company_id: UUID,
    body: RejectCompanyBody | None = None,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    reason = body.reason if body is not None else None
    try:
        company = await reject_company(db, company_id, auth.subject_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if company is None:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return _company_json(company)


# JSON: список юрлиц поставщика.
@api.get("/admin/billing-entities")
async def api_billing_entities_list(
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    entities, total = await list_billing_entities(db, page=page)
    return {
        "items": [_billing_json(entity) for entity in entities],
        "total": total,
        "page": page,
        "page_size": BILLING_ENTITIES_PAGE_SIZE,
    }


# JSON: создание юрлица поставщика.
@api.post("/admin/billing-entities")
async def api_create_billing_entity(
    body: CreateBillingEntityBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    data = _billing_input(
        body.name,
        body.legal_name,
        body.inn,
        body.kpp,
        body.legal_address,
        body.bank_name,
        body.bik,
        body.bank_account,
        body.corr_account,
    )
    try:
        entity = await create_billing_entity(db, data, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _billing_json(entity)


# JSON: правка юрлица поставщика.
@api.put("/admin/billing-entities/{entity_id}")
async def api_update_billing_entity(
    entity_id: UUID,
    body: CreateBillingEntityBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    data = _billing_input(
        body.name,
        body.legal_name,
        body.inn,
        body.kpp,
        body.legal_address,
        body.bank_name,
        body.bik,
        body.bank_account,
        body.corr_account,
    )
    try:
        entity = await update_billing_entity(db, entity_id, data, auth.subject_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if entity is None:
        raise HTTPException(status_code=404, detail="Юрлицо не найдено")
    return _billing_json(entity)
