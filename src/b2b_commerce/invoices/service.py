import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from b2b_commerce.audit.service import write_audit
from b2b_commerce.cart.models import Cart, CartItem
from b2b_commerce.cart.service import clear_cart_items
from b2b_commerce.catalog.models import Product
from b2b_commerce.catalog.service import list_active_products
from b2b_commerce.companies.models import BillingEntity, Company
from b2b_commerce.config import Settings
from b2b_commerce.enums import (
    InvoiceStatus,
    PaymentStatus,
    ProductStatus,
    ReservationStatus,
    StockMovementType,
)
from b2b_commerce.inventory.models import Inventory, InventoryReservation, StockMovement
from b2b_commerce.inventory.service import (
    get_availability,
    get_default_warehouse,
    get_or_create_inventory,
    list_availability,
)
from b2b_commerce.invoices.models import IdempotencyKey, Invoice, InvoiceItem
from b2b_commerce.notifications.service import (
    emit_invoice_expired,
    emit_invoice_paid,
    emit_invoice_shipped,
)
from b2b_commerce.payments.models import Payment

INVOICES_PAGE_SIZE = 30


@dataclass(frozen=True, slots=True)
class AddableInvoiceProduct:
    id: UUID
    name: str
    available: int


async def list_addable_invoice_products(db: AsyncSession) -> list[AddableInvoiceProduct]:
    products = await list_active_products(db)
    if not products:
        return []
    availability = await list_availability(db, [product.id for product in products])
    return [
        AddableInvoiceProduct(
            id=product.id,
            name=product.name,
            available=max(availability.get(product.id, 0), 0),
        )
        for product in products
    ]


logger = logging.getLogger(__name__)

MOSCOW = ZoneInfo("Europe/Moscow")


def _is_business_day(day: datetime) -> bool:
    return day.weekday() < 5


# Вычисляет expires_at: конец N-го рабочего дня после создания (день создания не считается).
# Рабочий день = пн–пт по календарю MSK; праздники РФ не учитываются.
def compute_invoice_expires_at(
    created_at: datetime,
    business_days: int = 2,
    tz: ZoneInfo = MOSCOW,
) -> datetime:
    if business_days < 1:
        raise ValueError("business_days must be >= 1")
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    current = created_at.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    added = 0
    while added < business_days:
        current += timedelta(days=1)
        if _is_business_day(current):
            added += 1
    end_local = current.replace(hour=23, minute=59, second=59, microsecond=999999)
    return end_local.astimezone(UTC)



@dataclass
class InvoiceItemView:
    id: UUID
    product_id: UUID
    product_name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


# Снимок реквизитов продавца на момент создания счёта.
@dataclass
class InvoiceSellerSnapshot:
    name: str | None
    legal_name: str | None
    inn: str | None
    kpp: str | None
    legal_address: str | None
    bank_name: str | None
    bik: str | None
    bank_account: str | None
    corr_account: str | None


# Снимок реквизитов покупателя и адреса доставки.
@dataclass
class InvoiceBuyerSnapshot:
    name: str | None
    legal_name: str | None
    inn: str | None
    kpp: str | None
    legal_address: str | None
    contact_phone: str | None
    contact_email: str | None
    recipient_address: str | None


@dataclass
class InvoiceView:
    id: UUID
    company_id: UUID
    company_name: str
    number: str
    status: str
    subtotal: Decimal
    total: Decimal
    notes: str | None
    payment_instructions: str | None
    created_at: datetime
    expires_at: datetime | None
    paid_at: datetime | None
    shipped_at: datetime | None
    canceled_at: datetime | None
    items: list[InvoiceItemView]
    seller: InvoiceSellerSnapshot
    buyer: InvoiceBuyerSnapshot


# Выдаёт следующий порядковый номер счёта.
async def _next_invoice_number(db: AsyncSession) -> str:
    value = await db.scalar(text("SELECT nextval('invoice_number_seq')"))
    return str(int(value))


# Возвращает сохранённый результат идемпотентной операции.
async def _idempotency_entity_id(db: AsyncSession, key: str) -> UUID | None:
    row = await db.get(IdempotencyKey, key)
    if row is None:
        return None
    return row.entity_id


# Сохраняет ключ идемпотентности.
async def _store_idempotency(
    db: AsyncSession,
    key: str,
    operation: str,
    entity_id: UUID,
) -> None:
    db.add(IdempotencyKey(key=key, operation=operation, entity_id=entity_id))



