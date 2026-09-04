import asyncio
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from b2b_commerce.audit.models import AuditLog
from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.cart.service import get_cart_view, upsert_cart_item
from b2b_commerce.catalog.service import ProductInput, create_product, update_product
from b2b_commerce.companies.models import BillingEntity, Company
from b2b_commerce.companies.service import (
    BillingEntityInput,
    CompanyInput,
    create_billing_entity,
    create_company,
    update_billing_entity,
)
from b2b_commerce.config import Settings
from b2b_commerce.enums import InvoiceStatus, PaymentStatus, ProductStatus, ReservationStatus
from b2b_commerce.inventory.models import Inventory, InventoryReservation, StockMovement, Warehouse
from b2b_commerce.inventory.service import correct_inventory, get_availability, reserved_quantity
from b2b_commerce.invoices.models import Invoice, InvoiceItem
from b2b_commerce.invoices.pdf import invoice_export_values, render_invoice_pdf
from b2b_commerce.invoices.service import (
    cancel_invoice,
    confirm_payment,
    create_invoice_from_cart,
    expire_due_invoices,
    get_invoice,
    ship_invoice,
    update_invoice_items,
)
from b2b_commerce.payments.models import Payment


# Создаёт админа.
async def _seed_admin(db_session):
    admin = AdminUser(
        login="invoice-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


# Компания с юрлицом поставщика, товар и остаток.
async def _seed_company_product(db_session, stock: int = 10):
    admin = await _seed_admin(db_session)
    entity = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name="Seller IE",
            legal_name="ИП Продавец",
            inn="7701234567",
            kpp="770101001",
            legal_address="Москва, ул. Продавца, 1",
            bank_name="Тест Банк",
            bik="044525225",
            bank_account="40702810100000000001",
            corr_account="30101810400000000225",
        ),
        admin.id,
    )
    company = await create_company(
        db_session,
        CompanyInput(
            name="Invoice Co",
            legal_name="ООО Покупатель",
            inn="7707654321",
            contact_email="buyer@example.com",
            contact_phone="+79990000000",
            billing_entity_id=entity.id,
        ),
        admin.id,
    )
    row = await db_session.get(Company, company.company_id)
    assert row is not None
    row.legal_address = "Москва, ул. Покупателя, 2"
    row.delivery_address = "Склад получателя, 3"
    row.kpp = "770201001"
    await db_session.commit()
    warehouse = Warehouse(code="MAIN", name="Main", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()
    product = await create_product(
        db_session,
        ProductInput(
            name="Invoice Product",
            sale_price=Decimal("200"),
            cost_price=Decimal("80"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, stock, "seed", admin.id)
    return admin, company, product


# Снимок on_hand, резерва, available и числа движений.
async def _stock_state(db_session, product_id):
    inventory = await db_session.scalar(select(Inventory).where(Inventory.product_id == product_id))
    assert inventory is not None
    reserved = await reserved_quantity(db_session, product_id)
    available = await get_availability(db_session, product_id)
    movement_count = await db_session.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.product_id == product_id)
    )
    return inventory.quantity_on_hand, reserved, available, int(movement_count or 0)


# Счёт awaiting_payment с уже прошедшим expires_at.
async def _due_invoice(db_session, qty: int = 3, stock: int = 10):
    admin, company, product = await _seed_company_product(db_session, stock=stock)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, qty)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
    )
    row = await db_session.get(Invoice, invoice.id)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    return product, invoice.id


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_invoice_reserves_and_clears_cart(db_session):
    admin, company, product = await _seed_company_product(db_session)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 4)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
        idempotency_key="create-1",
    )
    assert invoice.status == InvoiceStatus.AWAITING_PAYMENT.value
    assert invoice.total == Decimal("800")
    assert len(invoice.items) == 1

    cart = await get_cart_view(db_session, company.company_id)
    assert cart.items == []

    reservations = (
        await db_session.scalars(
            select(InventoryReservation).where(InventoryReservation.invoice_id == invoice.id)
        )
    ).all()
    assert len(reservations) == 1
    assert reservations[0].quantity == 4


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_invoice_idempotency(db_session):
    admin, company, product = await _seed_company_product(db_session)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    first = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
        idempotency_key="same-key",
    )
    second = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
        idempotency_key="same-key",
    )
    assert first.id == second.id


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_invoice_insufficient_stock(db_session):
    admin, company, product = await _seed_company_product(db_session, stock=3)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    await correct_inventory(db_session, product.id, 1, "shrink stock", admin.id)
    with pytest.raises(ValueError, match="Недостаточно"):
        await create_invoice_from_cart(
            db_session,
            company.company_id,
            admin.id,
            settings,
        )


