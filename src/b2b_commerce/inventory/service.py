from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from b2b_commerce.audit.service import write_audit
from b2b_commerce.catalog.models import Product
from b2b_commerce.companies.models import Company
from b2b_commerce.enums import ReservationStatus, StockMovementType
from b2b_commerce.inventory.models import Inventory, InventoryReservation, StockMovement, Warehouse
from b2b_commerce.invoices.models import Invoice


@dataclass
class InventoryRow:
    product_id: UUID
    product_name: str
    warehouse_id: UUID
    warehouse_code: str
    quantity_on_hand: int
    reserved: int
    available: int


@dataclass(frozen=True)
class ProductStockRow:
    on_hand: int
    reserved: int
    available: int


@dataclass(frozen=True)
class ProductReservationRow:
    invoice_id: UUID
    invoice_number: str
    company_name: str
    quantity: int


# Возвращает склад MAIN или создаёт его.
async def get_default_warehouse(db: AsyncSession) -> Warehouse:
    warehouse = await db.scalar(select(Warehouse).where(Warehouse.is_default.is_(True)))
    if warehouse is None:
        warehouse = await db.scalar(select(Warehouse).where(Warehouse.code == "MAIN"))
    if warehouse is None:
        warehouse = Warehouse(code="MAIN", name="Основной", is_default=True)
        db.add(warehouse)
        await db.flush()
    return warehouse


# Строка остатка или None.
async def get_inventory_row(
    db: AsyncSession,
    product_id: UUID,
    warehouse_id: UUID,
) -> Inventory | None:
    return await db.scalar(
        select(Inventory).where(
            Inventory.product_id == product_id,
            Inventory.warehouse_id == warehouse_id,
        )
    )


# Создаёт строку остатка с нулём, если её ещё нет.
async def get_or_create_inventory(
    db: AsyncSession,
    product_id: UUID,
    warehouse_id: UUID,
) -> Inventory:
    row = await get_inventory_row(db, product_id, warehouse_id)
    if row is not None:
        return row
    row = Inventory(product_id=product_id, warehouse_id=warehouse_id, quantity_on_hand=0)
    db.add(row)
    await db.flush()
    return row


# Сумма активных резервов по товару.
async def reserved_quantity(db: AsyncSession, product_id: UUID) -> int:
    value = await db.scalar(
        select(func.coalesce(func.sum(InventoryReservation.quantity), 0)).where(
            InventoryReservation.product_id == product_id,
            InventoryReservation.status == ReservationStatus.ACTIVE,
        )
    )
    return int(value or 0)


# Доступный остаток: on_hand − active reservations.
async def get_availability(db: AsyncSession, product_id: UUID) -> int:
    on_hand = await db.scalar(
        select(func.coalesce(func.sum(Inventory.quantity_on_hand), 0)).where(
            Inventory.product_id == product_id
        )
    )
    return int(on_hand or 0) - await reserved_quantity(db, product_id)


# Доступный остаток для списка товаров одним batch-запросом.
async def list_availability(
    db: AsyncSession,
    product_ids: list[UUID],
) -> dict[UUID, int]:
    stock_rows = await list_admin_product_stock(db, product_ids)
    return {product_id: row.available for product_id, row in stock_rows.items()}


# Остатки для списка товаров в админке.
async def list_admin_product_stock(
    db: AsyncSession,
    product_ids: list[UUID],
) -> dict[UUID, ProductStockRow]:
    if not product_ids:
        return {}

    on_hand_rows = (
        await db.execute(
            select(
                Inventory.product_id,
                func.coalesce(func.sum(Inventory.quantity_on_hand), 0),
            )
            .where(Inventory.product_id.in_(product_ids))
            .group_by(Inventory.product_id)
        )
    ).all()
    on_hand_map = {row[0]: int(row[1]) for row in on_hand_rows}

    reserved_rows = (
        await db.execute(
            select(
                InventoryReservation.product_id,
                func.coalesce(func.sum(InventoryReservation.quantity), 0),
            )
            .where(
                InventoryReservation.product_id.in_(product_ids),
                InventoryReservation.status == ReservationStatus.ACTIVE,
            )
            .group_by(InventoryReservation.product_id)
        )
    ).all()
    reserved_map = {row[0]: int(row[1]) for row in reserved_rows}

    return {
        product_id: ProductStockRow(
            on_hand=on_hand_map.get(product_id, 0),
            reserved=reserved_map.get(product_id, 0),
            available=on_hand_map.get(product_id, 0) - reserved_map.get(product_id, 0),
        )
        for product_id in product_ids
    }