# Следующий sort_order для новой строки счёта (append в конец).
async def _next_invoice_item_sort_order(db: AsyncSession, invoice_id: UUID) -> int:
    current = await db.scalar(
        select(func.coalesce(func.max(InvoiceItem.sort_order), 0)).where(
            InvoiceItem.invoice_id == invoice_id
        )
    )
    return int(current or 0) + 1


# Собирает InvoiceView из ORM и уже загруженных позиций.
def _build_invoice_view(
    invoice: Invoice,
    items: list[InvoiceItem],
    company_name: str,
) -> InvoiceView:
    return InvoiceView(
        id=invoice.id,
        company_id=invoice.company_id,
        company_name=company_name,
        number=invoice.number,
        status=invoice.status,
        subtotal=invoice.subtotal,
        total=invoice.total,
        notes=invoice.notes,
        payment_instructions=invoice.payment_instructions,
        created_at=invoice.created_at,
        expires_at=invoice.expires_at,
        paid_at=invoice.paid_at,
        shipped_at=invoice.shipped_at,
        canceled_at=invoice.canceled_at,
        items=[
            InvoiceItemView(
                id=item.id,
                product_id=item.product_id,
                product_name=item.product_name_snapshot,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
            )
            for item in items
        ],
        seller=InvoiceSellerSnapshot(
            name=invoice.seller_name,
            legal_name=invoice.seller_legal_name,
            inn=invoice.seller_inn,
            kpp=invoice.seller_kpp,
            legal_address=invoice.seller_legal_address,
            bank_name=invoice.seller_bank_name,
            bik=invoice.seller_bik,
            bank_account=invoice.seller_bank_account,
            corr_account=invoice.seller_corr_account,
        ),
        buyer=InvoiceBuyerSnapshot(
            name=invoice.buyer_name,
            legal_name=invoice.buyer_legal_name,
            inn=invoice.buyer_inn,
            kpp=invoice.buyer_kpp,
            legal_address=invoice.buyer_legal_address,
            contact_phone=invoice.buyer_contact_phone,
            contact_email=invoice.buyer_contact_email,
            recipient_address=invoice.recipient_address_snapshot,
        ),
    )


# Загружает позиции счетов одним запросом.
async def _invoice_items_by_invoice_id(
    db: AsyncSession,
    invoice_ids: list[UUID],
) -> dict[UUID, list[InvoiceItem]]:
    if not invoice_ids:
        return {}
    rows = (
        await db.scalars(
            select(InvoiceItem)
            .where(InvoiceItem.invoice_id.in_(invoice_ids))
            .order_by(InvoiceItem.invoice_id, InvoiceItem.sort_order, InvoiceItem.id)
        )
    ).all()
    grouped: dict[UUID, list[InvoiceItem]] = {}
    for item in rows:
        grouped.setdefault(item.invoice_id, []).append(item)
    return grouped


# Собирает список InvoiceView без N+1 по позициям.
async def _invoice_views_from_rows(
    db: AsyncSession,
    invoices: list[Invoice],
    *,
    company_names: dict[UUID, str] | None = None,
) -> list[InvoiceView]:
    if not invoices:
        return []
    if company_names is None:
        company_ids = {invoice.company_id for invoice in invoices}
        company_rows = await db.execute(
            select(Company.id, Company.name).where(Company.id.in_(company_ids))
        )
        company_names = {row[0]: row[1] for row in company_rows.all()}
    items_by_invoice = await _invoice_items_by_invoice_id(
        db, [invoice.id for invoice in invoices]
    )
    return [
        _build_invoice_view(
            invoice,
            items_by_invoice.get(invoice.id, []),
            company_names.get(invoice.company_id, "—"),
        )
        for invoice in invoices
    ]


# Собирает представление счёта с позициями.
async def _invoice_view(
    db: AsyncSession,
    invoice: Invoice,
    *,
    company_name: str | None = None,
) -> InvoiceView:
    items = (
        await db.scalars(
            select(InvoiceItem)
            .where(InvoiceItem.invoice_id == invoice.id)
            .order_by(InvoiceItem.sort_order, InvoiceItem.id)
        )
    ).all()
    if company_name is None:
        company = await db.get(Company, invoice.company_id)
        company_name = company.name if company else "—"
    return _build_invoice_view(invoice, list(items), company_name)


# Считает счета компании.
async def count_company_invoices(db: AsyncSession, company_id: UUID) -> int:
    stmt = select(func.count()).select_from(Invoice).where(Invoice.company_id == company_id)
    return int(await db.scalar(stmt) or 0)


