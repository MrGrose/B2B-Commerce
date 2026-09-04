from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from b2b_commerce.cart.models import Cart, CartItem
from b2b_commerce.catalog.models import Product
from b2b_commerce.enums import ProductStatus
from b2b_commerce.inventory.service import get_availability, list_availability


# Проверяет, что товар можно положить в корзину.
def _require_orderable_product(product: Product | None) -> Product:
    if product is None:
        raise ValueError("Товар не найден")
    if product.deleted_at is not None or product.status != ProductStatus.ACTIVE:
        raise ValueError("Товар недоступен для заказа")
    return product


@dataclass
class CartItemView:
    product_id: UUID
    name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    availability: int


@dataclass
class CartView:
    id: UUID
    items: list[CartItemView]
    subtotal: Decimal


# Возвращает корзину компании или создаёт пустую.
async def get_or_create_cart(db: AsyncSession, company_id: UUID) -> Cart:
    cart = await db.scalar(select(Cart).where(Cart.company_id == company_id))
    if cart is None:
        cart = Cart(company_id=company_id)
        db.add(cart)
        await db.flush()
    return cart


# Собирает позиции корзины с ценами и доступностью.
async def get_cart_view(db: AsyncSession, company_id: UUID) -> CartView:
    cart = await get_or_create_cart(db, company_id)
    rows = (
        await db.scalars(
            select(CartItem)
            .options(selectinload(CartItem.product))
            .where(CartItem.cart_id == cart.id)
            .order_by(CartItem.id)
        )
    ).all()
    availability_by_product = await list_availability(db, [row.product_id for row in rows])
    items: list[CartItemView] = []
    subtotal = Decimal("0")
    for row in rows:
        product = row.product
        line_total = product.sale_price * row.quantity
        subtotal += line_total
        items.append(
            CartItemView(
                product_id=row.product_id,
                name=product.name,
                quantity=row.quantity,
                unit_price=product.sale_price,
                line_total=line_total,
                availability=availability_by_product.get(row.product_id, 0),
            )
        )
    return CartView(id=cart.id, items=items, subtotal=subtotal)


# Возвращает суммарное количество единиц товара в корзине.
async def get_cart_items_count(db: AsyncSession, company_id: UUID) -> int:
    cart = await get_or_create_cart(db, company_id)
    rows = (
        await db.scalars(
            select(CartItem).where(CartItem.cart_id == cart.id)
        )
    ).all()
    return sum(row.quantity for row in rows)


# Возвращает количество по product_id в корзине компании.
async def get_cart_quantities(db: AsyncSession, company_id: UUID) -> dict[UUID, int]:
    cart = await get_or_create_cart(db, company_id)
    rows = (
        await db.scalars(
            select(CartItem).where(CartItem.cart_id == cart.id)
        )
    ).all()
    return {row.product_id: row.quantity for row in rows}


# Добавляет delta к позиции в корзине (для «В корзину» из каталога).
async def add_cart_item_delta(
    db: AsyncSession,
    company_id: UUID,
    product_id: UUID,
    delta: int,
) -> CartView:
    if delta <= 0:
        raise ValueError("Количество должно быть больше нуля")
    _require_orderable_product(await db.get(Product, product_id))
    available = await get_availability(db, product_id)
    cart = await get_or_create_cart(db, company_id)
    item = await db.scalar(
        select(CartItem).where(
            CartItem.cart_id == cart.id,
            CartItem.product_id == product_id,
        )
    )
    new_qty = delta + (item.quantity if item is not None else 0)
    if new_qty > available:
        raise ValueError(f"Недостаточно товара на складе (доступно {available})")
    if item is None:
        db.add(CartItem(cart_id=cart.id, product_id=product_id, quantity=new_qty))
    else:
        item.quantity = new_qty
    await db.commit()
    return await get_cart_view(db, company_id)


# Добавляет или обновляет позицию в корзине.
async def upsert_cart_item(
    db: AsyncSession,
    company_id: UUID,
    product_id: UUID,
    quantity: int,
) -> CartView:
    if quantity <= 0:
        raise ValueError("Количество должно быть больше нуля")
    _require_orderable_product(await db.get(Product, product_id))
    available = await get_availability(db, product_id)
    if quantity > available:
        raise ValueError(f"Недостаточно товара на складе (доступно {available})")
    cart = await get_or_create_cart(db, company_id)
    item = await db.scalar(
        select(CartItem).where(
            CartItem.cart_id == cart.id,
            CartItem.product_id == product_id,
        )
    )
    if item is None:
        db.add(CartItem(cart_id=cart.id, product_id=product_id, quantity=quantity))
    else:
        item.quantity = quantity
    await db.commit()
    return await get_cart_view(db, company_id)


# Удаляет позицию из корзины.
async def remove_cart_item(
    db: AsyncSession,
    company_id: UUID,
    product_id: UUID,
) -> CartView:
    cart = await get_or_create_cart(db, company_id)
    item = await db.scalar(
        select(CartItem).where(
            CartItem.cart_id == cart.id,
            CartItem.product_id == product_id,
        )
    )
    if item is not None:
        await db.delete(item)
        await db.commit()
    return await get_cart_view(db, company_id)


# Очищает все позиции корзины.
async def clear_cart_items(db: AsyncSession, cart_id: UUID) -> None:
    rows = (await db.scalars(select(CartItem).where(CartItem.cart_id == cart_id))).all()
    for row in rows:
        await db.delete(row)