# Активные резервы товара по счетам.
async def list_active_reservations_for_product(
    db: AsyncSession,
    product_id: UUID,
) -> list[ProductReservationRow]:
    rows = (
        await db.execute(
            select(
                InventoryReservation.quantity,
                Invoice.id,
                Invoice.number,
                Company.name,
            )
            .select_from(InventoryReservation)
            .join(Invoice, InventoryReservation.invoice_id == Invoice.id)
            .join(Company, Invoice.company_id == Company.id)
            .where(
                InventoryReservation.product_id == product_id,
                InventoryReservation.status == ReservationStatus.ACTIVE,
            )
            .order_by(Invoice.created_at.desc())
        )
    ).all()
    return [
        ProductReservationRow(
            invoice_id=row[1],
            invoice_number=row[2],
            company_name=row[3],
            quantity=int(row[0]),
        )
        for row in rows
    ]


# Список остатков для админки.
async def list_inventory_rows(db: AsyncSession) -> list[InventoryRow]:
    rows = list(
        await db.scalars(
            select(Inventory).options(
                selectinload(Inventory.product),
                selectinload(Inventory.warehouse),
            )
        )
    )
    rows.sort(key=lambda row: row.product.name)
    stock_by_product = await list_admin_product_stock(db, [row.product_id for row in rows])
    result: list[InventoryRow] = []
    for row in rows:
        stock = stock_by_product.get(row.product_id)
        reserved = stock.reserved if stock is not None else 0
        available = stock.available if stock is not None else row.quantity_on_hand
        result.append(
            InventoryRow(
                product_id=row.product_id,
                product_name=row.product.name,
                warehouse_id=row.warehouse_id,
                warehouse_code=row.warehouse.code,
                quantity_on_hand=row.quantity_on_hand,
                reserved=reserved,
                available=available,
            )
        )
    return result


# Корректирует остаток до целевого значения.
async def correct_inventory(
    db: AsyncSession,
    product_id: UUID,
    target_quantity: int,
    reason: str,
    actor_id: UUID,
) -> Inventory:
    if target_quantity < 0:
        raise ValueError("Остаток не может быть отрицательным")
    cleaned_reason = reason.strip()
    if not cleaned_reason:
        raise ValueError("Укажите причину корректировки")
    product = await db.get(Product, product_id)
    if product is None:
        raise ValueError("Товар не найден")

    warehouse = await get_default_warehouse(db)
    inventory = await get_or_create_inventory(db, product_id, warehouse.id)
    reserved = await reserved_quantity(db, product_id)
    if target_quantity < reserved:
        raise ValueError(
            f"Нельзя установить остаток {target_quantity}: зарезервировано {reserved}"
        )
    delta = target_quantity - inventory.quantity_on_hand
    if delta == 0:
        return inventory

    movement_count = await db.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.product_id == product_id)
    )
    movement_type = (
        StockMovementType.INITIAL
        if inventory.quantity_on_hand == 0 and int(movement_count or 0) == 0
        else StockMovementType.CORRECTION
    )
    inventory.quantity_on_hand = target_quantity
    db.add(
        StockMovement(
            product_id=product_id,
            warehouse_id=warehouse.id,
            type=movement_type,
            delta=delta,
            reason=cleaned_reason,
            actor_type="admin",
            actor_id=actor_id,
        )
    )
    await write_audit(
        db,
        actor_type="admin",
        actor_id=actor_id,
        action="inventory.correction",
        entity_type="product",
        entity_id=product_id,
        payload={
            "warehouse_id": str(warehouse.id),
            "target_quantity": target_quantity,
            "delta": delta,
            "reason": cleaned_reason,
        },
    )
    await db.commit()
    await db.refresh(inventory)
    return inventory