# Список счетов компании (page_size=None — все строки).
async def list_company_invoices(
    db: AsyncSession,
    company_id: UUID,
    page: int = 1,
    page_size: int | None = INVOICES_PAGE_SIZE,
) -> tuple[list[InvoiceView], int]:
    total = await count_company_invoices(db, company_id)
    page = max(1, page)
    stmt = (
        select(Invoice)
        .where(Invoice.company_id == company_id)
        .order_by(Invoice.created_at.desc())
    )
    if page_size is not None:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    invoices = (await db.scalars(stmt)).all()
    if not invoices:
        return [], total
    company_ids = {invoice.company_id for invoice in invoices}
    company_rows = await db.execute(
        select(Company.id, Company.name).where(Company.id.in_(company_ids))
    )
    company_names = {row[0]: row[1] for row in company_rows.all()}
    return await _invoice_views_from_rows(
        db, list(invoices), company_names=company_names
    ), total


# Считает счета, созданные не раньше указанного момента.
async def count_invoices_created_since(db: AsyncSession, since: datetime) -> int:
    stmt = (
        select(func.count())
        .select_from(Invoice)
        .where(
            Invoice.created_at >= since,
            Invoice.status.in_(
                (
                    InvoiceStatus.AWAITING_PAYMENT.value,
                    InvoiceStatus.PAID.value,
                )
            ),
        )
    )
    return int(await db.scalar(stmt) or 0)


async def count_invoices_by_status(
    db: AsyncSession,
    created_since: datetime | None = None,
) -> dict[str, int]:
    stmt = select(Invoice.status, func.count()).select_from(Invoice).group_by(Invoice.status)
    if created_since is not None:
        stmt = stmt.where(Invoice.created_at >= created_since)
    rows = (await db.execute(stmt)).all()
    by_status = {status: int(count) for status, count in rows}
    return {
        "all": sum(by_status.values()),
        "awaiting_payment": by_status.get(InvoiceStatus.AWAITING_PAYMENT.value, 0),
        "paid": by_status.get(InvoiceStatus.PAID.value, 0),
        "shipped": by_status.get(InvoiceStatus.SHIPPED.value, 0),
        "canceled": by_status.get(InvoiceStatus.CANCELED.value, 0),
        "expired": by_status.get(InvoiceStatus.EXPIRED.value, 0),
    }


# Считает счета для админского списка.
async def count_all_invoices(
    db: AsyncSession,
    status: str | None = None,
    created_since: datetime | None = None,
) -> int:
    stmt = select(func.count()).select_from(Invoice)
    if status is not None:
        stmt = stmt.where(Invoice.status == status)
    if created_since is not None:
        stmt = stmt.where(Invoice.created_at >= created_since)
    return int(await db.scalar(stmt) or 0)


# Список всех счетов для админки (page_size=None — все строки).
async def list_all_invoices(
    db: AsyncSession,
    status: str | None = None,
    page: int = 1,
    page_size: int | None = INVOICES_PAGE_SIZE,
    created_since: datetime | None = None,
) -> tuple[list[InvoiceView], int]:
    total = await count_all_invoices(
        db, status=status, created_since=created_since
    )
    page = max(1, page)
    stmt = select(Invoice).order_by(Invoice.created_at.desc())
    if status is not None:
        stmt = stmt.where(Invoice.status == status)
    if created_since is not None:
        stmt = stmt.where(Invoice.created_at >= created_since)
    if page_size is not None:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    invoices = (await db.scalars(stmt)).all()
    return await _invoice_views_from_rows(db, list(invoices)), total


# Карточка счёта компании.
async def get_company_invoice(
    db: AsyncSession,
    company_id: UUID,
    invoice_id: UUID,
) -> InvoiceView | None:
    invoice = await db.get(Invoice, invoice_id)
    if invoice is None or invoice.company_id != company_id:
        return None
    return await _invoice_view(db, invoice)


# Карточка счёта для админки.
async def get_invoice(db: AsyncSession, invoice_id: UUID) -> InvoiceView | None:
    invoice = await db.get(Invoice, invoice_id)
    if invoice is None:
        return None
    return await _invoice_view(db, invoice)


