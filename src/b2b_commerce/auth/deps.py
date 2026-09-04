from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.models import Session
from b2b_commerce.auth.service import hash_token
from b2b_commerce.companies.models import Company, CompanyAccount
from b2b_commerce.config import Settings, get_settings
from b2b_commerce.db import get_session
from b2b_commerce.enums import CompanyStatus, SessionSubjectType


class LoginRequired(Exception):
    def __init__(self, json_mode: bool) -> None:
        self.json_mode = json_mode


class Forbidden(Exception):
    def __init__(self, json_mode: bool) -> None:
        self.json_mode = json_mode


class MustChangePassword(Exception):
    def __init__(self, json_mode: bool) -> None:
        self.json_mode = json_mode


class ApprovalRequired(Exception):
    def __init__(self, json_mode: bool, company_status: str | None) -> None:
        self.json_mode = json_mode
        self.company_status = company_status


@dataclass
class AuthContext:
    subject_type: SessionSubjectType
    subject_id: UUID
    session_id: UUID
    company_id: UUID | None
    must_change_password: bool
    company_status: str | None = None


# True, если запрос к JSON API.
def _json_mode(request: Request) -> bool:
    return request.url.path.startswith("/api")


# Достаёт действующую сессию из cookie.
async def current_auth(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    token = request.cookies.get(settings.session_cookie)
    if not token:
        raise LoginRequired(_json_mode(request))
    row = await db.scalar(
        select(Session).where(
            Session.token_hash == hash_token(token),
            Session.revoked_at.is_(None),
            Session.expires_at > datetime.now(UTC),
        )
    )
    if row is None:
        raise LoginRequired(_json_mode(request))
    subject_type = SessionSubjectType(row.subject_type)
    company_id = None
    must_change = False
    company_status = None
    if subject_type is SessionSubjectType.COMPANY:
        account = await db.get(CompanyAccount, row.subject_id)
        if account is None or not account.is_active:
            raise LoginRequired(_json_mode(request))
        company = await db.get(Company, account.company_id)
        if company is None or company.status == CompanyStatus.SUSPENDED.value:
            raise LoginRequired(_json_mode(request))
        company_id = account.company_id
        must_change = account.must_change_password
        company_status = company.status
    return AuthContext(
        subject_type=subject_type,
        subject_id=row.subject_id,
        session_id=row.id,
        company_id=company_id,
        must_change_password=must_change,
        company_status=company_status,
    )


# Пускает только админа.
async def require_admin(auth: AuthContext = Depends(current_auth)) -> AuthContext:
    if auth.subject_type is not SessionSubjectType.ADMIN:
        raise Forbidden(False)
    return auth


# Пускает клиента; при временном пароле гонит на смену.
async def require_company(
    request: Request,
    auth: AuthContext = Depends(current_auth),
) -> AuthContext:
    if auth.subject_type is not SessionSubjectType.COMPANY:
        raise Forbidden(_json_mode(request))
    allowed = {
        "/profile",
        "/change-password",
        "/pending",
        "/rejected",
        "/api/auth/change-password",
        "/api/auth/me",
    }
    if auth.must_change_password and request.url.path not in allowed:
        raise MustChangePassword(_json_mode(request))
    return auth


# Пускает только одобренную компанию к B2B-операциям.
async def require_approved_company(
    request: Request,
    auth: AuthContext = Depends(require_company),
) -> AuthContext:
    if auth.company_status != CompanyStatus.ACTIVE.value:
        raise ApprovalRequired(_json_mode(request), auth.company_status)
    return auth


# Пускает админа на JSON-маршрутах.
async def require_admin_api(
    request: Request,
    auth: AuthContext = Depends(current_auth),
) -> AuthContext:
    if auth.subject_type is not SessionSubjectType.ADMIN:
        raise Forbidden(_json_mode(request))
    return auth
