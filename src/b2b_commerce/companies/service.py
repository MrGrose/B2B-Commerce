import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.audit.service import write_audit
from b2b_commerce.auth.service import hash_password, revoke_company_sessions
from b2b_commerce.cart.models import Cart
from b2b_commerce.companies.models import BillingEntity, Company, CompanyAccount
from b2b_commerce.config import Settings
from b2b_commerce.enums import CompanyStatus, InvoiceStatus, SessionSubjectType
from b2b_commerce.invoices.models import Invoice
from b2b_commerce.invoices.service import count_company_invoices

logger = logging.getLogger(__name__)

_PROFILE_PAID_STATUSES = (
    InvoiceStatus.PAID.value,
    InvoiceStatus.SHIPPED.value,
    InvoiceStatus.COMPLETED.value,
)

MIN_PASSWORD_LENGTH = 8
COMPANIES_PAGE_SIZE = 30
BILLING_ENTITIES_PAGE_SIZE = 30
_LOGIN_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[0-9]{10,15}$")
_INN_RE = re.compile(r"^(\d{10}|\d{12})$")


@dataclass
class CompanyInput:
    name: str
    legal_name: str | None = None
    inn: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    login: str | None = None
    billing_entity_id: UUID | None = None


@dataclass
class BillingEntityInput:
    name: str
    legal_name: str
    inn: str
    kpp: str | None = None
    legal_address: str | None = None
    bank_name: str | None = None
    bik: str | None = None
    bank_account: str | None = None
    corr_account: str | None = None


@dataclass
class BillingEntityView:
    id: UUID
    name: str
    legal_name: str
    inn: str
    kpp: str | None
    legal_address: str | None
    bank_name: str | None
    bik: str | None
    bank_account: str | None
    corr_account: str | None


@dataclass
class CompanyProfileInput:
    name: str
    legal_name: str | None = None
    inn: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    legal_address: str | None = None
    contact_person: str | None = None
    kpp: str | None = None
    delivery_address: str | None = None
    delivery_contact: str | None = None


@dataclass
class RegistrationInput:
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


@dataclass
class CompanyCredentials:
    company_id: UUID
    login: str
    temporary_password: str


@dataclass
class CompanyAccountView:
    id: UUID
    login: str
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None


@dataclass
class CompanyView:
    id: UUID
    name: str
    legal_name: str | None
    inn: str | None
    contact_email: str | None
    contact_phone: str | None
    status: str
    account: CompanyAccountView | None
    legal_address: str | None = None
    contact_person: str | None = None
    kpp: str | None = None
    delivery_address: str | None = None
    delivery_contact: str | None = None
    rejection_reason: str | None = None
    created_at: datetime | None = None
    billing_entity_id: UUID | None = None
    billing_entity_name: str | None = None


# Собирает представление юрлица поставщика.
def _billing_entity_view(entity: BillingEntity) -> BillingEntityView:
    return BillingEntityView(
        id=entity.id,
        name=entity.name,
        legal_name=entity.legal_name,
        inn=entity.inn,
        kpp=entity.kpp,
        legal_address=entity.legal_address,
        bank_name=entity.bank_name,
        bik=entity.bik,
        bank_account=entity.bank_account,
        corr_account=entity.corr_account,
    )


# Делает короткий slug из названия компании для login.
def _login_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip())
    slug = slug.strip("-")
    return slug[:24] if slug else "company"


# Пустую строку приводит к None.
def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


# Генерирует уникальный login для новой учётки.
async def _generate_unique_login(db: AsyncSession, name: str) -> str:
    base = _login_slug(name)
    for _attempt in range(20):
        suffix = secrets.token_hex(3)
        login = f"{base}-{suffix}"
        exists = await db.scalar(select(CompanyAccount.id).where(CompanyAccount.login == login))
        if exists is None:
            return login
    login = f"company-{secrets.token_hex(6)}"
    logger.warning("Использован fallback login для компании %s", name)
    return login


# Генерирует временный пароль для передачи клиенту один раз.
def _generate_temporary_password() -> str:
    return secrets.token_urlsafe(12)