# Создаёт счёт из корзины с резервом остатков.
async def create_invoice_from_cart(
    db: AsyncSession,
    company_id: UUID,
    actor_id: UUID,
    settings: Settings,
    idempotency_key: str | None = None,
    notes: str | None = None,
) -> InvoiceView:
    if idempotency_key:
        existing = await db.scalar(
            select(Invoice).where(Invoice.create_idempotency_key == idempotency_key)
        )
        if existing is not None:
            return await _invoice_view(db, existing)
        stored_id = await _idempotency_entity_id(db, idempotency_key)
        if stored_id is not None:
            invoice = await db.get(Invoice, stored_id)
            if invoice is not None:
                return await _invoice_view(db, invoice)

    cart = await db.scalar(
        select(Cart).where(Cart.company_id == company_id).with_for_update()
    )
    if cart is None:
        cart = Cart(company_id=company_id)
        db.add(cart)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            cart = await db.scalar(
                select(Cart).where(Cart.company_id == company_id).with_for_update()
            )
            if cart is None:
                raise

    cart_items = (
        await db.scalars(
            select(CartItem)
            .options(selectinload(CartItem.product))
            .where(CartItem.cart_id == cart.id)
        )
    ).all()
    if not cart_items:
        if idempotency_key:
            existing = await db.scalar(
                select(Invoice).where(Invoice.create_idempotency_key == idempotency_key)
            )
            if existing is not None:
                return await _invoice_view(db, existing)
            stored_id = await _idempotency_entity_id(db, idempotency_key)
            if stored_id is not None:
                winner = await db.get(Invoice, stored_id)
                if winner is not None:
                    return await _invoice_view(db, winner)
        raise ValueError("Корзина пуста")

    company = await db.get(Company, company_id)
    if company is None:
        raise ValueError("Компания не найдена")
    if company.billing_entity_id is None:
        raise ValueError("Нельзя выставить счёт: у компании не указано юрлицо поставщика")
    seller = await db.get(BillingEntity, company.billing_entity_id)
    if seller is None:
        raise ValueError("Юрлицо поставщика не найдено")

    warehouse = await get_default_warehouse(db)
    product_ids = sorted({item.product_id for item in cart_items})
    for product_id in product_ids:
        await get_or_create_inventory(db, product_id, warehouse.id)

    await db.scalars(
        select(Inventory)
        .where(
            Inventory.warehouse_id == warehouse.id,
            Inventory.product_id.in_(product_ids),
        )
        .order_by(Inventory.product_id, Inventory.warehouse_id)
        .with_for_update()
    )

    for item in cart_items:
        if item.product.deleted_at is not None or item.product.status != ProductStatus.ACTIVE:
            raise ValueError(f"Товар {item.product.name} недоступен")
        available = await get_availability(db, item.product_id)
        if item.quantity > available:
            raise ValueError(f"Недостаточно товара: {item.product.name}")

    now = datetime.now(UTC)
    subtotal = Decimal("0")
    for item in cart_items:
        subtotal += item.product.sale_price * item.quantity

    invoice = Invoice(
        company_id=company_id,
        number=await _next_invoice_number(db),
        status=InvoiceStatus.AWAITING_PAYMENT.value,
        subtotal=subtotal,
        total=subtotal,
        notes=notes or None,
        payment_instructions=_payment_instructions_from_entity(seller),
        expires_at=compute_invoice_expires_at(
            now, business_days=settings.invoice_ttl_business_days
        ),
        create_idempotency_key=idempotency_key,
        seller_name=seller.name,
        seller_legal_name=seller.legal_name,
        seller_inn=seller.inn,
        seller_kpp=seller.kpp,
        seller_legal_address=seller.legal_address,
        seller_bank_name=seller.bank_name,
        seller_bik=seller.bik,
        seller_bank_account=seller.bank_account,
        seller_corr_account=seller.corr_account,
        buyer_name=company.name,
        buyer_legal_name=company.legal_name,
        buyer_inn=company.inn,
        buyer_kpp=company.kpp,
        buyer_legal_address=company.legal_address,
        buyer_contact_phone=company.contact_phone,
        buyer_contact_email=company.contact_email,
        recipient_address_snapshot=company.delivery_address,
    )
    db.add(invoice)
    await db.flush()

    for sort_order, item in enumerate(cart_items, start=1):
        product = item.product
        line_total = product.sale_price * item.quantity
        db.add(
            InvoiceItem(
                invoice_id=invoice.id,
                product_id=product.id,
                warehouse_id=warehouse.id,
                sort_order=sort_order,
                quantity=item.quantity,
                unit_price=product.sale_price,
                cost_price_snapshot=product.cost_price,
                line_total=line_total,
                product_name_snapshot=product.name,
            )
        )
        db.add(
            InventoryReservation(
                invoice_id=invoice.id,
                product_id=product.id,
                warehouse_id=warehouse.id,
                quantity=item.quantity,
                status=ReservationStatus.ACTIVE.value,
            )
        )

    await clear_cart_items(db, cart.id)
    if idempotency_key:
        await _store_idempotency(db, idempotency_key, "invoice.create", invoice.id)
    await write_audit(
        db,
        actor_type="company",
        actor_id=actor_id,
        action="invoice.create",
        entity_type="invoice",
        entity_id=invoice.id,
        payload={"number": invoice.number, "total": str(invoice.total)},
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if idempotency_key:
            existing = await db.scalar(
                select(Invoice).where(Invoice.create_idempotency_key == idempotency_key)
            )
            if existing is not None:
                return await _invoice_view(db, existing)
            stored_id = await _idempotency_entity_id(db, idempotency_key)
            if stored_id is not None:
                winner = await db.get(Invoice, stored_id)
                if winner is not None:
                    return await _invoice_view(db, winner)
        raise
    await db.refresh(invoice)
    logger.info("Создан счёт %s для компании %s", invoice.number, company_id)
    return await _invoice_view(db, invoice)



# Проверяет, что счёт можно править (статус, компания, срок).
def _ensure_invoice_editable(
    invoice: Invoice,
    company_id: UUID | None = None,
) -> None:
    if company_id is not None and invoice.company_id != company_id:
        raise ValueError("Счёт не найден")
    if invoice.status != InvoiceStatus.AWAITING_PAYMENT.value:
        raise ValueError("Редактирование доступно только для счёта awaiting_payment")
    now = datetime.now(UTC)
    if invoice.expires_at is not None and invoice.expires_at <= now:
        raise ValueError("Срок редактирования счёта истёк")


# Меняет позиции awaiting_payment: qty, удаление, добавление; expires_at не трогает.
async def mutate_invoice_items(
    db: AsyncSession,
    invoice_id: UUID,
    actor_type: str,
    actor_id: UUID,
    company_id: UUID | None = None,
    quantities: dict[UUID, int] | None = None,
    additions: dict[UUID, int] | None = None,
) -> InvoiceView | None:
    invoice = await db.scalar(
        select(Invoice).where(Invoice.id == invoice_id).with_for_update()
    )
    if invoice is None:
        return None
    _ensure_invoice_editable(invoice, company_id=company_id)

    quantities = quantities or {}
    additions = additions or {}
    if not quantities and not additions:
        return await _invoice_view(db, invoice)

    items = list(
        await db.scalars(
            select(InvoiceItem)
            .where(InvoiceItem.invoice_id == invoice_id)
            .order_by(InvoiceItem.sort_order, InvoiceItem.id)
            .with_for_update()
        )
    )
    if not items and not additions:
        raise ValueError("В счёте нет позиций")

    if quantities:
        unknown = set(quantities) - {item.id for item in items}
        if unknown:
            raise ValueError("Неизвестная позиция счёта")
        planned = {
            item.id: quantities[item.id] if item.id in quantities else item.quantity
            for item in items
        }
        if any(qty < 0 for qty in planned.values()):
            raise ValueError("Количество не может быть отрицательным")
        if items and all(qty == 0 for qty in planned.values()) and not additions:
            raise ValueError("В счёте должна остаться хотя бы одна позиция")
    else:
        planned = {item.id: item.quantity for item in items}

    product_ids = sorted({item.product_id for item in items} | set(additions))
    warehouse = await get_default_warehouse(db)
    for product_id in product_ids:
        await get_or_create_inventory(db, product_id, warehouse.id)
    await db.scalars(
        select(Inventory)
        .where(
            Inventory.warehouse_id == warehouse.id,
            Inventory.product_id.in_(product_ids),
        )
        .order_by(Inventory.product_id, Inventory.warehouse_id)
        .with_for_update()
    )
    reservations = list(
        await db.scalars(
            select(InventoryReservation)
            .where(
                InventoryReservation.invoice_id == invoice_id,
                InventoryReservation.status == ReservationStatus.ACTIVE.value,
            )
            .order_by(
                InventoryReservation.product_id,
                InventoryReservation.warehouse_id,
            )
            .with_for_update()
        )
    )
    reservation_by_product = {row.product_id: row for row in reservations}

    now = datetime.now(UTC)
    changed = False
    for item in items:
        new_qty = planned[item.id]
        reservation = reservation_by_product.get(item.product_id)
        if reservation is None:
            raise ValueError(f"Нет активного резерва для {item.product_name_snapshot}")
        if new_qty == item.quantity:
            continue
        if new_qty == 0:
            reservation.status = ReservationStatus.RELEASED.value
            reservation.released_at = now
            await db.delete(item)
            changed = True
            continue
        if new_qty > item.quantity:
            extra = new_qty - item.quantity
            available = await get_availability(db, item.product_id)
            if extra > available:
                raise ValueError(f"Недостаточно товара: {item.product_name_snapshot}")
        item.quantity = new_qty
        item.line_total = item.unit_price * new_qty
        reservation.quantity = new_qty
        changed = True

    items_by_product = {
        row.product_id: row
        for row in (
            await db.scalars(
                select(InvoiceItem)
                .where(InvoiceItem.invoice_id == invoice_id)
                .order_by(InvoiceItem.sort_order, InvoiceItem.id)
            )
        ).all()
    }

    for product_id, add_qty in additions.items():
        if add_qty <= 0:
            raise ValueError("Количество должно быть больше нуля")
        existing = items_by_product.get(product_id)
        if existing is not None:
            target_qty = existing.quantity + add_qty
            available = await get_availability(db, product_id)
            if target_qty - existing.quantity > available:
                raise ValueError(f"Недостаточно товара: {existing.product_name_snapshot}")
            reservation = reservation_by_product.get(product_id)
            if reservation is None:
                raise ValueError(f"Нет активного резерва для {existing.product_name_snapshot}")
            existing.quantity = target_qty
            existing.line_total = existing.unit_price * target_qty
            reservation.quantity = target_qty
            changed = True
            continue

        product = await db.get(Product, product_id)
        if product is None or product.deleted_at is not None:
            raise ValueError("Товар не найден")
        if product.status != ProductStatus.ACTIVE:
            raise ValueError(f"Товар {product.name} недоступен")
        available = await get_availability(db, product_id)
        if add_qty > available:
            raise ValueError(f"Недостаточно товара: {product.name}")
        line_total = product.sale_price * add_qty
        sort_order = await _next_invoice_item_sort_order(db, invoice_id)
        db.add(
            InvoiceItem(
                invoice_id=invoice_id,
                product_id=product.id,
                warehouse_id=warehouse.id,
                sort_order=sort_order,
                quantity=add_qty,
                unit_price=product.sale_price,
                cost_price_snapshot=product.cost_price,
                line_total=line_total,
                product_name_snapshot=product.name,
            )
        )
        db.add(
            InventoryReservation(
                invoice_id=invoice_id,
                product_id=product.id,
                warehouse_id=warehouse.id,
                quantity=add_qty,
                status=ReservationStatus.ACTIVE.value,
            )
        )
        changed = True

    await db.flush()
    remaining = list(
        await db.scalars(
            select(InvoiceItem)
            .where(InvoiceItem.invoice_id == invoice_id)
            .order_by(InvoiceItem.sort_order, InvoiceItem.id)
        )
    )
    if not remaining:
        raise ValueError("В счёте должна остаться хотя бы одна позиция")
    subtotal = sum((row.line_total for row in remaining), Decimal("0"))
    invoice.subtotal = subtotal
    invoice.total = subtotal
    if changed:
        action = (
            "invoice.update_items"
            if actor_type == "admin"
            else "invoice.update_items_by_company"
        )
        await write_audit(
            db,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type="invoice",
            entity_id=invoice_id,
            payload={"number": invoice.number, "total": str(invoice.total)},
        )
        logger.info("Обновлены позиции счёта %s", invoice.number)
    await db.commit()
    await db.refresh(invoice)
    return await _invoice_view(db, invoice)


# Правит позиции awaiting_payment: кол-во вниз снимает резерв, вверх — добирает, 0 удаляет строку.
async def update_invoice_items(
    db: AsyncSession,
    invoice_id: UUID,
    admin_id: UUID,
    quantities: dict[UUID, int],
    additions: dict[UUID, int] | None = None,
) -> InvoiceView | None:
    return await mutate_invoice_items(
        db,
        invoice_id,
        actor_type="admin",
        actor_id=admin_id,
        quantities=quantities,
        additions=additions,
    )


# Клиент правит позиции своего awaiting_payment счёта до expires_at.
async def update_invoice_items_by_company(
    db: AsyncSession,
    invoice_id: UUID,
    company_id: UUID,
    actor_id: UUID,
    quantities: dict[UUID, int] | None = None,
    additions: dict[UUID, int] | None = None,
) -> InvoiceView | None:
    return await mutate_invoice_items(
        db,
        invoice_id,
        actor_type="company",
        actor_id=actor_id,
        company_id=company_id,
        quantities=quantities,
        additions=additions,
    )

# Отменяет счёт до оплаты и снимает резерв.
async def cancel_invoice(
    db: AsyncSession,
    invoice_id: UUID,
    actor_type: str,
    actor_id: UUID,
    company_id: UUID | None = None,
) -> InvoiceView | None:
    invoice = await db.scalar(
        select(Invoice).where(Invoice.id == invoice_id).with_for_update()
    )
    if invoice is None:
        return None
    if company_id is not None and invoice.company_id != company_id:
        return None
    if invoice.status != InvoiceStatus.AWAITING_PAYMENT.value:
        raise ValueError("Счёт не может быть отменён в текущем статусе")

    now = datetime.now(UTC)
    invoice.status = InvoiceStatus.CANCELED.value
    invoice.canceled_at = now
    await db.execute(
        update(InventoryReservation)
        .where(
            InventoryReservation.invoice_id == invoice_id,
            InventoryReservation.status == ReservationStatus.ACTIVE.value,
        )
        .values(status=ReservationStatus.RELEASED.value, released_at=now)
    )
    await write_audit(
        db,
        actor_type=actor_type,
        actor_id=actor_id,
        action="invoice.cancel",
        entity_type="invoice",
        entity_id=invoice_id,
        payload={"number": invoice.number},
    )
    await db.commit()
    await db.refresh(invoice)
    return await _invoice_view(db, invoice)


# Подтверждает оплату счёта админом.
async def confirm_payment(
    db: AsyncSession,
    invoice_id: UUID,
    admin_id: UUID,
    idempotency_key: str | None = None,
) -> InvoiceView | None:
    if idempotency_key:
        stored_id = await _idempotency_entity_id(db, idempotency_key)
        if stored_id is not None:
            invoice = await db.get(Invoice, stored_id)
            if invoice is not None:
                return await _invoice_view(db, invoice)

    invoice = await db.scalar(
        select(Invoice).where(Invoice.id == invoice_id).with_for_update()
    )
    if invoice is None:
        return None
    if invoice.status != InvoiceStatus.AWAITING_PAYMENT.value:
        raise ValueError("Оплата возможна только для счёта awaiting_payment")

    existing_payment = await db.scalar(
        select(Payment).where(
            Payment.invoice_id == invoice_id,
            Payment.status == PaymentStatus.PAID.value,
        )
    )
    if existing_payment is not None:
        return await _invoice_view(db, invoice)

    now = datetime.now(UTC)
    db.add(
        Payment(
            invoice_id=invoice_id,
            amount=invoice.total,
            status=PaymentStatus.PAID.value,
            confirmed_by=admin_id,
            confirmed_at=now,
            idempotency_key=idempotency_key,
        )
    )
    invoice.status = InvoiceStatus.PAID.value
    invoice.paid_at = now
    if idempotency_key:
        await _store_idempotency(db, idempotency_key, "invoice.confirm_payment", invoice_id)
    await write_audit(
        db,
        actor_type="admin",
        actor_id=admin_id,
        action="invoice.pay",
        entity_type="invoice",
        entity_id=invoice_id,
        payload={"number": invoice.number},
    )
    await emit_invoice_paid(db, invoice)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        invoice = await db.get(Invoice, invoice_id)
        if invoice is None:
            return None
        return await _invoice_view(db, invoice)
    await db.refresh(invoice)
    return await _invoice_view(db, invoice)


# Отгружает оплаченный счёт и списывает остаток.
async def ship_invoice(
    db: AsyncSession,
    invoice_id: UUID,
    admin_id: UUID,
) -> InvoiceView | None:
    invoice = await db.scalar(
        select(Invoice).where(Invoice.id == invoice_id).with_for_update()
    )
    if invoice is None:
        return None
    if invoice.status == InvoiceStatus.SHIPPED.value:
        return await _invoice_view(db, invoice)
    if invoice.status != InvoiceStatus.PAID.value:
        raise ValueError("Отгрузка возможна только для оплаченного счёта")

    items = (
        await db.scalars(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id))
    ).all()
    if not items:
        raise ValueError("Счёт не содержит позиций для отгрузки")
    product_ids = sorted({item.product_id for item in items})
    warehouse_ids = sorted({item.warehouse_id for item in items})

    await db.scalars(
        select(Inventory)
        .where(
            Inventory.product_id.in_(product_ids),
            Inventory.warehouse_id.in_(warehouse_ids),
        )
        .order_by(Inventory.product_id, Inventory.warehouse_id)
        .with_for_update()
    )

    active_reservations = (
        await db.scalars(
            select(InventoryReservation).where(
                InventoryReservation.invoice_id == invoice_id,
                InventoryReservation.status == ReservationStatus.ACTIVE.value,
            )
        )
    ).all()
    reservation_by_key = {
        (row.product_id, row.warehouse_id): row for row in active_reservations
    }
    for item in items:
        reservation = reservation_by_key.get((item.product_id, item.warehouse_id))
        if reservation is None:
            raise ValueError(
                f"Нет активного резерва для отгрузки: {item.product_name_snapshot}"
            )
        if reservation.quantity != item.quantity:
            raise ValueError(
                "Резерв не совпадает с позицией счёта: "
                f"{item.product_name_snapshot}"
            )

    now = datetime.now(UTC)
    for item in items:
        reservation = reservation_by_key[(item.product_id, item.warehouse_id)]
        reservation.status = ReservationStatus.CONSUMED.value
        reservation.consumed_at = now
        delta = -item.quantity
        inventory = await db.scalar(
            select(Inventory).where(
                Inventory.product_id == item.product_id,
                Inventory.warehouse_id == item.warehouse_id,
            )
        )
        if inventory is not None:
            inventory.quantity_on_hand += delta
        db.add(
            StockMovement(
                product_id=item.product_id,
                warehouse_id=item.warehouse_id,
                type=StockMovementType.SHIPMENT.value,
                delta=delta,
                invoice_id=invoice_id,
                reason=f"Отгрузка счёта {invoice.number}",
                actor_type="admin",
                actor_id=admin_id,
            )
        )

    invoice.status = InvoiceStatus.SHIPPED.value
    invoice.shipped_at = now
    await write_audit(
        db,
        actor_type="admin",
        actor_id=admin_id,
        action="invoice.ship",
        entity_type="invoice",
        entity_id=invoice_id,
        payload={"number": invoice.number},
    )
    await emit_invoice_shipped(db, invoice)
    await db.commit()
    await db.refresh(invoice)
    return await _invoice_view(db, invoice)


