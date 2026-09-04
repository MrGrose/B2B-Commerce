import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.audit.service import write_audit
from b2b_commerce.auth.models import AdminUser, Session
from b2b_commerce.companies.models import Company, CompanyAccount
from b2b_commerce.config import Settings
from b2b_commerce.enums import CompanyStatus, SessionSubjectType

logger = logging.getLogger(__name__)
_hasher = PasswordHasher()


class RateLimitUnavailable(Exception):
    pass


@dataclass
class LoginHit:
    subject_type: SessionSubjectType
    subject_id: UUID
    password_hash: str
    must_change_password: bool
    company_status: str | None = None


# Считает Argon2id-хеш пароля.
def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


# Проверяет пароль против хеша.
def verify_password(plain: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, plain)
    except VerifyMismatchError:
        return False


# Хеширует токен сессии для хранения в БД.
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()




@dataclass
class AdminUserRow:
    id: UUID
    login: str
    is_active: bool
    last_login_at: datetime | None


# Список администраторов.
async def list_admin_users(db: AsyncSession) -> list[AdminUserRow]:
    rows = await db.scalars(select(AdminUser).order_by(AdminUser.login))
    return [
        AdminUserRow(
            id=row.id,
            login=row.login,
            is_active=row.is_active,
            last_login_at=row.last_login_at,
        )
        for row in rows.all()
    ]


# Создаёт администратора.
async def create_admin_user(
    db: AsyncSession,
    login: str,
    password: str,
    actor_id: UUID,
) -> AdminUser:
    label = login.strip()
    if not label:
        raise ValueError("Укажите логин")
    if len(password) < 8:
        raise ValueError("Пароль должен быть не короче 8 символов")
    existing = await db.scalar(select(AdminUser).where(AdminUser.login == label))
    if existing is not None:
        raise ValueError("Администратор с таким логином уже есть")
    admin = AdminUser(login=label, password_hash=hash_password(password), is_active=True)
    db.add(admin)
    await db.flush()


    await write_audit(
        db,
        actor_type="admin",
        actor_id=actor_id,
        action="admin.create",
        entity_type="admin_user",
        entity_id=admin.id,
        payload={"login": label},
    )
    await db.commit()
    await db.refresh(admin)
    return admin


# Создаёт первого администратора (bootstrap). Не требует существующего actor.
async def bootstrap_first_admin(
    db: AsyncSession,
    login: str,
    password: str,
) -> AdminUser:
    label = login.strip()
    if not label:
        raise ValueError("Укажите логин")
    if len(password) < 8:
        raise ValueError("Пароль должен быть не короче 8 символов")
    existing = await db.scalar(select(AdminUser).where(AdminUser.login == label))
    if existing is not None:
        raise ValueError("Администратор с таким логином уже есть")
    admin = AdminUser(login=label, password_hash=hash_password(password), is_active=True)
    db.add(admin)
    await db.flush()
    await write_audit(
        db,
        actor_type="admin",
        actor_id=admin.id,
        action="admin.bootstrap",
        entity_type="admin_user",
        entity_id=admin.id,
        payload={"login": label},
    )
    await db.commit()
    await db.refresh(admin)
    return admin


# Включает или отключает администратора.
async def set_admin_active(
    db: AsyncSession,
    admin_id: UUID,
    is_active: bool,
    actor_id: UUID,
) -> AdminUser:
    if admin_id == actor_id and not is_active:
        raise ValueError("Нельзя деактивировать себя")
    admin = await db.get(AdminUser, admin_id)
    if admin is None:
        raise ValueError("Администратор не найден")
    admin.is_active = is_active


    await write_audit(
        db,
        actor_type="admin",
        actor_id=actor_id,
        action="admin.deactivate" if not is_active else "admin.activate",
        entity_type="admin_user",
        entity_id=admin.id,
        payload={"login": admin.login},
    )
    await db.commit()
    await db.refresh(admin)
    return admin

# Ищет админа или клиентскую учётку по login.
async def find_login(db: AsyncSession, login: str) -> LoginHit | None:
    admin = await db.scalar(select(AdminUser).where(AdminUser.login == login))
    if admin and admin.is_active:
        return LoginHit(SessionSubjectType.ADMIN, admin.id, admin.password_hash, False)
    account = await db.scalar(select(CompanyAccount).where(CompanyAccount.login == login))
    if account is None or not account.is_active:
        return None
    company = await db.get(Company, account.company_id)
    if company is None or company.status == CompanyStatus.SUSPENDED.value:
        return None
    return LoginHit(
        SessionSubjectType.COMPANY,
        account.id,
        account.password_hash,
        account.must_change_password,
        company.status,
    )


# Создаёт сессию и возвращает plaintext-токен для cookie.
async def create_session(
    db: AsyncSession,
    settings: Settings,
    subject_type: SessionSubjectType,
    subject_id: UUID,
) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        Session(
            subject_type=subject_type.value,
            subject_id=subject_id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours),
        )
    )
    return token