# Собирает карточку компании с учёткой.
# Собирает CompanyAccountView или None.
def _company_account_view(account: CompanyAccount | None) -> CompanyAccountView | None:
    if account is None:
        return None
    return CompanyAccountView(
        id=account.id,
        login=account.login,
        is_active=account.is_active,
        must_change_password=account.must_change_password,
        last_login_at=account.last_login_at,
    )


# Собирает CompanyView из company и уже загруженных account/billing.
def _company_view_from_parts(
    company: Company,
    account: CompanyAccount | None,
    billing_name: str | None,
) -> CompanyView:
    return CompanyView(
        id=company.id,
        name=company.name,
        legal_name=company.legal_name,
        inn=company.inn,
        contact_email=company.contact_email,
        contact_phone=company.contact_phone,
        status=company.status,
        account=_company_account_view(account),
        legal_address=company.legal_address,
        contact_person=company.contact_person,
        kpp=company.kpp,
        delivery_address=company.delivery_address,
        delivery_contact=company.delivery_contact,
        rejection_reason=company.rejection_reason,
        created_at=company.created_at,
        billing_entity_id=company.billing_entity_id,
        billing_entity_name=billing_name,
    )


# Возвращает CompanyView для одной компании.
async def _company_view(db: AsyncSession, company: Company) -> CompanyView:
    account = await db.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == company.id)
    )
    billing_name = None
    if company.billing_entity_id is not None:
        entity = await db.get(BillingEntity, company.billing_entity_id)
        billing_name = entity.name if entity is not None else None
    return _company_view_from_parts(company, account, billing_name)


# Собирает CompanyView для списка без N+1 по account и billing.
async def _company_views_from_rows(
    db: AsyncSession,
    companies: list[Company],
) -> list[CompanyView]:
    if not companies:
        return []

    company_ids = [company.id for company in companies]
    account_rows = (
        await db.scalars(
            select(CompanyAccount).where(CompanyAccount.company_id.in_(company_ids))
        )
    ).all()
    accounts_by_company = {row.company_id: row for row in account_rows}

    billing_ids = {
        company.billing_entity_id
        for company in companies
        if company.billing_entity_id is not None
    }
    billing_by_id: dict[UUID, BillingEntity] = {}
    if billing_ids:
        billing_rows = (
            await db.scalars(select(BillingEntity).where(BillingEntity.id.in_(billing_ids)))
        ).all()
        billing_by_id = {row.id: row for row in billing_rows}

    return [
        _company_view_from_parts(
            company,
            accounts_by_company.get(company.id),
            (
                billing_by_id[company.billing_entity_id].name
                if company.billing_entity_id is not None
                and company.billing_entity_id in billing_by_id
                else None
            ),
        )
        for company in companies
    ]


# Фильтры списка компаний по статусу и поиску.
def _company_list_filters(status: str | None, q: str | None):
    filters = []
    if status:
        filters.append(Company.status == status)
    query = (q or "").strip()
    if query:
        pattern = f"%{query}%"
        login_ids = select(CompanyAccount.company_id).where(CompanyAccount.login.ilike(pattern))
        filters.append(
            or_(
                Company.name.ilike(pattern),
                Company.legal_name.ilike(pattern),
                Company.inn.ilike(pattern),
                Company.contact_email.ilike(pattern),
                Company.id.in_(login_ids),
            )
        )
    return filters


# Считает компании по тем же фильтрам, что и список.
async def count_companies(
    db: AsyncSession,
    status: str | None = None,
    q: str | None = None,
) -> int:
    stmt = select(func.count()).select_from(Company)
    filters = _company_list_filters(status, q)
    if filters:
        stmt = stmt.where(*filters)
    return int(await db.scalar(stmt) or 0)


# Возвращает страницу компаний для админки и общее число.
async def list_companies(
    db: AsyncSession,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
) -> tuple[list[CompanyView], int]:
    filters = _company_list_filters(status, q)
    total = await count_companies(db, status=status, q=q)
    page = max(1, page)
    stmt = select(Company)
    if filters:
        stmt = stmt.where(*filters)
    pending_first = case((Company.status == CompanyStatus.PENDING.value, 0), else_=1)
    stmt = (
        stmt.order_by(pending_first, Company.created_at.desc())
        .offset((page - 1) * COMPANIES_PAGE_SIZE)
        .limit(COMPANIES_PAGE_SIZE)
    )
    companies = (await db.scalars(stmt)).all()
    return await _company_views_from_rows(db, companies), total


