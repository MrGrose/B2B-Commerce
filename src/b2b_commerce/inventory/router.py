from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.deps import AuthContext, require_admin, require_admin_api
from b2b_commerce.db import get_session
from b2b_commerce.inventory.service import correct_inventory, list_inventory_rows

html = APIRouter()
api = APIRouter()


class InventoryCorrectionBody(BaseModel):
    product_id: UUID
    quantity: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


# Остатки склада — legacy URL, основной UX в списке товаров.
@html.get("/admin/inventory")
async def inventory_page(
    _request: Request,
    _auth: AuthContext = Depends(require_admin),
):
    return RedirectResponse("/admin/products", status_code=303)


# JSON: остатки.
@api.get("/admin/inventory")
async def api_inventory(
    db: AsyncSession = Depends(get_session),
    _auth: AuthContext = Depends(require_admin_api),
):
    rows = await list_inventory_rows(db)
    return [
        {
            "product_id": str(row.product_id),
            "product_name": row.product_name,
            "warehouse_id": str(row.warehouse_id),
            "warehouse_code": row.warehouse_code,
            "quantity_on_hand": row.quantity_on_hand,
            "reserved": row.reserved,
            "available": row.available,
        }
        for row in rows
    ]


# JSON: корректировка остатка.
@api.post("/admin/inventory/corrections")
async def api_correction(
    body: InventoryCorrectionBody,
    db: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_admin_api),
):
    try:
        inventory = await correct_inventory(
            db,
            body.product_id,
            body.quantity,
            body.reason,
            auth.subject_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "product_id": str(body.product_id),
        "quantity_on_hand": inventory.quantity_on_hand,
    }