# Переводит просроченные awaiting_payment в expired и снимает резервы.
async def expire_due_invoices(db: AsyncSession) -> int:
    now = datetime.now(UTC)
    result = await db.execute(
        select(Invoice.id).where(
            Invoice.status == InvoiceStatus.AWAITING_PAYMENT.value,
            Invoice.expires_at <= now,
        ).with_for_update(skip_locked=True)
    )
    ids = list(result.scalars().all())
    if not ids:
        return 0

    expired_count = 0
    for invoice_id in ids:
        expire_key = f"expire:{invoice_id}"
        if await _idempotency_entity_id(db, expire_key) is not None:
            continue
        invoice = await db.get(Invoice, invoice_id)
        if invoice is None or invoice.status != InvoiceStatus.AWAITING_PAYMENT.value:
            continue
        invoice.status = InvoiceStatus.EXPIRED.value
        await db.execute(
            update(InventoryReservation)
            .where(
                InventoryReservation.invoice_id == invoice_id,
                InventoryReservation.status == ReservationStatus.ACTIVE.value,
            )
            .values(status=ReservationStatus.RELEASED.value, released_at=now)
        )
        await _store_idempotency(db, expire_key, "invoice.expire", invoice_id)
        await emit_invoice_expired(db, invoice)
        expired_count += 1

    if expired_count:
        logger.info("Истекло счетов: %s", expired_count)
    return expired_count