@pytest.mark.db
@pytest.mark.asyncio
async def test_price_snapshot_after_product_price_change(db_session):
    admin, company, product = await _seed_company_product(db_session)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
    )
    await update_product(
        db_session,
        product.id,
        ProductInput(
            name=product.name,
            sale_price=Decimal("500"),
            cost_price=Decimal("80"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    item = await db_session.scalar(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
    )
    assert item is not None
    assert item.unit_price == Decimal("200")


@pytest.mark.db
@pytest.mark.asyncio
async def test_invoice_allows_null_cost_price_snapshot(db_session):
    admin, company, _ = await _seed_company_product(db_session)
    imported = await create_product(
        db_session,
        ProductInput(
            name="Imported null cost",
            sale_price=Decimal("150"),
            cost_price=None,
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, imported.id, 5, "seed", admin.id)
    await upsert_cart_item(db_session, company.company_id, imported.id, 1)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        Settings(),
    )
    item = await db_session.scalar(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
    )
    assert item is not None
    assert item.cost_price_snapshot is None
    assert item.unit_price == Decimal("150")


@pytest.mark.db
@pytest.mark.asyncio
async def test_cancel_releases_reservation(db_session):
    admin, company, product = await _seed_company_product(db_session)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
    )
    on_hand_before, reserved_before, _available, movements_before = await _stock_state(
        db_session, product.id
    )
    assert reserved_before == 2
    await cancel_invoice(db_session, invoice.id, "company", admin.id, company_id=company.company_id)
    reservation = await db_session.scalar(
        select(InventoryReservation).where(InventoryReservation.invoice_id == invoice.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASED.value
    on_hand, reserved, available, movements = await _stock_state(db_session, product.id)
    assert on_hand == on_hand_before
    assert reserved == 0
    assert available == on_hand
    assert movements == movements_before


@pytest.mark.db
@pytest.mark.asyncio
async def test_pay_and_ship_flow(db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
    )
    paid = await confirm_payment(db_session, invoice.id, admin.id, idempotency_key="pay-1")
    assert paid is not None
    assert paid.status == InvoiceStatus.PAID.value

    shipped = await ship_invoice(db_session, invoice.id, admin.id)
    assert shipped is not None
    assert shipped.status == InvoiceStatus.SHIPPED.value

    inventory = await db_session.scalar(select(Inventory).where(Inventory.product_id == product.id))
    assert inventory is not None
    assert inventory.quantity_on_hand == 7

    shipped_again = await ship_invoice(db_session, invoice.id, admin.id)
    assert shipped_again is not None
    assert shipped_again.status == InvoiceStatus.SHIPPED.value
    inventory_after = await db_session.scalar(
        select(Inventory).where(Inventory.product_id == product.id)
    )
    assert inventory_after is not None
    assert inventory_after.quantity_on_hand == 7


@pytest.mark.db
@pytest.mark.asyncio
async def test_confirm_payment_twice_idempotent(db_session):
    admin, company, product = await _seed_company_product(db_session)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
    )
    first = await confirm_payment(db_session, invoice.id, admin.id, idempotency_key="pay-dup")
    second = await confirm_payment(db_session, invoice.id, admin.id, idempotency_key="pay-dup")
    assert first is not None and second is not None
    assert first.status == InvoiceStatus.PAID.value
    assert second.status == InvoiceStatus.PAID.value


@pytest.mark.db
@pytest.mark.asyncio
async def test_expired_invoice_cannot_be_paid(db_session):
    admin, company, product = await _seed_company_product(db_session)
    settings = Settings()
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session,
        company.company_id,
        admin.id,
        settings,
    )
    row = await db_session.get(Invoice, invoice.id)
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    await expire_due_invoices(db_session)
    await db_session.commit()
    with pytest.raises(ValueError):
        await confirm_payment(db_session, invoice.id, admin.id)


@pytest.mark.db
@pytest.mark.asyncio
async def test_expire_invoice_releases_reservation(db_session):
    product, invoice_id = await _due_invoice(db_session, qty=3, stock=10)
    on_hand, reserved, available, movements = await _stock_state(db_session, product.id)
    assert on_hand == 10
    assert reserved == 3
    assert available == 7

    expired_count = await expire_due_invoices(db_session)
    await db_session.commit()

    assert expired_count == 1
    invoice = await db_session.get(Invoice, invoice_id)
    assert invoice is not None
    assert invoice.status == InvoiceStatus.EXPIRED.value

    reservations = (
        await db_session.scalars(
            select(InventoryReservation).where(InventoryReservation.invoice_id == invoice_id)
        )
    ).all()
    assert len(reservations) == 1
    assert reservations[0].status == ReservationStatus.RELEASED.value
    assert reservations[0].quantity == 3

    on_hand, reserved, available, movements_after = await _stock_state(db_session, product.id)
    assert on_hand == 10
    assert reserved == 0
    assert available == 10
    assert movements_after == movements


@pytest.mark.db
@pytest.mark.asyncio
async def test_expire_invoice_is_idempotent(db_session):
    product, invoice_id = await _due_invoice(db_session, qty=3, stock=10)

    first = await expire_due_invoices(db_session)
    await db_session.commit()
    assert first == 1

    on_hand, reserved, available, movements = await _stock_state(db_session, product.id)
    assert on_hand == 10
    assert reserved == 0
    assert available == 10

    second = await expire_due_invoices(db_session)
    await db_session.commit()
    assert second == 0

    invoice = await db_session.get(Invoice, invoice_id)
    assert invoice is not None
    assert invoice.status == InvoiceStatus.EXPIRED.value
    reservations = (
        await db_session.scalars(
            select(InventoryReservation).where(InventoryReservation.invoice_id == invoice_id)
        )
    ).all()
    assert len(reservations) == 1
    assert reservations[0].status == ReservationStatus.RELEASED.value

    on_hand_after, reserved_after, available_after, movements_after = await _stock_state(
        db_session, product.id
    )
    assert on_hand_after == 10
    assert reserved_after == 0
    assert available_after == 10
    assert on_hand_after == on_hand
    assert reserved_after == reserved
    assert available_after == available
    assert movements_after == movements
    assert on_hand_after >= 0
    assert available_after >= 0


# Две компании, один товар с on_hand=1, в каждой корзине qty=1.
async def _two_companies_one_unit(db_session):
    admin, company_a, product = await _seed_company_product(db_session, stock=1)
    seller_row = await db_session.get(Company, company_a.company_id)
    assert seller_row is not None
    company_b = await create_company(
        db_session,
        CompanyInput(name="Invoice Co B", billing_entity_id=seller_row.billing_entity_id),
        admin.id,
    )
    await upsert_cart_item(db_session, company_a.company_id, product.id, 1)
    await upsert_cart_item(db_session, company_b.company_id, product.id, 1)
    return admin, company_a, company_b, product


@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_invoice_reservation_does_not_oversell(db_engine, db_session):
    admin, company_a, company_b, product = await _two_companies_one_unit(db_session)
    on_hand, reserved, available, _movements = await _stock_state(db_session, product.id)
    assert on_hand == 1
    assert reserved == 0
    assert available == 1

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    settings = Settings()
    barrier = asyncio.Barrier(2)

    async def _attempt(company_id):
        async with factory() as session:
            await barrier.wait()
            try:
                invoice = await create_invoice_from_cart(
                    session,
                    company_id,
                    admin.id,
                    settings,
                )
                return invoice
            except ValueError as exc:
                await session.rollback()
                return exc

    first, second = await asyncio.gather(
        _attempt(company_a.company_id),
        _attempt(company_b.company_id),
    )

    outcomes = [first, second]
    successes = [item for item in outcomes if not isinstance(item, Exception)]
    failures = [item for item in outcomes if isinstance(item, ValueError)]
    unexpected = [
        item
        for item in outcomes
        if isinstance(item, Exception) and not isinstance(item, ValueError)
    ]
    assert unexpected == []
    assert len(successes) == 1
    assert len(failures) == 1
    assert "Недостаточно" in str(failures[0])
    assert successes[0].status == InvoiceStatus.AWAITING_PAYMENT.value

    async with factory() as check:
        invoices = list(await check.scalars(select(Invoice)))
        items = list(await check.scalars(select(InvoiceItem)))
        reservations = list(await check.scalars(select(InventoryReservation)))
        active = [row for row in reservations if row.status == ReservationStatus.ACTIVE.value]
        on_hand, reserved, available, _movements = await _stock_state(check, product.id)
        cart_a = await get_cart_view(check, company_a.company_id)
        cart_b = await get_cart_view(check, company_b.company_id)

    assert len(invoices) == 1
    assert len(items) == 1
    assert len(reservations) == 1
    assert len(active) == 1
    assert active[0].quantity == 1
    assert invoices[0].id == successes[0].id
    assert on_hand == 1
    assert reserved == 1
    assert available == 0
    assert reserved <= on_hand
    assert available >= 0
    cart_counts = sorted((len(cart_a.items), len(cart_b.items)))
    assert cart_counts == [0, 1]


# Счёт на 5 шт при остатке 10: уменьшение до 3 отпускает 2 из резерва.
@pytest.mark.db
@pytest.mark.asyncio
async def test_update_invoice_items_decrease_releases_reserve(db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 5)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    on_hand, reserved, available, _movements = await _stock_state(db_session, product.id)
    assert on_hand == 10
    assert reserved == 5
    assert available == 5

    updated = await update_invoice_items(
        db_session, invoice.id, admin.id, {invoice.items[0].id: 3}
    )
    assert updated is not None
    assert updated.items[0].quantity == 3
    assert updated.total == Decimal("600")
    on_hand, reserved, available, movements = await _stock_state(db_session, product.id)
    assert on_hand == 10
    assert reserved == 3
    assert available == 7
    assert reserved <= on_hand
    assert available >= 0
    assert available == on_hand - reserved
    assert movements == 1


# Увеличение 3→5 наращивает резерв, on_hand не меняется.
@pytest.mark.db
@pytest.mark.asyncio
async def test_update_invoice_items_increase_reserves(db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    on_hand, reserved, available, movements = await _stock_state(db_session, product.id)
    assert on_hand == 10
    assert reserved == 3
    assert available == 7

    updated = await update_invoice_items(
        db_session, invoice.id, admin.id, {invoice.items[0].id: 5}
    )
    assert updated is not None
    assert updated.items[0].quantity == 5
    assert updated.total == Decimal("1000")
    on_hand, reserved, available, movements_after = await _stock_state(
        db_session, product.id
    )
    assert on_hand == 10
    assert reserved == 5
    assert available == 5
    assert available == on_hand - reserved
    assert reserved <= on_hand
    assert movements_after == movements
    audit = await db_session.scalar(
        select(AuditLog)
        .where(
            AuditLog.action == "invoice.update_items",
            AuditLog.entity_id == invoice.id,
        )
        .order_by(AuditLog.id.desc())
    )
    assert audit is not None
    assert audit.actor_type == "admin"
    assert audit.actor_id == admin.id
    assert audit.entity_type == "invoice"
    assert audit.created_at is not None


# Увеличение сверх available отклоняется, состояние не меняется.
@pytest.mark.db
@pytest.mark.asyncio
async def test_update_invoice_items_increase_rejects_insufficient(db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 5)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    product_id = product.id
    invoice_id = invoice.id
    item_id = invoice.items[0].id
    on_hand, reserved, available, movements = await _stock_state(db_session, product_id)
    assert on_hand == 10
    assert reserved == 5
    assert available == 5
    with pytest.raises(ValueError, match="Недостаточно товара"):
        await update_invoice_items(
            db_session, invoice_id, admin.id, {item_id: 11}
        )
    await db_session.rollback()
    reloaded = await get_invoice(db_session, invoice_id)
    assert reloaded is not None
    assert reloaded.items[0].quantity == 5
    on_hand, reserved, available, movements_after = await _stock_state(
        db_session, product_id
    )
    assert on_hand == 10
    assert reserved == 5
    assert available == 5
    assert movements_after == movements


# Удаление строки (qty 0) снимает её резерв, вторая позиция остаётся.
@pytest.mark.db
@pytest.mark.asyncio
async def test_update_invoice_items_remove_line_releases_reserve(db_session):
    admin, company, first = await _seed_company_product(db_session, stock=10)
    second = await create_product(
        db_session,
        ProductInput(
            name="Second Product",
            sale_price=Decimal("50"),
            cost_price=Decimal("20"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, second.id, 10, "seed", admin.id)
    await upsert_cart_item(db_session, company.company_id, first.id, 2)
    await upsert_cart_item(db_session, company.company_id, second.id, 4)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    assert len(invoice.items) == 2
    drop_id = next(item.id for item in invoice.items if item.product_id == second.id)
    keep_id = next(item.id for item in invoice.items if item.product_id == first.id)
    updated = await update_invoice_items(
        db_session,
        invoice.id,
        admin.id,
        {keep_id: 2, drop_id: 0},
    )
    assert updated is not None
    assert len(updated.items) == 1
    assert updated.items[0].product_id == first.id
    _on_hand, first_reserved, _available, _movements = await _stock_state(
        db_session, first.id
    )
    _on_hand, second_reserved, _available, _movements = await _stock_state(
        db_session, second.id
    )
    assert first_reserved == 2
    assert second_reserved == 0
    first_on_hand, _r, _a, _m = await _stock_state(db_session, first.id)
    second_on_hand, _r, _a, _m = await _stock_state(db_session, second.id)
    assert first_on_hand == 10
    assert second_on_hand == 10


# Последнюю позицию нельзя обнулить — счёт не должен остаться пустым.
@pytest.mark.db
@pytest.mark.asyncio
async def test_update_invoice_items_rejects_empty_invoice(db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    product_id = product.id
    invoice_id = invoice.id
    item_id = invoice.items[0].id
    with pytest.raises(ValueError, match="хотя бы одна позиция"):
        await update_invoice_items(
            db_session, invoice_id, admin.id, {item_id: 0}
        )
    await db_session.rollback()
    reloaded = await get_invoice(db_session, invoice_id)
    assert reloaded is not None
    assert len(reloaded.items) == 1
    assert reloaded.items[0].quantity == 3
    on_hand, reserved, available, _movements = await _stock_state(db_session, product_id)
    assert on_hand == 10
    assert reserved == 3
    assert available == 7


# Ошибка по второй позиции откатывает изменение первой.
@pytest.mark.db
@pytest.mark.asyncio
async def test_update_invoice_items_partial_failure_rolls_back(db_session):
    admin, company, first = await _seed_company_product(db_session, stock=10)
    second = await create_product(
        db_session,
        ProductInput(
            name="Tight Product",
            sale_price=Decimal("50"),
            cost_price=Decimal("20"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, second.id, 3, "seed", admin.id)
    await upsert_cart_item(db_session, company.company_id, first.id, 2)
    await upsert_cart_item(db_session, company.company_id, second.id, 3)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    first_id = first.id
    second_id = second.id
    invoice_id = invoice.id
    rich = next(item for item in invoice.items if item.product_id == first_id)
    poor = next(item for item in invoice.items if item.product_id == second_id)
    with pytest.raises(ValueError, match="Недостаточно товара"):
        await update_invoice_items(
            db_session,
            invoice_id,
            admin.id,
            {rich.id: 3, poor.id: 4},
        )
    await db_session.rollback()
    reloaded = await get_invoice(db_session, invoice_id)
    assert reloaded is not None
    by_product = {item.product_id: item.quantity for item in reloaded.items}
    assert by_product[first_id] == 2
    assert by_product[second_id] == 3
    first_on_hand, first_reserved, _a, _m = await _stock_state(db_session, first_id)
    second_on_hand, second_reserved, _a, _m = await _stock_state(db_session, second_id)
    assert first_on_hand == 10
    assert first_reserved == 2
    assert second_on_hand == 3
    assert second_reserved == 3


# Paid счёт нельзя редактировать.
@pytest.mark.db
@pytest.mark.asyncio
async def test_update_invoice_items_rejects_paid(db_session):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    product_id = product.id
    invoice_id = invoice.id
    await confirm_payment(db_session, invoice.id, admin.id)
    item_id = invoice.items[0].id
    with pytest.raises(ValueError, match="awaiting_payment"):
        await update_invoice_items(
            db_session, invoice_id, admin.id, {item_id: 1}
        )
    await db_session.rollback()
    reloaded = await get_invoice(db_session, invoice_id)
    assert reloaded is not None
    assert reloaded.status == InvoiceStatus.PAID.value
    assert reloaded.items[0].quantity == 2
    _on_hand, reserved, _available, _movements = await _stock_state(
        db_session, product_id
    )
    assert reserved == 2


# Paid счёт нельзя отменить.
@pytest.mark.db
@pytest.mark.asyncio
async def test_cancel_paid_invoice_forbidden(db_session):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    await confirm_payment(db_session, invoice.id, admin.id)
    _on_hand, reserved, _available, _movements = await _stock_state(
        db_session, product.id
    )
    assert reserved == 2
    with pytest.raises(ValueError, match="не может быть отменён"):
        await cancel_invoice(db_session, invoice.id, "admin", admin.id)
    await db_session.rollback()
    await db_session.rollback()


# Cancel и confirm_payment конкурируют за один awaiting_payment счёт.
@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_cancel_and_pay_are_exclusive(db_engine, db_session):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    invoice_id = invoice.id
    on_hand_before, reserved_before, available_before, movements_before = await _stock_state(
        db_session, product.id
    )
    assert reserved_before == 2
    assert available_before == on_hand_before - reserved_before

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    barrier = asyncio.Barrier(2)

    async def _cancel():
        async with factory() as session:
            await barrier.wait()
            try:
                return await cancel_invoice(session, invoice_id, "admin", admin.id)
            except ValueError as exc:
                await session.rollback()
                return exc

    async def _pay():
        async with factory() as session:
            await barrier.wait()
            try:
                return await confirm_payment(session, invoice_id, admin.id)
            except ValueError as exc:
                await session.rollback()
                return exc

    cancel_out, pay_out = await asyncio.gather(_cancel(), _pay())

    async with factory() as check:
        row = await check.get(Invoice, invoice_id)
        assert row is not None
        payments = list(
            await check.scalars(select(Payment).where(Payment.invoice_id == invoice_id))
        )
        reservations = list(
            await check.scalars(
                select(InventoryReservation).where(
                    InventoryReservation.invoice_id == invoice_id
                )
            )
        )
        on_hand_after, reserved_after, available_after, movements_after = await _stock_state(
            check, product.id
        )

    outcomes = [cancel_out, pay_out]
    errors = [item for item in outcomes if isinstance(item, ValueError)]
    successes = [item for item in outcomes if not isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(errors) == 1
    assert on_hand_after == on_hand_before
    assert movements_after == movements_before
    assert available_after == on_hand_after - reserved_after

    if row.status == InvoiceStatus.CANCELED.value:
        assert payments == []
        assert len(reservations) == 1
        assert reservations[0].status == ReservationStatus.RELEASED.value
        assert reserved_after == 0
    else:
        assert row.status == InvoiceStatus.PAID.value
        assert len(payments) == 1
        assert payments[0].status == PaymentStatus.PAID.value
        assert len(reservations) == 1
        assert reservations[0].status == ReservationStatus.ACTIVE.value
        assert reserved_after == 2


# Отгрузка без active reservation запрещена, счёт остаётся paid.
@pytest.mark.db
@pytest.mark.asyncio
async def test_ship_without_active_reservation_forbidden(db_session):
    admin, company, product = await _seed_company_product(db_session)
    product_id = product.id
    await upsert_cart_item(db_session, company.company_id, product_id, 2)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    invoice_id = invoice.id
    await confirm_payment(db_session, invoice_id, admin.id)
    reservation = await db_session.scalar(
        select(InventoryReservation).where(
            InventoryReservation.invoice_id == invoice_id
        )
    )
    assert reservation is not None
    reservation.status = ReservationStatus.RELEASED.value
    await db_session.commit()

    on_hand_before, reserved_before, _available, movements_before = await _stock_state(
        db_session, product_id
    )
    assert reserved_before == 0

    with pytest.raises(ValueError, match="активного резерва"):
        await ship_invoice(db_session, invoice_id, admin.id)
    await db_session.rollback()

    row = await db_session.scalar(select(Invoice).where(Invoice.id == invoice_id))
    assert row is not None
    assert row.status == InvoiceStatus.PAID.value
    on_hand_after, reserved_after, _available, movements_after = await _stock_state(
        db_session, product_id
    )
    assert on_hand_after == on_hand_before
    assert reserved_after == reserved_before
    assert movements_after == movements_before


# Повторная отмена не снимает резерв второй раз.
@pytest.mark.db
@pytest.mark.asyncio
async def test_cancel_invoice_twice_does_not_double_release(db_session):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    await cancel_invoice(db_session, invoice.id, "admin", admin.id)
    on_hand, reserved, available, movements = await _stock_state(db_session, product.id)
    assert reserved == 0
    assert available == on_hand
    assert movements >= 1
    with pytest.raises(ValueError, match="не может быть отменён"):
        await cancel_invoice(db_session, invoice.id, "admin", admin.id)
    await db_session.rollback()


# Два awaiting_payment счёта не могут одновременно забрать последний available.
@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_invoice_edit_does_not_oversell(db_engine, db_session):
    admin, company_a, product = await _seed_company_product(db_session, stock=3)
    seller_row = await db_session.get(Company, company_a.company_id)
    assert seller_row is not None
    company_b = await create_company(
        db_session,
        CompanyInput(name="Invoice Co B", billing_entity_id=seller_row.billing_entity_id),
        admin.id,
    )
    await upsert_cart_item(db_session, company_a.company_id, product.id, 1)
    invoice_a = await create_invoice_from_cart(
        db_session, company_a.company_id, admin.id, Settings()
    )
    await upsert_cart_item(db_session, company_b.company_id, product.id, 1)
    invoice_b = await create_invoice_from_cart(
        db_session, company_b.company_id, admin.id, Settings()
    )
    on_hand, reserved, available, _movements = await _stock_state(db_session, product.id)
    assert on_hand == 3
    assert reserved == 2
    assert available == 1
    item_a = invoice_a.items[0].id
    item_b = invoice_b.items[0].id

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    barrier = asyncio.Barrier(2)

    async def _attempt(invoice_id, item_id):
        async with factory() as session:
            await barrier.wait()
            try:
                return await update_invoice_items(
                    session, invoice_id, admin.id, {item_id: 2}
                )
            except ValueError as exc:
                await session.rollback()
                return exc

    first, second = await asyncio.gather(
        _attempt(invoice_a.id, item_a),
        _attempt(invoice_b.id, item_b),
    )
    outcomes = [first, second]
    successes = [item for item in outcomes if not isinstance(item, Exception)]
    failures = [item for item in outcomes if isinstance(item, ValueError)]
    unexpected = [
        item
        for item in outcomes
        if isinstance(item, Exception) and not isinstance(item, ValueError)
    ]
    assert unexpected == []
    assert len(successes) == 1
    assert len(failures) == 1
    assert "Недостаточно" in str(failures[0])

    async with factory() as check:
        on_hand, reserved, available, _movements = await _stock_state(check, product.id)
        items = list(await check.scalars(select(InvoiceItem)))
    assert on_hand == 3
    assert reserved == 3
    assert available == 0
    assert reserved <= on_hand
    assert available >= 0
    assert sorted(item.quantity for item in items) == [1, 2]


# Без billing_entity счёт выставить нельзя.
@pytest.mark.db
@pytest.mark.asyncio
async def test_create_invoice_requires_billing_entity(db_session):
    admin = await _seed_admin(db_session)
    company = await create_company(db_session, CompanyInput(name="No Bill Co"), admin.id)
    warehouse = Warehouse(code="MAIN", name="Main", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()
    product = await create_product(
        db_session,
        ProductInput(
            name="No Bill Product",
            sale_price=Decimal("200"),
            cost_price=Decimal("80"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, 10, "seed", admin.id)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    with pytest.raises(ValueError, match="юрлицо поставщика"):
        await create_invoice_from_cart(
            db_session, company.company_id, admin.id, Settings()
        )


_PDF_DATE_RE = re.compile(rb"/(CreationDate|ModDate) \(D:\d{14}Z\)")
_PDF_ID_RE = re.compile(rb"/ID \[<[0-9A-Fa-f]+><[0-9A-Fa-f]+>\]")


def _pdf_for_snapshot_compare(pdf: bytes) -> bytes:
    normalized = _PDF_DATE_RE.sub(rb"/\1 (D:00000000000000Z)", pdf)
    return _PDF_ID_RE.sub(
        rb"/ID [<00000000000000000000000000000000><00000000000000000000000000000000>]",
        normalized,
    )


# Смена BillingEntity и адреса компании не меняет снимок и PDF уже созданного счёта.
@pytest.mark.db
@pytest.mark.asyncio
async def test_invoice_snapshots_ignore_later_party_changes(db_session, db_engine):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    assert invoice.seller.legal_name == "ИП Продавец"
    assert invoice.seller.inn == "7701234567"
    assert invoice.buyer.legal_name == "ООО Покупатель"
    assert invoice.buyer.kpp == "770201001"
    assert invoice.buyer.legal_address == "Москва, ул. Покупателя, 2"
    assert invoice.buyer.recipient_address == "Склад получателя, 3"
    assert "7701234567" in (invoice.payment_instructions or "")
    pdf_before = render_invoice_pdf(invoice)
    row = await db_session.get(Company, company.company_id)
    assert row is not None and row.billing_entity_id is not None
    seller = await db_session.get(BillingEntity, row.billing_entity_id)
    assert seller is not None
    seller.legal_name = "ООО Новое Юрлицо"
    seller.inn = "7709999999"
    seller.bank_account = "40702810100000000999"
    row.legal_name = "ООО Новый Покупатель"
    row.legal_address = "Новый адрес"
    row.delivery_address = "Новая доставка"
    row.kpp = "770209999"
    await db_session.commit()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as verify_session:
        reloaded = await get_invoice(verify_session, invoice.id)
    assert reloaded is not None
    assert reloaded.seller.legal_name == "ИП Продавец"
    assert reloaded.seller.inn == "7701234567"
    assert reloaded.buyer.legal_name == "ООО Покупатель"
    assert reloaded.buyer.kpp == "770201001"
    assert reloaded.buyer.legal_address == "Москва, ул. Покупателя, 2"
    assert reloaded.buyer.recipient_address == "Склад получателя, 3"
    assert invoice_export_values(invoice) == invoice_export_values(reloaded)
    pdf_after = render_invoice_pdf(reloaded)
    assert _pdf_for_snapshot_compare(pdf_before) == _pdf_for_snapshot_compare(pdf_after)
    assert reloaded.seller.bank_account == "40702810100000000001"
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    fresh = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    assert fresh.seller.legal_name == "ООО Новое Юрлицо"
    assert fresh.seller.inn == "7709999999"
    assert fresh.seller.bank_account == "40702810100000000999"
    assert fresh.buyer.legal_name == "ООО Новый Покупатель"
    assert fresh.buyer.kpp == "770209999"
    assert fresh.id != invoice.id




# Две компании с разными ИП: счета изолированы, правка ИП A не трогает счёт A.
@pytest.mark.db
@pytest.mark.asyncio


@pytest.mark.db
@pytest.mark.asyncio
async def test_invoice_snapshot_survives_fresh_db_session(db_session, db_engine):
    """Regression: party edits in one session must not leak into invoice reload."""
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    company_row = await db_session.get(Company, company.company_id)
    assert company_row is not None and company_row.billing_entity_id is not None
    seller = await db_session.get(BillingEntity, company_row.billing_entity_id)
    assert seller is not None
    seller.inn = "7709999999"
    await db_session.commit()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as other_session:
        reloaded = await get_invoice(other_session, invoice.id)
    assert reloaded is not None
    assert reloaded.seller.inn == "7701234567"

async def test_invoice_snapshots_isolated_between_billing_entities(db_session):
    admin = await _seed_admin(db_session)
    seller_a = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name="Seller A",
            legal_name="ИП Альфа",
            inn="7701111111",
            kpp="770101001",
            bank_account="40702810100000000001",
            bik="044525001",
            bank_name="Банк А",
            corr_account="30101810400000000001",
        ),
        admin.id,
    )
    seller_b = await create_billing_entity(
        db_session,
        BillingEntityInput(
            name="Seller B",
            legal_name="ИП Бета",
            inn="7702222222",
            kpp="770202002",
            bank_account="40702810100000000002",
            bik="044525002",
            bank_name="Банк Б",
            corr_account="30101810400000000002",
        ),
        admin.id,
    )
    company_a = await create_company(
        db_session,
        CompanyInput(
            name="Co A",
            legal_name="ООО Альфа",
            inn="7711111111",
            contact_email="a@example.com",
            billing_entity_id=seller_a.id,
        ),
        admin.id,
    )
    company_b = await create_company(
        db_session,
        CompanyInput(
            name="Co B",
            legal_name="ООО Бета",
            inn="7722222222",
            contact_email="b@example.com",
            billing_entity_id=seller_b.id,
        ),
        admin.id,
    )
    row_a = await db_session.get(Company, company_a.company_id)
    row_b = await db_session.get(Company, company_b.company_id)
    assert row_a is not None and row_b is not None
    row_a.kpp = "771101001"
    row_b.kpp = "772202002"
    await db_session.commit()
    warehouse = Warehouse(code="MAIN", name="Main", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()
    product = await create_product(
        db_session,
        ProductInput(
            name="AB Product",
            sale_price=Decimal("100"),
            cost_price=Decimal("40"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, product.id, 20, "seed", admin.id)
    await upsert_cart_item(db_session, company_a.company_id, product.id, 1)
    invoice_a = await create_invoice_from_cart(
        db_session, company_a.company_id, admin.id, Settings()
    )
    await upsert_cart_item(db_session, company_b.company_id, product.id, 1)
    invoice_b = await create_invoice_from_cart(
        db_session, company_b.company_id, admin.id, Settings()
    )
    assert invoice_a.seller.legal_name == "ИП Альфа"
    assert invoice_a.seller.bank_account == "40702810100000000001"
    assert invoice_a.buyer.kpp == "771101001"
    assert invoice_b.seller.legal_name == "ИП Бета"
    assert invoice_b.seller.bank_account == "40702810100000000002"
    assert invoice_b.buyer.kpp == "772202002"
    updated = await update_billing_entity(
        db_session,
        seller_a.id,
        BillingEntityInput(
            name="Seller A",
            legal_name="ИП Альфа Новый",
            inn="7701111111",
            kpp="770101001",
            bank_account="40702810100000000991",
            bik="044525001",
            bank_name="Банк А",
            corr_account="30101810400000000001",
        ),
        admin.id,
    )
    assert updated is not None
    reloaded_a = await get_invoice(db_session, invoice_a.id)
    reloaded_b = await get_invoice(db_session, invoice_b.id)
    assert reloaded_a is not None and reloaded_b is not None
    assert reloaded_a.seller.legal_name == "ИП Альфа"
    assert reloaded_a.seller.bank_account == "40702810100000000001"
    assert reloaded_b.seller.legal_name == "ИП Бета"
    await upsert_cart_item(db_session, company_a.company_id, product.id, 1)
    invoice_a2 = await create_invoice_from_cart(
        db_session, company_a.company_id, admin.id, Settings()
    )
    assert invoice_a2.seller.legal_name == "ИП Альфа Новый"
    assert invoice_a2.seller.bank_account == "40702810100000000991"


# HTTP-клиент на той же test-БД, что и маршруты.

# Логинит через HTML и ставит cookie.
async def _login(client: AsyncClient, login: str, password: str):
    return await client.post(
        "/login",
        data={"login": login, "password": password},
        follow_redirects=False,
    )


# Админский список: номер — ссылка, кнопки «Карточка» нет.
@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_invoice_list_number_is_link(db_session, client):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    await _login(client, "invoice-admin", "admin-pass")
    html = await client.get("/admin/invoices")
    assert html.status_code == 200
    assert f'href="/admin/invoices/{invoice.id}"' in html.text
    assert invoice.number in html.text
    assert "Карточка" not in html.text


# POST уменьшения qty переживает reload карточки и не трогает on_hand.
@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_update_items_persists_after_reload(db_session, client):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    product_id = product.id
    await upsert_cart_item(db_session, company.company_id, product_id, 3)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    item_id = invoice.items[0].id
    await _login(client, "invoice-admin", "admin-pass")
    saved = await client.post(
        f"/admin/invoices/{invoice.id}/items",
        data={f"qty-{item_id}": "2"},
    )
    assert saved.status_code == 200
    assert "Позиции обновлены" in saved.text
    assert f'name="qty-{item_id}"' in saved.text
    assert "value=\"2\"" in saved.text or 'value="2"' in saved.text
    reloaded = await client.get(f"/admin/invoices/{invoice.id}")
    assert reloaded.status_code == 200
    assert 'value="2"' in reloaded.text
    on_hand, reserved, available, _movements = await _stock_state(db_session, product_id)
    assert on_hand == 10
    assert reserved == 2
    assert available == 8


# Awaiting_payment: поля qty и сохранение; paid — только отгрузка, POST items = 400.
@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_invoice_edit_html_and_paid_forbidden(db_session, client):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    await _login(client, "invoice-admin", "admin-pass")
    awaiting = await client.get(f"/admin/invoices/{invoice.id}")
    assert awaiting.status_code == 200
    assert f'name="qty-{invoice.items[0].id}"' in awaiting.text
    assert "Сохранить" in awaiting.text
    assert awaiting.text.count('id="invoice-summary-total"') == 1
    assert "hx-swap-oob" not in awaiting.text
    assert "Отменить" in awaiting.text
    assert "Отгрузить" not in awaiting.text

    product_page = await client.get(f"/admin/products/{product.id}")
    assert product_page.status_code == 200
    assert "Остаток" in product_page.text
    assert "резерв" in product_page.text
    assert "Доступно" in product_page.text

    await confirm_payment(db_session, invoice.id, admin.id)
    paid = await client.get(f"/admin/invoices/{invoice.id}")
    assert paid.status_code == 200
    assert "Сохранить" not in paid.text
    assert f'name="qty-{invoice.items[0].id}"' not in paid.text
    assert 'action="/admin/invoices/' + str(invoice.id) + '/cancel"' not in paid.text
    assert "Отгрузить" in paid.text

    forbidden = await client.post(
        f"/admin/invoices/{invoice.id}/items",
        data={f"qty-{invoice.items[0].id}": "1"},
    )
    assert forbidden.status_code == 400
    reloaded = await get_invoice(db_session, invoice.id)
    assert reloaded is not None
    assert reloaded.items[0].quantity == 3
    _on_hand, reserved, _available, _movements = await _stock_state(
        db_session, product.id
    )
    assert reserved == 3

@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_invoices_awaiting_payment_filter(db_session, client):
    admin, company, product = await _seed_company_product(db_session)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    unpaid = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings(), idempotency_key="inv-filter-unpaid"
    )
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    paid_invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings(), idempotency_key="inv-filter-paid"
    )
    await confirm_payment(db_session, paid_invoice.id, admin.id)
    await _login(client, "invoice-admin", "admin-pass")

    all_page = await client.get("/admin/invoices")
    assert all_page.status_code == 200
    assert unpaid.number in all_page.text
    assert paid_invoice.number in all_page.text

    filtered = await client.get("/admin/invoices?status=awaiting_payment")
    assert filtered.status_code == 200
    assert unpaid.number in filtered.text
    assert f"><strong>{paid_invoice.number}</strong>" not in filtered.text
    assert 'href="/admin/invoices?status=awaiting_payment"' in filtered.text


# Счёт получает expires_at = 2 рабочих дня от created_at.
@pytest.mark.db
@pytest.mark.asyncio
async def test_create_invoice_business_day_expires_at(db_session):
    from b2b_commerce.invoices.service import compute_invoice_expires_at

    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    row = await db_session.get(Invoice, invoice.id)
    assert row is not None
    assert row.expires_at == compute_invoice_expires_at(
        row.created_at, business_days=Settings().invoice_ttl_business_days
    )




# До expires_at worker не переводит счёт в expired.
@pytest.mark.db
@pytest.mark.asyncio
async def test_expire_due_invoices_waits_until_business_day_end(db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    row = await db_session.get(Invoice, invoice.id)
    assert row is not None
    assert row.expires_at is not None
    assert row.expires_at > datetime.now(UTC)
    expired_count = await expire_due_invoices(db_session)
    assert expired_count == 0
    reloaded = await db_session.get(Invoice, invoice.id)
    assert reloaded is not None
    assert reloaded.status == InvoiceStatus.AWAITING_PAYMENT.value

# Правка позиций не сдвигает expires_at.
@pytest.mark.db
@pytest.mark.asyncio
async def test_update_invoice_items_keeps_expires_at(db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 4)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    row = await db_session.get(Invoice, invoice.id)
    assert row is not None
    expires_before = row.expires_at
    item_id = invoice.items[0].id
    updated = await update_invoice_items(
        db_session, invoice.id, admin.id, {item_id: 2}
    )
    assert updated is not None
    row = await db_session.get(Invoice, invoice.id)
    assert row is not None
    assert row.expires_at == expires_before


# Клиент добавляет товар в свой счёт.
@pytest.mark.db
@pytest.mark.asyncio
async def test_company_update_invoice_items_add_product(db_session):
    from b2b_commerce.invoices.service import update_invoice_items_by_company

    admin, company, first = await _seed_company_product(db_session, stock=10)
    second = await create_product(
        db_session,
        ProductInput(
            name="Add-on",
            sale_price=Decimal("80"),
            cost_price=Decimal("30"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, second.id, 5, "seed", admin.id)
    await upsert_cart_item(db_session, company.company_id, first.id, 2)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    keep_id = invoice.items[0].id
    updated = await update_invoice_items_by_company(
        db_session,
        invoice.id,
        company.company_id,
        admin.id,
        quantities={keep_id: 2},
        additions={second.id: 3},
    )
    assert updated is not None
    assert len(updated.items) == 2
    by_name = {item.product_name: item for item in updated.items}
    assert by_name["Add-on"].quantity == 3
    assert updated.total == sum(item.line_total for item in updated.items)
    _on_hand, second_reserved, _a, _m = await _stock_state(db_session, second.id)
    assert second_reserved == 3


# Админ добавляет товар в счёт.
@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_update_invoice_items_add_product(db_session):
    admin, company, first = await _seed_company_product(db_session, stock=10)
    second = await create_product(
        db_session,
        ProductInput(
            name="Admin add",
            sale_price=Decimal("60"),
            cost_price=Decimal("20"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, second.id, 4, "seed", admin.id)
    await upsert_cart_item(db_session, company.company_id, first.id, 1)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    item_id = invoice.items[0].id
    updated = await update_invoice_items(
        db_session,
        invoice.id,
        admin.id,
        {item_id: 1},
        additions={second.id: 2},
    )
    assert updated is not None
    assert len(updated.items) == 2
    _on_hand, reserved, _a, _m = await _stock_state(db_session, second.id)
    assert reserved == 2


# Новая позиция из dropdown добавляется в конец списка.
@pytest.mark.db
@pytest.mark.asyncio
async def test_add_invoice_item_appends_to_end(db_session):
    admin, company, first = await _seed_company_product(db_session, stock=10)
    second = await create_product(
        db_session,
        ProductInput(
            name="ZZZ Last Product",
            sale_price=Decimal("10"),
            cost_price=Decimal("5"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    third = await create_product(
        db_session,
        ProductInput(
            name="AAA First Add",
            sale_price=Decimal("20"),
            cost_price=Decimal("8"),
            status=ProductStatus.ACTIVE,
        ),
        admin.id,
    )
    await correct_inventory(db_session, second.id, 5, "seed", admin.id)
    await correct_inventory(db_session, third.id, 5, "seed", admin.id)
    await upsert_cart_item(db_session, company.company_id, first.id, 1)
    await upsert_cart_item(db_session, company.company_id, second.id, 1)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    assert [item.product_name for item in invoice.items] == [
        first.name,
        "ZZZ Last Product",
    ]
    item_ids = {item.product_id: item.id for item in invoice.items}
    updated = await update_invoice_items(
        db_session,
        invoice.id,
        admin.id,
        {item_ids[first.id]: 1, item_ids[second.id]: 1},
        additions={third.id: 1},
    )
    assert updated is not None
    assert [item.product_name for item in updated.items] == [
        first.name,
        "ZZZ Last Product",
        "AAA First Add",
    ]


# HTMX POST позиций возвращает только слот формы, без полной страницы.
@pytest.mark.db
@pytest.mark.asyncio
async def test_admin_update_items_htmx_returns_slot_only(db_session, client):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    item_id = invoice.items[0].id
    await _login(client, "invoice-admin", "admin-pass")
    saved = await client.post(
        f"/admin/invoices/{invoice.id}/items",
        data={f"qty-{item_id}": "2"},
        headers={"HX-Request": "true"},
    )
    assert saved.status_code == 200
    assert 'id="invoice-items-slot"' in saved.text
    assert "Позиции обновлены" in saved.text
    assert "<h1>" not in saved.text
    assert 'value="2"' in saved.text
    assert f'hx-post="/admin/invoices/{invoice.id}/items"' in saved.text
    assert 'id="invoice-summary-total"' in saved.text
    assert 'hx-swap-oob="innerHTML"' in saved.text


async def _login_customer(client: AsyncClient, company):
    login_resp = await _login(client, company.login, company.temporary_password)
    assert login_resp.status_code == 303
    if login_resp.headers.get("location") == "/profile":
        change = await client.post(
            "/change-password",
            data={
                "new_password": "customer-pass12",
            },
            follow_redirects=False,
        )
        assert change.status_code == 200


# HTMX POST клиента: слот с form_action, ошибка валидации — 200 для swap.
@pytest.mark.db
@pytest.mark.asyncio
async def test_customer_update_items_htmx_slot_and_validation_error(db_session, client):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 3)
    invoice = await create_invoice_from_cart(
        db_session, company.company_id, admin.id, Settings()
    )
    item_id = invoice.items[0].id
    await _login_customer(client, company)
    saved = await client.post(
        f"/invoices/{invoice.id}/items",
        data={f"qty-{item_id}": "2"},
        headers={"HX-Request": "true"},
    )
    assert saved.status_code == 200
    assert f'hx-post="/invoices/{invoice.id}/items"' in saved.text
    assert "Счёт обновлён" in saved.text

    invalid = await client.post(
        f"/invoices/{invoice.id}/items",
        data={f"qty-{item_id}": ""},
        headers={"HX-Request": "true"},
    )
    assert invalid.status_code == 200
    assert "Некорректные данные позиций" in invalid.text
    assert f'hx-post="/invoices/{invoice.id}/items"' in invalid.text

@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_same_company_checkout_single_invoice(db_engine, db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    settings = Settings()
    barrier = asyncio.Barrier(2)

    async def _attempt():
        async with factory() as session:
            await barrier.wait()
            try:
                return await create_invoice_from_cart(
                    session,
                    company.company_id,
                    admin.id,
                    settings,
                )
            except ValueError as exc:
                await session.rollback()
                return exc

    first, second = await asyncio.gather(_attempt(), _attempt())

    successes = [item for item in (first, second) if not isinstance(item, Exception)]
    failures = [item for item in (first, second) if isinstance(item, ValueError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "Корзина пуста" in str(failures[0])

    async with factory() as check:
        invoices = list(await check.scalars(select(Invoice)))
        cart = await get_cart_view(check, company.company_id)

    assert len(invoices) == 1
    assert invoices[0].id == successes[0].id
    assert cart.items == []


@pytest.mark.db
@pytest.mark.asyncio
async def test_http_post_invoices_checkout_from_cart(db_session, client):
    _admin, company, product = await _seed_company_product(db_session, stock=5)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)
    await db_session.commit()

    login = await client.post(
        "/login",
        data={"login": company.login, "password": company.temporary_password},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/profile"

    change = await client.post(
        "/change-password",
        data={"new_password": "checkoutpass1"},
        follow_redirects=False,
    )
    assert change.status_code == 200

    response = await client.post(
        "/invoices",
        data={"notes": "тест checkout"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/invoices/")

    invoices = list((await db_session.scalars(select(Invoice))).all())
    assert len(invoices) == 1
    assert invoices[0].notes == "тест checkout"
    cart = await get_cart_view(db_session, company.company_id)
    assert cart.items == []

@pytest.mark.db
@pytest.mark.asyncio
async def test_concurrent_create_invoice_idempotency_single_invoice(db_engine, db_session):
    admin, company, product = await _seed_company_product(db_session, stock=10)
    await upsert_cart_item(db_session, company.company_id, product.id, 2)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    settings = Settings()
    barrier = asyncio.Barrier(2)
    idem_key = "concurrent-create-idem"

    async def _attempt():
        async with factory() as session:
            await barrier.wait()
            return await create_invoice_from_cart(
                session,
                company.company_id,
                admin.id,
                settings,
                idempotency_key=idem_key,
            )

    first, second = await asyncio.gather(_attempt(), _attempt())
    assert first.id == second.id

    async with factory() as check:
        invoices = list(await check.scalars(select(Invoice)))
    assert len(invoices) == 1


@pytest.mark.db
@pytest.mark.asyncio
async def test_http_invoice_pdf_download_returns_pdf(db_session, client):
    _admin, company, product = await _seed_company_product(db_session, stock=5)
    await upsert_cart_item(db_session, company.company_id, product.id, 1)
    await db_session.commit()

    login = await client.post(
        "/login",
        data={"login": company.login, "password": company.temporary_password},
        follow_redirects=False,
    )
    assert login.status_code == 303
    await client.post(
        "/change-password",
        data={"new_password": "pdfpass1234"},
        follow_redirects=False,
    )

    checkout = await client.post("/invoices", data={}, follow_redirects=False)
    assert checkout.status_code == 303
    invoice_id = checkout.headers["location"].rstrip("/").split("/")[-1]

    pdf = await client.get(f"/invoices/{invoice_id}/download.pdf", follow_redirects=False)
    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert pdf.content.startswith(b"%PDF")
