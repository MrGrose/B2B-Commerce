from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.audit.models import AuditLog

AUDIT_PAGE_SIZE = 30


# Пишет строку в audit_logs.
async def write_audit(
    db: AsyncSession,
    actor_type: str,
    actor_id: UUID | None,
    action: str,
    entity_type: str,
    entity_id: UUID | None,
    payload: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
        )
    )


# Считает записи журнала.
async def count_audit_logs(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(AuditLog)) or 0)


# Список audit_logs (пагинация через limit/offset).
async def list_audit_logs(
    db: AsyncSession,
    limit: int = AUDIT_PAGE_SIZE,
    offset: int = 0,
) -> list[AuditLog]:
    return list(
        await db.scalars(
            select(AuditLog)
            .order_by(AuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
