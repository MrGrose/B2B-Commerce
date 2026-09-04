from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.catalog.models import Product, ProductImage
from b2b_commerce.enums import InvoiceStatus, ProductStatus, ReservationStatus
from b2b_commerce.inventory.models import Inventory, InventoryReservation
from b2b_commerce.invoices.models import Invoice
from b2b_commerce.invoices.service import InvoiceView, list_all_invoices
from b2b_commerce.support.service import count_open_tickets

LOW_STOCK_THRESHOLD = 10
LOW_STOCK_PREVIEW_LIMIT = 4
RECENT_INVOICES_LIMIT = 4


@dataclass
class DashboardLowStockRow:
    product_id: UUID
    name: str
    brand_name: str | None
    image_storage_key: str | None
    available: int


@dataclass
class AdminDashboardView:
    awaiting_count: int
    awaiting_total: Decimal
    open_tickets: int
    low_stock_count: int
    recent_invoices: list[InvoiceView]
    low_stock: list[DashboardLowStockRow]


# SQL запрос для получения товаров с низким остатком.
def _low_stock_stmt(threshold: int):
    on_hand = func.coalesce(
        select(func.sum(Inventory.quantity_on_hand))
        .where(Inventory.product_id == Product.id)
        .correlate(Product)
        .scalar_subquery(),
        0,
    )
    reserved = func.coalesce(
        select(func.sum(InventoryReservation.quantity))
        .where(
            InventoryReservation.product_id == Product.id,
            InventoryReservation.status == ReservationStatus.ACTIVE.value,
        )
        .correlate(Product)
        .scalar_subquery(),
        0,
    )
    available = on_hand - reserved
    has_inventory = exists(select(1).where(Inventory.product_id == Product.id))
    return (
        select(
            Product.id,
            Product.name,
            Product.brand_name,
            available.label("available"),
        )
        .where(
            Product.deleted_at.is_(None),
            or_(
                Product.status == ProductStatus.ACTIVE.value,
                has_inventory,
            ),
            available < threshold,
        )
        .order_by(available.asc(), Product.name.asc())
    )


# Подсчитывает количество товаров с низким остатком.
async def _count_low_stock_products(db: AsyncSession, threshold: int) -> int:
    stmt = select(func.count()).select_from(_low_stock_stmt(threshold).subquery())
    return int(await db.scalar(stmt) or 0)


# Получает список товаров с низким остатком.
async def _list_low_stock_products(
    db: AsyncSession,
    threshold: int,
    limit: int,
) -> list[DashboardLowStockRow]:
    rows = (await db.execute(_low_stock_stmt(threshold).limit(limit))).all()
    if not rows:
        return []
    product_ids = [row[0] for row in rows]
    image_rows = (
        await db.execute(
            select(ProductImage.product_id, ProductImage.storage_key)
            .where(ProductImage.product_id.in_(product_ids))
            .order_by(ProductImage.product_id, ProductImage.sort_order)
        )
    ).all()
    image_by_product: dict[UUID, str] = {}
    for product_id, storage_key in image_rows:
        image_by_product.setdefault(product_id, storage_key)
    return [
        DashboardLowStockRow(
            product_id=row[0],
            name=row[1],
            brand_name=row[2],
            image_storage_key=image_by_product.get(row[0]),
            available=int(row[3]),
        )
        for row in rows
    ]


# Получает данные для дашборда администратора.
async def get_admin_dashboard(db: AsyncSession) -> AdminDashboardView:
    awaiting_count = int(
        await db.scalar(
            select(func.count())
            .select_from(Invoice)
            .where(Invoice.status == InvoiceStatus.AWAITING_PAYMENT.value)
        )
        or 0
    )
    awaiting_total = Decimal(
        await db.scalar(
            select(func.coalesce(func.sum(Invoice.total), 0)).where(
                Invoice.status == InvoiceStatus.AWAITING_PAYMENT.value
            )
        )
        or 0
    )
    recent_invoices, _ = await list_all_invoices(
        db,
        page_size=RECENT_INVOICES_LIMIT,
    )
    low_stock_count = await _count_low_stock_products(db, LOW_STOCK_THRESHOLD)
    low_stock = await _list_low_stock_products(
        db,
        LOW_STOCK_THRESHOLD,
        LOW_STOCK_PREVIEW_LIMIT,
    )
    return AdminDashboardView(
        awaiting_count=awaiting_count,
        awaiting_total=awaiting_total,
        open_tickets=await count_open_tickets(db),
        low_stock_count=low_stock_count,
        recent_invoices=recent_invoices,
        low_stock=low_stock,
    )