# Пишет поля профиля в модель после валидации.
async def _apply_profile_fields(
    db: AsyncSession,
    company: Company,
    data: CompanyProfileInput,
) -> None:
    name = data.name.strip()
    if not name:
        raise ValueError("Укажите название компании")
    inn = _blank_to_none(data.inn)
    email = _blank_to_none(data.contact_email)
    phone = _blank_to_none(data.contact_phone)
    legal_name = _blank_to_none(data.legal_name)
    if inn is not None:
        inn = _validate_inn(inn)
    if email is not None:
        email = _validate_email(email)
    if phone is not None:
        phone = _validate_phone(phone)
    await _assert_unique_company_fields(db, inn, email, exclude_company_id=company.id)
    company.name = name
    company.legal_name = legal_name
    company.inn = inn
    company.contact_email = email
    company.contact_phone = phone
    company.legal_address = _blank_to_none(data.legal_address)
    company.contact_person = _blank_to_none(data.contact_person)
    company.kpp = _blank_to_none(data.kpp)
    company.delivery_address = _blank_to_none(data.delivery_address)
    company.delivery_contact = _blank_to_none(data.delivery_contact)


# Находит юрлицо или бросает, если id задан и записи нет.
async def _resolve_billing_entity(
    db: AsyncSession,
    billing_entity_id: UUID | None,
) -> UUID | None:
    if billing_entity_id is None:
        return None
    entity = await db.get(BillingEntity, billing_entity_id)
    if entity is None:
        raise ValueError("Юрлицо поставщика не найдено")
    return entity.id


# Проверяет, может ли клиент менять login и пароль.
def assert_can_change_credentials(status: str) -> None:
    if status in (CompanyStatus.ACTIVE.value, CompanyStatus.PENDING.value):
        return
    if status == CompanyStatus.REJECTED.value:
        raise ValueError("Нельзя изменить учётные данные отклонённой заявки")
    raise ValueError("Учётная запись недоступна")

