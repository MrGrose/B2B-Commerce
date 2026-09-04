from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.deps import AuthContext, require_admin, require_admin_api
from b2b_commerce.db import get_session
from b2b_commerce.http import templates
from b2b_commerce.rapira.service import (
    get_rapira_stats,
    list_rate_history,
    rate_history_context,
    sync_rapira_prices,
)

html = APIRouter()
api = APIRouter()


# Сериализует строку истории USD/RUB для JSON.
def _row_json(row) -> dict:
    return {
        "id": str(row.id),
        "pair": "USD/RUB",
        "rate": str(row.rate),
        "fetched_at": row.fetched_at.isoformat(),
    }


# Страница курса USD Rapira.
@html.get("/admin/rapira")
async def rapira_page(
    request: Request,
    synced: str | None = None,
    unchanged: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    history_rows = await list_rate_history(db)
    stats = await get_rapira_stats(db)
    return templates.TemplateResponse(
        request,
        "rapira/list.html",
        {
            "stats": stats,
            "synced": synced,
            "unchanged": unchanged,
            "error": error,
            **rate_history_context(history_rows),
        },
    )


# HTMX: подгрузка истории курса USD/RUB.
@html.get("/admin/rapira/history")
async def rapira_history_partial(
    request: Request,
    before_id: UUID | None = None,
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    rows = await list_rate_history(db, before_id=before_id)
    return templates.TemplateResponse(
        request,
        "rapira/partials/rate_history_rows.html",
        rate_history_context(rows),
    )


# Запускает синхронизацию курса USD из админки.
@html.post("/admin/rapira/sync")
async def rapira_sync_submit(
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    try:
        result = await sync_rapira_prices(db)
        await db.commit()
    except ValueError as exc:
        return RedirectResponse(
            f"/admin/rapira?error={quote(str(exc))}",
            status_code=303,
        )
    except Exception:
        await db.rollback()
        return RedirectResponse(
            "/admin/rapira?error=Сбой синхронизации Rapira",
            status_code=303,
        )
    if result.changed:
        return RedirectResponse(
            f"/admin/rapira?synced={result.rate}",
            status_code=303,
        )
    return RedirectResponse("/admin/rapira?unchanged=1", status_code=303)


# JSON: история курса USD (7 дней + cursor через before_id в HTML partial).
@api.get("/admin/rapira")
async def api_rapira(
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    rows = await list_rate_history(db)
    stats = await get_rapira_stats(db)
    return {
        "items": [_row_json(row) for row in rows],
        **rate_history_context(rows),
        "latest_rate": str(stats.latest_rate) if stats.latest_rate is not None else None,
        "last_changed_at": (
            stats.last_changed_at.isoformat() if stats.last_changed_at else None
        ),
    }


# JSON: запуск синхронизации курса USD.
@api.post("/admin/rapira/sync")
async def api_rapira_sync(
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    try:
        result = await sync_rapira_prices(db)
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail="Сбой синхронизации Rapira") from exc
    return {
        "pair": "USD/RUB",
        "rate": str(result.rate),
        "fetched_at": result.fetched_at.isoformat(),
        "changed": result.changed,
    }