# Ставит last_login_at у админа или клиентской учётки.
async def touch_last_login(db: AsyncSession, hit: LoginHit) -> None:
    now = datetime.now(UTC)
    if hit.subject_type is SessionSubjectType.ADMIN:
        await db.execute(
            update(AdminUser).where(AdminUser.id == hit.subject_id).values(last_login_at=now)
        )
        return
    await db.execute(
        update(CompanyAccount).where(CompanyAccount.id == hit.subject_id).values(last_login_at=now)
    )


# Проверяет, не превышен ли лимит неудачных входов.
async def is_login_rate_limited(settings: Settings, client_key: str) -> bool:
    key = f"login_fail:{client_key}"
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        attempts = await client.get(key)
        await client.aclose()
        if attempts is None:
            return False
        return int(attempts) >= settings.login_rate_limit_attempts
    except Exception:
        if settings.is_prod:
            raise RateLimitUnavailable from None
        logger.warning("Rate limit Redis недоступен, проверка пропущена")
        return False


# Проверяет лимит попыток регистрации.
async def is_register_rate_limited(settings: Settings, client_key: str) -> bool:
    key = f"register:{client_key}"
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        attempts = await client.get(key)
        await client.aclose()
        if attempts is None:
            return False
        return int(attempts) >= settings.register_rate_limit_attempts
    except Exception:
        if settings.is_prod:
            raise RateLimitUnavailable from None
        logger.warning("Rate limit Redis недоступен, проверка регистрации пропущена")
        return False


# Увеличивает счётчик попыток регистрации.
async def record_register_attempt(settings: Settings, client_key: str) -> None:
    key = f"register:{client_key}"
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, settings.register_rate_limit_window_seconds)
        await pipe.execute()
        await client.aclose()
    except Exception:
        if settings.is_prod:
            raise RateLimitUnavailable from None
        logger.warning("Не удалось записать попытку регистрации в Redis")


# Увеличивает счётчик неудачных входов.
async def record_login_failure(settings: Settings, client_key: str) -> None:
    key = f"login_fail:{client_key}"
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, settings.login_rate_limit_window_seconds)
        await pipe.execute()
        await client.aclose()
    except Exception:
        if settings.is_prod:
            raise RateLimitUnavailable from None
        logger.warning("Не удалось записать неудачный вход в Redis")


# Сбрасывает счётчик неудачных входов после успешного логина.
async def clear_login_failures(settings: Settings, client_key: str) -> None:
    key = f"login_fail:{client_key}"
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        await client.delete(key)
        await client.aclose()
    except Exception:
        logger.warning("Не удалось сбросить счётчик входов в Redis")


# Логинит и пишет сессию.
async def authenticate(
    db: AsyncSession,
    settings: Settings,
    login: str,
    password: str,
    client_key: str,
) -> tuple[str, LoginHit] | None:
    if await is_login_rate_limited(settings, client_key):
        logger.warning("Лимит входов для %s", client_key)
        return None
    hit = await find_login(db, login)
    if hit is None or not verify_password(password, hit.password_hash):
        logger.info("Неудачный вход: login=%s", login)
        await record_login_failure(settings, client_key)
        return None
    await clear_login_failures(settings, client_key)
    token = await create_session(db, settings, hit.subject_type, hit.subject_id)
    await touch_last_login(db, hit)
    await db.commit()
    return token, hit


# Отзывает все активные сессии клиентской учётки.
async def revoke_company_sessions(db: AsyncSession, account_id: UUID) -> None:
    now = datetime.now(UTC)
    await db.execute(
        update(Session)
        .where(
            Session.subject_type == SessionSubjectType.COMPANY.value,
            Session.subject_id == account_id,
            Session.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


# Отзывает сессию по plaintext-токену cookie.
async def revoke_token(db: AsyncSession, token: str | None) -> None:
    if not token:
        return
    row = await db.scalar(
        select(Session).where(
            Session.token_hash == hash_token(token),
            Session.revoked_at.is_(None),
        )
    )
    if row is None:
        return
    row.revoked_at = datetime.now(UTC)
    await db.commit()


# Меняет пароль клиента и снимает флаг обязательной смены.
async def change_company_password(
    db: AsyncSession,
    account_id: UUID,
    session_id: UUID,
    new_password: str,
    company_status: str,
) -> None:
    from b2b_commerce.companies.service import _validate_password, assert_can_change_credentials

    assert_can_change_credentials(company_status)
    _validate_password(new_password)
    account = await db.get(CompanyAccount, account_id)
    if account is None:
        raise ValueError("Учётка не найдена")
    account.password_hash = hash_password(new_password)
    account.must_change_password = False
    await db.execute(
        update(Session)
        .where(
            Session.subject_id == account_id,
            Session.id != session_id,
            Session.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    await db.commit()
