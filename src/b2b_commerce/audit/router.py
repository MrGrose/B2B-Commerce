from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.audit.labels import (
    action_label,
    actor_type_label,
    entity_type_label,
    format_audit_log,
)
from b2b_commerce.audit.service import AUDIT_PAGE_SIZE, count_audit_logs, list_audit_logs
from b2b_commerce.auth.deps import AuthContext, require_admin, require_admin_api
from b2b_commerce.db import get_session
from b2b_commerce.http import templates

html = APIRouter()
api = APIRouter()


def _audit_total_pages(total: int) -> int:
    return max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)


# Сериализует запись аудита для JSON.
def _audit_json(row) -> dict:
    return {
        "id": row.id,
        "actor_type": row.actor_type,
        "actor_type_label": actor_type_label(row.actor_type),
        "actor_id": str(row.actor_id) if row.actor_id else None,
        "action": row.action,
        "action_label": action_label(row.action),
        "entity_type": row.entity_type,
        "entity_type_label": entity_type_label(row.entity_type),
        "entity_id": str(row.entity_id) if row.entity_id else None,
        "payload": row.payload,
        "created_at": row.created_at.isoformat(),
    }


# Журнал событий системы.
@html.get("/admin/audit")
async def audit_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    total = await count_audit_logs(db)
    total_pages = _audit_total_pages(total)
    current_page = min(page, total_pages)
    offset = (current_page - 1) * AUDIT_PAGE_SIZE
    rows = await list_audit_logs(db, limit=AUDIT_PAGE_SIZE, offset=offset)
    return templates.TemplateResponse(
        request,
        "audit/list.html",
        {
            "logs": [format_audit_log(row) for row in rows],
            "page": current_page,
            "total_pages": total_pages,
            "total": total,
        },
    )


# JSON: audit_logs.
@api.get("/admin/audit")
async def api_audit(
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    logs = await list_audit_logs(db, limit=limit, offset=offset)
    return [_audit_json(row) for row in logs]