# Обновляет контактные данные компании из клиентского профиля.
async def update_company_profile(
    db: AsyncSession,
    company_id: UUID,
    account_id: UUID,
    data: CompanyProfileInput,
) -> CompanyView | None:
    company = await db.get(Company, company_id)
    if company is None:
        return None
    if company.status == CompanyStatus.REJECTED.value:
        raise ValueError("Отклонённую заявку нельзя изменить")
    await _apply_profile_fields(db, company, data)
    await write_audit(
        db,
        SessionSubjectType.COMPANY.value,
        account_id,
        "company.profile_update",
        "company",
        company_id,
        None,
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(_registration_integrity_message(exc)) from exc
    return await get_company(db, company_id)


# Админ правит реквизиты компании и закрепляет юрлицо поставщика.
async def update_company_admin(
    db: AsyncSession,
    company_id: UUID,
    data: CompanyProfileInput,
    admin_id: UUID,
    billing_entity_id: UUID | None = None,
) -> CompanyView | None:
    company = await db.get(Company, company_id)
    if company is None:
        return None
    await _apply_profile_fields(db, company, data)
    company.billing_entity_id = await _resolve_billing_entity(db, billing_entity_id)
    await write_audit(
        db,
        SessionSubjectType.ADMIN.value,
        admin_id,
        "company.admin_update",
        "company",
        company_id,
        {
            "billing_entity_id": (
                str(company.billing_entity_id) if company.billing_entity_id else None
            )
        },
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(_registration_integrity_message(exc)) from exc
    return await get_company(db, company_id)


# Возвращает компанию по id или None.
async def get_company(db: AsyncSession, company_id: UUID) -> CompanyView | None:
    company = await db.get(Company, company_id)
    if company is None:
        return None
    return await _company_view(db, company)


# Создаёт компанию, учётку и корзину; возвращает временный пароль один раз.
async def create_company(
    db: AsyncSession,
    data: CompanyInput,
    admin_id: UUID,
) -> CompanyCredentials:
    inn = _blank_to_none(data.inn)
    email = _blank_to_none(data.contact_email)
    if email is not None:
        email = email.lower()
    requested_login = _blank_to_none(data.login)
    login = _validate_login(requested_login) if requested_login else None
    await _assert_unique_company_fields(db, inn, email, login=login)
    if login is None:
        login = await _generate_unique_login(db, data.name)
    temporary_password = _generate_temporary_password()
    billing_entity_id = await _resolve_billing_entity(db, data.billing_entity_id)
    company = Company(
        name=data.name.strip(),
        legal_name=_blank_to_none(data.legal_name),
        inn=inn,
        contact_email=email,
        contact_phone=_blank_to_none(data.contact_phone),
        billing_entity_id=billing_entity_id,
        status=CompanyStatus.ACTIVE.value,
    )
    db.add(company)
    try:
        await db.flush()
        account = CompanyAccount(
            company_id=company.id,
            login=login,
            password_hash=hash_password(temporary_password),
            must_change_password=True,
            is_active=True,
        )
        db.add(account)
        db.add(Cart(company_id=company.id))
        await write_audit(
            db,
            SessionSubjectType.ADMIN.value,
            admin_id,
            "company.create",
            "company",
            company.id,
            {"login": login},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(_registration_integrity_message(exc)) from exc
    logger.info("Создана компания %s, login=%s", company.id, login)
    return CompanyCredentials(company.id, login, temporary_password)


# Проверяет login клиента.
def _validate_login(login: str) -> str:
    value = login.strip()
    if not value:
        raise ValueError("Укажите логин")
    if not _LOGIN_RE.fullmatch(value):
        raise ValueError("Логин: 3–64 символа, латиница, цифры, точка, дефис или подчёркивание")
    return value


# Проверяет пароль по политике.
def _validate_password(password: str) -> str:
    if not password:
        raise ValueError("Укажите пароль")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Пароль не короче {MIN_PASSWORD_LENGTH} символов")
    if len(password) > 128:
        raise ValueError("Пароль слишком длинный")
    return password


# Проверяет ИНН: 10 или 12 цифр.
def _validate_inn(inn: str) -> str:
    if not _INN_RE.fullmatch(inn):
        raise ValueError("ИНН должен содержать 10 или 12 цифр")
    return inn


# Проверяет email.
def _validate_email(email: str) -> str:
    value = email.strip().lower()
    if not _EMAIL_RE.fullmatch(value):
        raise ValueError("Укажите корректный email")
    return value


# Проверяет телефон.
def _validate_phone(phone: str) -> str:
    compact = re.sub(r"[\s()-]", "", phone.strip())
    if not _PHONE_RE.fullmatch(compact):
        raise ValueError("Укажите телефон: 10–15 цифр, можно с +")
    return compact


# Сообщение по нарушению unique-ограничения.
def _registration_integrity_message(exc: IntegrityError) -> str:
    detail = str(exc.orig).lower() if exc.orig is not None else str(exc).lower()
    if "company_accounts_login" in detail or "login" in detail and "company_account" in detail:
        return "Такой логин уже занят"
    if "uq_companies_inn" in detail or "companies_inn" in detail:
        return "Компания с таким ИНН уже зарегистрирована"
    if "uq_companies_contact_email" in detail or "contact_email" in detail:
        return "Компания с таким email уже зарегистрирована"
    if "uq_billing_entities_inn" in detail:
        return "Юрлицо с таким ИНН уже есть"
    return "Не удалось сохранить данные компании"


# Проверяет уникальность login/email/inn до вставки.
async def _assert_unique_company_fields(
    db: AsyncSession,
    inn: str | None,
    email: str | None,
    exclude_company_id: UUID | None = None,
    login: str | None = None,
) -> None:
    if login:
        login_stmt = select(CompanyAccount.id).where(
            func.lower(CompanyAccount.login) == login.lower()
        )
        if await db.scalar(login_stmt) is not None:
            raise ValueError("Такой логин уже занят")
    if inn:
        inn_stmt = select(Company.id).where(Company.inn == inn)
        if exclude_company_id is not None:
            inn_stmt = inn_stmt.where(Company.id != exclude_company_id)
        if await db.scalar(inn_stmt) is not None:
            raise ValueError("Компания с таким ИНН уже зарегистрирована")
    if email:
        email_stmt = select(Company.id).where(func.lower(Company.contact_email) == email.lower())
        if exclude_company_id is not None:
            email_stmt = email_stmt.where(Company.id != exclude_company_id)
        if await db.scalar(email_stmt) is not None:
            raise ValueError("Компания с таким email уже зарегистрирована")


# Регистрирует клиента: Company + Account + Cart в одной транзакции.
async def register_company(db: AsyncSession, data: RegistrationInput) -> CompanyView:
    login = _validate_login(data.login)
    password = _validate_password(data.password)
    name = data.name.strip()
    if not name:
        raise ValueError("Укажите название компании")
    legal_name = _blank_to_none(data.legal_name)
    if legal_name is None:
        raise ValueError("Укажите юридическое название")
    inn = _validate_inn((data.inn or "").strip())
    email = _validate_email(data.contact_email or "")
    phone = _validate_phone(data.contact_phone or "")
    await _assert_unique_company_fields(db, inn, email, login=login)
    company = Company(
        name=name,
        legal_name=legal_name,
        inn=inn,
        contact_email=email,
        contact_phone=phone,
        legal_address=_blank_to_none(data.legal_address),
        contact_person=_blank_to_none(data.contact_person),
        kpp=_blank_to_none(data.kpp),
        delivery_address=_blank_to_none(data.delivery_address),
        delivery_contact=_blank_to_none(data.delivery_contact),
        status=CompanyStatus.PENDING.value,
    )
    db.add(company)
    try:
        await db.flush()
        account = CompanyAccount(
            company_id=company.id,
            login=login,
            password_hash=hash_password(password),
            must_change_password=False,
            is_active=True,
        )
        db.add(account)
        db.add(Cart(company_id=company.id))
        await db.flush()
        await write_audit(
            db,
            SessionSubjectType.COMPANY.value,
            account.id,
            "company.register",
            "company",
            company.id,
            {"login": login},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(_registration_integrity_message(exc)) from exc
    except Exception:
        await db.rollback()
        raise
    logger.info("Заявка компании %s, login=%s", company.id, login)
    return await get_company(db, company.id)


# Проверяет, что pending-компания готова к одобрению (юрлицо поставщика назначено).
async def _assert_approval_ready(db: AsyncSession, company: Company) -> None:
    if company.billing_entity_id is None:
        raise ValueError("Нельзя одобрить заявку: назначьте юрлицо поставщика")
    entity = await db.get(BillingEntity, company.billing_entity_id)
    if entity is None:
        raise ValueError("Юрлицо поставщика не найдено")


# Одобряет pending-заявку.
async def approve_company(
    db: AsyncSession,
    company_id: UUID,
    admin_id: UUID,
) -> CompanyView | None:
    company = await db.get(Company, company_id)
    if company is None:
        return None
    if company.status != CompanyStatus.PENDING.value:
        raise ValueError("Одобрить можно только заявку на рассмотрении")
    await _assert_approval_ready(db, company)
    company.status = CompanyStatus.ACTIVE.value
    company.rejection_reason = None
    await write_audit(
        db,
        SessionSubjectType.ADMIN.value,
        admin_id,
        "company.approve",
        "company",
        company_id,
        None,
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    logger.info("Одобрена компания %s", company_id)
    return await get_company(db, company_id)


# Отклоняет pending-заявку.
async def reject_company(
    db: AsyncSession,
    company_id: UUID,
    admin_id: UUID,
    reason: str | None = None,
) -> CompanyView | None:
    company = await db.get(Company, company_id)
    if company is None:
        return None
    if company.status != CompanyStatus.PENDING.value:
        raise ValueError("Отклонить можно только заявку на рассмотрении")
    company.status = CompanyStatus.REJECTED.value
    company.rejection_reason = _blank_to_none(reason)
    await write_audit(
        db,
        SessionSubjectType.ADMIN.value,
        admin_id,
        "company.reject",
        "company",
        company_id,
        {"reason": company.rejection_reason},
    )
    await db.commit()
    logger.info("Отклонена компания %s", company_id)
    return await get_company(db, company_id)


# Сбрасывает пароль учётки и отзывает её сессии.
async def reset_company_password(
    db: AsyncSession,
    company_id: UUID,
    admin_id: UUID,
) -> CompanyCredentials | None:
    account = await db.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == company_id)
    )
    if account is None:
        return None
    temporary_password = _generate_temporary_password()
    account.password_hash = hash_password(temporary_password)
    account.must_change_password = True
    await revoke_company_sessions(db, account.id)
    await write_audit(
        db,
        SessionSubjectType.ADMIN.value,
        admin_id,
        "company.reset_password",
        "company",
        company_id,
        {"login": account.login},
    )
    await db.commit()
    logger.info("Сброшен пароль компании %s", company_id)
    return CompanyCredentials(company_id, account.login, temporary_password)


# Деактивирует компанию и её учётку.
async def deactivate_company(
    db: AsyncSession,
    company_id: UUID,
    admin_id: UUID,
) -> CompanyView | None:
    company = await db.get(Company, company_id)
    if company is None:
        return None
    if company.status != CompanyStatus.ACTIVE.value:
        raise ValueError("Приостановить можно только активную компанию")
    account = await db.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == company_id)
    )
    company.status = CompanyStatus.SUSPENDED.value
    if account is not None:
        account.is_active = False
        await revoke_company_sessions(db, account.id)
    await write_audit(
        db,
        SessionSubjectType.ADMIN.value,
        admin_id,
        "company.deactivate",
        "company",
        company_id,
        None,
    )
    await db.commit()
    return await get_company(db, company_id)


# Активирует приостановленную компанию и её учётку.
async def activate_company(
    db: AsyncSession,
    company_id: UUID,
    admin_id: UUID,
) -> CompanyView | None:
    company = await db.get(Company, company_id)
    if company is None:
        return None
    if company.status != CompanyStatus.SUSPENDED.value:
        raise ValueError("Возобновить можно только приостановленную компанию")
    account = await db.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == company_id)
    )
    company.status = CompanyStatus.ACTIVE.value
    if account is not None:
        account.is_active = True
    await write_audit(
        db,
        SessionSubjectType.ADMIN.value,
        admin_id,
        "company.activate",
        "company",
        company_id,
        None,
    )
    await db.commit()
    return await get_company(db, company_id)


