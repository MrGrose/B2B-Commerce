from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.deps import AuthContext, require_admin, require_admin_api
from b2b_commerce.db import get_session
from b2b_commerce.finance.service import (
    FINANCE_PERIODS,
    FinanceSummary,
    get_finance_summary,
)
from b2b_commerce.http import templates

html = APIRouter()
api = APIRouter()

_PERIOD_LABELS = (
    ("7d", "7 дней"),
    ("30d", "30 дней"),
    ("month", "Этот месяц"),
    ("all", "Всё время"),
)


# Сериализует строку разреза для JSON.
def _breakdown_json(row) -> dict:
    payload = {
        "name": row.name,
        "revenue": str(row.revenue),
        "cost": str(row.cost),
        "margin": str(row.margin),
    }
    if row.quantity is not None:
        payload["quantity"] = row.quantity
    return payload


# Сериализует сводку финансов для JSON.
def _finance_json(summary: FinanceSummary) -> dict:
    return {
        "period": summary.period,
        "paid_count": summary.paid_count,
        "paid_total": str(summary.paid_total),
        "shipped_count": summary.shipped_count,
        "shipped_revenue": str(summary.shipped_revenue),
        "shipped_cost": str(summary.shipped_cost),
        "shipped_margin": str(summary.shipped_margin),
        "shipped_margin_percent": (
            str(summary.shipped_margin_percent)
            if summary.shipped_margin_percent is not None
            else None
        ),
        "warehouse_stock_value": str(summary.warehouse_stock_value),
        "unpaid_count": summary.unpaid_count,
        "unpaid_total": str(summary.unpaid_total),
        "unpaid_invoices": [
            {
                "id": str(row.id),
                "number": row.number,
                "company_id": str(row.company_id),
                "company_name": row.company_name,
                "total": str(row.total),
                "created_at": row.created_at.isoformat(),
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            }
            for row in summary.unpaid_invoices
        ],
        "by_company": [_breakdown_json(row) for row in summary.by_company],
        "by_product": [_breakdown_json(row) for row in summary.by_product],
    }


# Нормализует query-параметр периода.
def _period_param(value: str) -> str:
    if value in FINANCE_PERIODS:
        return value
    return "30d"


# Финансовый дашборд по снимкам счетов.
@html.get("/admin/finance")
async def finance_page(
    request: Request,
    period: str = Query(default="30d"),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin),
):
    chosen = _period_param(period)
    summary = await get_finance_summary(db, chosen)
    return templates.TemplateResponse(
        request,
        "finance/dashboard.html",
        {
            "summary": summary,
            "period": chosen,
            "period_options": _PERIOD_LABELS,
        },
    )


# JSON: сводка финансов.
@api.get("/admin/finance")
async def api_finance(
    period: str = Query(default="30d"),
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    return _finance_json(await get_finance_summary(db, _period_param(period)))