# Собирает текст реквизитов из полей юрлица.
def _format_payment_instructions(
    legal_name: str,
    inn: str,
    kpp: str | None,
    legal_address: str | None,
    bank_name: str | None,
    bik: str | None,
    bank_account: str | None,
    corr_account: str | None,
) -> str:
    return (
        f"Получатель: {legal_name}\n"
        f"ИНН {inn} / КПП {kpp or ''}\n"
        f"Адрес: {legal_address or ''}\n"
        f"Банк: {bank_name or ''}\n"
        f"БИК {bik or ''}\n"
        f"Р/с {bank_account or ''}\n"
        f"К/с {corr_account or ''}\n"
        "Назначение платежа: оплата по счёту. Укажите номер счёта."
    )


# Собирает реквизиты поставщика из BillingEntity.
def _payment_instructions_from_entity(entity: BillingEntity) -> str:
    return _format_payment_instructions(
        legal_name=entity.legal_name,
        inn=entity.inn,
        kpp=entity.kpp,
        legal_address=entity.legal_address,
        bank_name=entity.bank_name,
        bik=entity.bik,
        bank_account=entity.bank_account,
        corr_account=entity.corr_account,
    )


# Собирает реквизиты поставщика из env (seed BillingEntity).
def build_payment_instructions(settings: Settings) -> str:
    return _format_payment_instructions(
        legal_name=settings.supplier_legal_name,
        inn=settings.supplier_inn,
        kpp=settings.supplier_kpp,
        legal_address=settings.supplier_legal_address,
        bank_name=settings.supplier_bank_name,
        bik=settings.supplier_bik,
        bank_account=settings.supplier_bank_account,
        corr_account=settings.supplier_corr_account,
    )