# Считает юрлица поставщика.
async def count_billing_entities(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(BillingEntity)) or 0)


# Список юрлиц поставщика (page_size=None — все строки для select в формах).
async def list_billing_entities(
    db: AsyncSession,
    page: int = 1,
    page_size: int | None = BILLING_ENTITIES_PAGE_SIZE,
) -> tuple[list[BillingEntityView], int]:
    total = await count_billing_entities(db)
    page = max(1, page)
    stmt = select(BillingEntity).order_by(BillingEntity.name)
    if page_size is not None:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    entities = (await db.scalars(stmt)).all()
    return [_billing_entity_view(entity) for entity in entities], total


# Нормализует и проверяет поля юрлица поставщика.
def _validated_billing_input(data: BillingEntityInput) -> BillingEntityInput:
    name = data.name.strip()
    legal_name = data.legal_name.strip()
    if not name:
        raise ValueError("Укажите наименование")
    if not legal_name:
        raise ValueError("Укажите юридическое название")
    return BillingEntityInput(
        name=name,
        legal_name=legal_name,
        inn=_validate_inn(data.inn.strip()),
        kpp=_blank_to_none(data.kpp),
        legal_address=_blank_to_none(data.legal_address),
        bank_name=_blank_to_none(data.bank_name),
        bik=_blank_to_none(data.bik),
        bank_account=_blank_to_none(data.bank_account),
        corr_account=_blank_to_none(data.corr_account),
    )


# Копирует проверенные поля в модель юрлица.
def _apply_billing_fields(entity: BillingEntity, data: BillingEntityInput) -> None:
    entity.name = data.name
    entity.legal_name = data.legal_name
    entity.inn = data.inn
    entity.kpp = data.kpp
    entity.legal_address = data.legal_address
    entity.bank_name = data.bank_name
    entity.bik = data.bik
    entity.bank_account = data.bank_account
    entity.corr_account = data.corr_account


# Возвращает юрлицо поставщика по id.
async def get_billing_entity(
    db: AsyncSession,
    entity_id: UUID,
) -> BillingEntityView | None:
    entity = await db.get(BillingEntity, entity_id)
    if entity is None:
        return None
    return _billing_entity_view(entity)


# Создаёт юрлицо поставщика.
async def create_billing_entity(
    db: AsyncSession,
    data: BillingEntityInput,
    admin_id: UUID,
) -> BillingEntityView:
    fields = _validated_billing_input(data)
    exists = await db.scalar(select(BillingEntity.id).where(BillingEntity.inn == fields.inn))
    if exists is not None:
        raise ValueError("Юрлицо с таким ИНН уже есть")
    entity = BillingEntity()
    _apply_billing_fields(entity, fields)
    db.add(entity)
    try:
        await db.flush()
        await write_audit(
            db,
            SessionSubjectType.ADMIN.value,
            admin_id,
            "billing_entity.create",
            "billing_entity",
            entity.id,
            {"inn": fields.inn},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(_registration_integrity_message(exc)) from exc
    return _billing_entity_view(entity)


# Обновляет существующее юрлицо поставщика.
async def update_billing_entity(
    db: AsyncSession,
    entity_id: UUID,
    data: BillingEntityInput,
    admin_id: UUID,
) -> BillingEntityView | None:
    entity = await db.get(BillingEntity, entity_id)
    if entity is None:
        return None
    fields = _validated_billing_input(data)
    duplicate = await db.scalar(
        select(BillingEntity.id).where(
            BillingEntity.inn == fields.inn,
            BillingEntity.id != entity_id,
        )
    )
    if duplicate is not None:
        raise ValueError("Юрлицо с таким ИНН уже есть")
    _apply_billing_fields(entity, fields)
    try:
        await write_audit(
            db,
            SessionSubjectType.ADMIN.value,
            admin_id,
            "billing_entity.update",
            "billing_entity",
            entity.id,
            {"inn": fields.inn},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(_registration_integrity_message(exc)) from exc
    return _billing_entity_view(entity)


# Создаёт дефолтное юрлицо из SUPPLIER_* настроек, если его ещё нет.
async def ensure_default_billing_entity(db: AsyncSession, settings: Settings) -> BillingEntity:
    existing = await db.scalar(
        select(BillingEntity).where(BillingEntity.inn == settings.supplier_inn)
    )
    if existing is not None:
        return existing
    entity = BillingEntity(
        name=settings.supplier_legal_name,
        legal_name=settings.supplier_legal_name,
        inn=settings.supplier_inn,
        kpp=settings.supplier_kpp,
        legal_address=settings.supplier_legal_address,
        bank_name=settings.supplier_bank_name,
        bik=settings.supplier_bik,
        bank_account=settings.supplier_bank_account,
        corr_account=settings.supplier_corr_account,
    )
    db.add(entity)
    await db.flush()
    logger.info("Создано юрлицо поставщика %s", entity.inn)
    return entity

@dataclass
class CompanyProfileMetrics:
    invoice_count: int
    paid_total: Decimal


# Сводка для боковой панели профиля клиента.
async def get_company_profile_metrics(
    db: AsyncSession,
    company_id: UUID,
) -> CompanyProfileMetrics:
    invoice_count = await count_company_invoices(db, company_id)
    paid_total = Decimal(
        await db.scalar(
            select(func.coalesce(func.sum(Invoice.total), 0)).where(
                Invoice.company_id == company_id,
                Invoice.status.in_(_PROFILE_PAID_STATUSES),
            )
        )
        or 0
    )
    return CompanyProfileMetrics(
        invoice_count=invoice_count,
        paid_total=paid_total,
    )

