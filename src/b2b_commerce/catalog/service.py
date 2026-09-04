import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from uuid import UUID

from PIL import Image
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from b2b_commerce.audit.service import write_audit
from b2b_commerce.cart.models import CartItem
from b2b_commerce.catalog.models import Brand, Category, PriceHistory, Product, ProductImage
from b2b_commerce.enums import ProductStatus, ReservationStatus
from b2b_commerce.inventory.models import Inventory, InventoryReservation
from b2b_commerce.inventory.service import get_default_warehouse
from b2b_commerce.invoices.models import InvoiceItem

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


# Нормализует MIME-тип загружаемого изображения.
def normalize_image_content_type(content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized == "image/jpg":
        return "image/jpeg"
    return normalized


# Приводит JPEG к единому формату и возвращает bytes, MIME и расширение.
def prepare_image_upload(file_bytes: bytes, content_type: str) -> tuple[bytes, str, str]:
    normalized_type = normalize_image_content_type(content_type)
    if normalized_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("Разрешены только JPEG, PNG и WebP")
    if normalized_type == "image/jpeg":
        image = Image.open(BytesIO(file_bytes))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        return buffer.getvalue(), "image/jpeg", "jpg"
    if normalized_type == "image/png":
        return file_bytes, "image/png", "png"
    return file_bytes, "image/webp", "webp"


@dataclass
class ProductInput:
    name: str
    brand_id: UUID | None = None
    category_id: UUID | None = None
    description: str | None = None
    cost_price: Decimal | None = None
    sale_price: Decimal = Decimal("0")
    model_year: int | None = None
    status: str = ProductStatus.INACTIVE


# Считает маржу в процентах от sale_price.
def compute_margin(cost_price: Decimal | None, sale_price: Decimal) -> Decimal | None:
    if sale_price <= 0 or cost_price is None:
        return None
    return ((sale_price - cost_price) / sale_price * 100).quantize(Decimal("0.01"))


# Список категорий для форм.
async def list_categories(db: AsyncSession) -> list[Category]:
    return list(await db.scalars(select(Category).order_by(Category.name)))


@dataclass
class CategoryRow:
    id: UUID
    name: str
    slug: str
    product_count: int
    margin_percent: Decimal | None


# Список категорий с числом товаров для админки.
async def list_category_rows(db: AsyncSession) -> list[CategoryRow]:
    rows = await db.execute(
        select(
            Category.id,
            Category.name,
            Category.slug,
            Category.margin_percent,
            func.count(Product.id),
        )
        .outerjoin(
            Product,
            (Product.category_id == Category.id) & (Product.deleted_at.is_(None)),
        )
        .group_by(Category.id, Category.name, Category.slug, Category.margin_percent)
        .order_by(Category.name)
    )
    return [
        CategoryRow(
            id=row[0],
            name=row[1],
            slug=row[2],
            product_count=int(row[4]),
            margin_percent=row[3],
        )
        for row in rows
    ]


# Возвращает категорию по id.
async def get_category(db: AsyncSession, category_id: UUID) -> Category | None:
    return await db.get(Category, category_id)


# Считает активные товары в категории.
async def count_products_in_category(db: AsyncSession, category_id: UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                Product.category_id == category_id,
                Product.deleted_at.is_(None),
            )
        )
        or 0
    )


# Slug категории из названия (импорт или транслит).
def _category_slug_for_name(name: str) -> str:
    label = name.strip()
    return IMPORT_CATEGORY_SLUGS.get(label, _name_slug(label))


# Создаёт категорию каталога.
async def create_category(
    db: AsyncSession,
    name: str,
    admin_id: UUID,
) -> Category:
    label = name.strip()
    if not label:
        raise ValueError("Укажите название категории")
    slug = await _unique_slug(db, Category, _category_slug_for_name(label))
    category = Category(name=label, slug=slug)
    db.add(category)
    await db.flush()
    await write_audit(
        db,
        actor_type="admin",
        actor_id=admin_id,
        action="category.create",
        entity_type="category",
        entity_id=category.id,
        payload={"name": label, "slug": slug},
    )
    await db.commit()
    await db.refresh(category)
    return category


# Обновляет название категории (slug не меняется).
async def update_category(
    db: AsyncSession,
    category_id: UUID,
    name: str,
    admin_id: UUID,
) -> Category | None:
    category = await db.get(Category, category_id)
    if category is None:
        return None
    label = name.strip()
    if not label:
        raise ValueError("Укажите название категории")
    old_name = category.name
    category.name = label
    await write_audit(
        db,
        actor_type="admin",
        actor_id=admin_id,
        action="category.update",
        entity_type="category",
        entity_id=category.id,
        payload={"old_name": old_name, "new_name": label},
    )
    await db.commit()
    await db.refresh(category)
    return category


# Удаляет категорию без товаров.
async def delete_category(
    db: AsyncSession,
    category_id: UUID,
    admin_id: UUID,
) -> bool:
    category = await db.get(Category, category_id)
    if category is None:
        return False
    product_count = await count_products_in_category(db, category_id)
    if product_count > 0:
        raise ValueError(
            f"В категории {product_count} товар(ов) — удаление невозможно"
        )
    await write_audit(
        db,
        actor_type="admin",
        actor_id=admin_id,
        action="category.delete",
        entity_type="category",
        entity_id=category.id,
        payload={"name": category.name},
    )
    await db.delete(category)
    await db.commit()
    return True




# Обновляет маржу категории.
async def update_category_margin(
    db: AsyncSession,
    category_id: UUID,
    margin_percent: Decimal | None,
    admin_id: UUID,
) -> Category | None:
    category = await db.get(Category, category_id)
    if category is None:
        return None
    old_margin = category.margin_percent
    category.margin_percent = margin_percent
    await write_audit(
        db,
        actor_type="admin",
        actor_id=admin_id,
        action="category.margin.update",
        entity_type="category",
        entity_id=category.id,
        payload={
            "name": category.name,
            "old_margin_percent": str(old_margin) if old_margin is not None else None,
            "new_margin_percent": str(margin_percent) if margin_percent is not None else None,
        },
    )
    await db.commit()
    await db.refresh(category)
    return category


# Пересчитывает sale_price по марже категории.
async def reprice_products_from_categories(db: AsyncSession, actor_id: UUID) -> int:
    stmt = (
        select(Product)
        .join(Category, Product.category_id == Category.id)
        .where(
            Product.deleted_at.is_(None),
            Product.cost_price.is_not(None),
            Category.margin_percent.is_not(None),
        )
        .options(selectinload(Product.category))
    )
    products = (await db.scalars(stmt)).all()
    updated = 0
    for product in products:
        category = product.category
        if category is None or category.margin_percent is None or product.cost_price is None:
            continue
        factor = Decimal("1") + category.margin_percent / Decimal("100")
        new_price = (product.cost_price * factor).quantize(Decimal("0.01"))
        if product.sale_price == new_price:
            continue
        old_price = product.sale_price
        product.sale_price = new_price
        product.margin_percent = compute_margin(product.cost_price, new_price)
        await write_audit(
            db,
            actor_type="admin",
            actor_id=actor_id,
            action="product.reprice",
            entity_type="product",
            entity_id=product.id,
            payload={
                "old_sale_price": str(old_price),
                "new_sale_price": str(new_price),
                "category_id": str(category.id),
                "margin_percent": str(category.margin_percent),
            },
        )
        updated += 1
    await db.commit()
    return updated


# Ставит переоценку в очередь worker.
async def enqueue_product_repricing(actor_id: UUID) -> str:
    from arq import create_pool
    from arq.connections import RedisSettings

    from b2b_commerce.config import get_settings

    redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        job = await redis.enqueue_job("reprice_products", str(actor_id))
        if job is None:
            raise RuntimeError("Не удалось поставить задачу переоценки в очередь")
        return job.job_id
    finally:
        await redis.aclose()

@dataclass
class BrandRow:
    id: UUID
    name: str
    slug: str
    product_count: int


# Список брендов с числом товаров для админки.
async def list_brand_rows(db: AsyncSession) -> list[BrandRow]:
    rows = await db.execute(
        select(
            Brand.id,
            Brand.name,
            Brand.slug,
            func.count(Product.id),
        )
        .outerjoin(
            Product,
            (Product.brand_id == Brand.id) & (Product.deleted_at.is_(None)),
        )
        .group_by(Brand.id, Brand.name, Brand.slug)
        .order_by(Brand.name)
    )
    return [
        BrandRow(
            id=row[0],
            name=row[1],
            slug=row[2],
            product_count=int(row[3]),
        )
        for row in rows
    ]


# Возвращает бренд по id.
async def get_brand(db: AsyncSession, brand_id: UUID) -> Brand | None:
    return await db.get(Brand, brand_id)


# Считает активные товары бренда.
async def count_products_in_brand(db: AsyncSession, brand_id: UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                Product.brand_id == brand_id,
                Product.deleted_at.is_(None),
            )
        )
        or 0
    )


# Создаёт бренд каталога.
async def create_brand(
    db: AsyncSession,
    name: str,
    admin_id: UUID,
) -> Brand:
    label = name.strip()
    if not label:
        raise ValueError("Укажите название бренда")
    existing = await db.scalar(select(Brand).where(Brand.name == label))
    if existing is not None:
        raise ValueError("Бренд с таким названием уже есть")
    slug = await _unique_slug(db, Brand, _name_slug(label))
    brand = Brand(name=label, slug=slug)
    db.add(brand)
    await db.flush()
    await write_audit(
        db,
        actor_type="admin",
        actor_id=admin_id,
        action="brand.create",
        entity_type="brand",
        entity_id=brand.id,
        payload={"name": label, "slug": slug},
    )
    await db.commit()
    await db.refresh(brand)
    return brand


# Обновляет название бренда (slug не меняется).
async def update_brand(
    db: AsyncSession,
    brand_id: UUID,
    name: str,
    admin_id: UUID,
) -> Brand | None:
    brand = await db.get(Brand, brand_id)
    if brand is None:
        return None
    label = name.strip()
    if not label:
        raise ValueError("Укажите название бренда")
    duplicate = await db.scalar(
        select(Brand.id).where(Brand.name == label, Brand.id != brand_id)
    )
    if duplicate is not None:
        raise ValueError("Бренд с таким названием уже есть")
    old_name = brand.name
    brand.name = label
    await write_audit(
        db,
        actor_type="admin",
        actor_id=admin_id,
        action="brand.update",
        entity_type="brand",
        entity_id=brand.id,
        payload={"old_name": old_name, "new_name": label},
    )
    await db.commit()
    await db.refresh(brand)
    return brand


# Удаляет бренд без товаров.
async def delete_brand(
    db: AsyncSession,
    brand_id: UUID,
    admin_id: UUID,
) -> bool:
    brand = await db.get(Brand, brand_id)
    if brand is None:
        return False
    product_count = await count_products_in_brand(db, brand_id)
    if product_count > 0:
        raise ValueError(
            f"У бренда {product_count} товар(ов) — удаление невозможно"
        )
    await write_audit(
        db,
        actor_type="admin",
        actor_id=admin_id,
        action="brand.delete",
        entity_type="brand",
        entity_id=brand.id,
        payload={"name": brand.name},
    )
    await db.delete(brand)
    await db.commit()
    return True


# Список брендов для форм.
async def list_brands(db: AsyncSession) -> list[Brand]:
    return list(await db.scalars(select(Brand).order_by(Brand.name)))


# Список активных товаров для select (счёт, формы).
async def list_active_products(db: AsyncSession, limit: int = 200) -> list[Product]:
    return list(
        await db.scalars(
            select(Product)
            .where(
                Product.deleted_at.is_(None),
                Product.status == ProductStatus.ACTIVE,
            )
            .order_by(Product.name.asc())
            .limit(limit)
        )
    )


# Возвращает название бренда для денормализации search.
async def _brand_name_for(db: AsyncSession, brand_id: UUID | None) -> str | None:
    if brand_id is None:
        return None
    return await db.scalar(select(Brand.name).where(Brand.id == brand_id))


# Список товаров для админки.
ADMIN_PRODUCT_SORTS = frozenset(
    {"price_asc", "price_desc", "date_desc", "date_asc", "stock_desc", "stock_asc"}
)

STOREFRONT_SORTS = ADMIN_PRODUCT_SORTS


# Нормализует сортировку для витрины.
def normalize_storefront_sort(value: str | None) -> str | None:
    if value in STOREFRONT_SORTS:
        return value
    return None


# Нормализует сортировку для админки.
def normalize_admin_sort(value: str | None) -> str:
    if value in ADMIN_PRODUCT_SORTS:
        return value
    return "date_desc"


# Применяет фильтры для товаров в админке.
def _apply_admin_product_filters(
    stmt,
    q: str | None = None,
    brand_id: UUID | None = None,
    category_id: UUID | None = None,
    model_year: int | None = None,
    status: str | None = None,
    include_deleted: bool = False,
):
    if status:
        stmt = stmt.where(Product.status == status)
    if brand_id is not None:
        stmt = stmt.where(Product.brand_id == brand_id)
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if model_year is not None:
        stmt = stmt.where(Product.model_year == model_year)
    if not include_deleted:
        stmt = stmt.where(Product.deleted_at.is_(None))
    return _apply_storefront_search(stmt, q)


# Присоединяет available stock для сортировки админки.
async def _admin_products_stmt_with_stock_join(
    db: AsyncSession,
    stmt,
    normalized_sort: str,
) -> tuple[object, bool]:
    if normalized_sort not in {"stock_asc", "stock_desc"}:
        return stmt, False
    warehouse = await get_default_warehouse(db)
    stmt = stmt.outerjoin(
        Inventory,
        (Inventory.product_id == Product.id) & (Inventory.warehouse_id == warehouse.id),
    )
    return stmt, True


# Считает количество товаров в админке.
async def count_products_admin(
    db: AsyncSession,
    q: str | None = None,
    brand_id: UUID | None = None,
    category_id: UUID | None = None,
    model_year: int | None = None,
    status: str | None = None,
    include_deleted: bool = False,
) -> int:
    stmt = select(func.count()).select_from(Product)
    stmt, _ = _apply_admin_product_filters(
        stmt,
        q=q,
        brand_id=brand_id,
        category_id=category_id,
        model_year=model_year,
        status=status,
        include_deleted=include_deleted,
    )
    return int(await db.scalar(stmt) or 0)


# Список товаров в админке.
async def list_products_admin(
    db: AsyncSession,
    q: str | None = None,
    brand_id: UUID | None = None,
    category_id: UUID | None = None,
    model_year: int | None = None,
    status: str | None = None,
    sort: str = "date_desc",
    include_deleted: bool = False,
    offset: int = 0,
    limit: int | None = None,
) -> list[Product]:
    normalized_sort = normalize_admin_sort(sort)

    stmt = select(Product).options(
        selectinload(Product.images),
        selectinload(Product.category),
    )
    stmt, need_stock_join = await _admin_products_stmt_with_stock_join(db, stmt, normalized_sort)
    stmt, rank = _apply_admin_product_filters(
        stmt,
        q=q,
        brand_id=brand_id,
        category_id=category_id,
        model_year=model_year,
        status=status,
        include_deleted=include_deleted,
    )
    if rank is not None:
        stmt = stmt.order_by(rank.desc(), Product.created_at.desc())
    elif normalized_sort == "price_asc":
        stmt = stmt.order_by(Product.sale_price.asc(), Product.name.asc())
    elif normalized_sort == "price_desc":
        stmt = stmt.order_by(Product.sale_price.desc(), Product.name.asc())
    elif normalized_sort == "date_asc":
        stmt = stmt.order_by(Product.created_at.asc())
    elif normalized_sort == "stock_asc":
        stock_qty = func.coalesce(Inventory.quantity_on_hand, 0)
        stmt = stmt.order_by(stock_qty.asc(), Product.name.asc())
    elif normalized_sort == "stock_desc":
        stock_qty = func.coalesce(Inventory.quantity_on_hand, 0)
        stmt = stmt.order_by(stock_qty.desc(), Product.name.asc())
    else:
        stmt = stmt.order_by(Product.created_at.desc())

    if limit is not None:
        stmt = stmt.offset(offset).limit(limit)

    if need_stock_join:
        return list((await db.scalars(stmt)).unique())
    return list(await db.scalars(stmt))


STOREFRONT_PAGE_SIZE = 30


STOREFRONT_CATEGORY_LABELS = {
    "balls": "Мячи",
    "rackets": "Ракетки",
    "racket": "Ракетки",
    "shoes": "Обувь",
}

IMPORT_CATEGORY_SLUGS = {
    "Мячи": "myachi",
    "Ракетки": "raketki",
    "Аксессуары": "aksessuary",
}


# Slug для бренда или категории.
def _name_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip())
    return slug.strip("-") or "item"


# Подбирает уникальный slug в таблице brands или categories.
async def _unique_slug(db: AsyncSession, model, base_slug: str) -> str:
    slug = base_slug
    suffix = 2
    while await db.scalar(select(model.id).where(model.slug == slug)) is not None:
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    return slug


# Возвращает бренд, создавая запись при отсутствии.
async def get_or_create_brand(db: AsyncSession, name: str) -> Brand:
    cleaned = name.strip()
    brand = await db.scalar(select(Brand).where(Brand.name == cleaned))
    if brand is not None:
        return brand
    slug = await _unique_slug(db, Brand, _name_slug(cleaned))
    brand = Brand(name=cleaned, slug=slug)
    db.add(brand)
    await db.flush()
    return brand


# Возвращает категорию по русскому названию из Excel.
async def get_or_create_category_by_name(db: AsyncSession, name: str) -> Category:
    label = name.strip()
    slug = IMPORT_CATEGORY_SLUGS.get(label, _name_slug(label))
    category = await db.scalar(select(Category).where(Category.slug == slug))
    if category is None:
        category = Category(name=label, slug=slug)
        db.add(category)
        await db.flush()
    elif category.name != label:
        category.name = label
    return category


# Возвращает русское название категории витрины.
def storefront_category_label(name: str) -> str:
    return STOREFRONT_CATEGORY_LABELS.get(name, name)



@dataclass
class StorefrontCategoryStat:
    id: UUID
    name: str
    count: int


@dataclass
class StorefrontBrandStat:
    id: UUID
    name: str
    count: int


@dataclass
class StorefrontModelYearStat:
    year: int
    count: int


# Добавляет фильтры витрины по статусу, категории, бренду и году.
def _apply_storefront_filters(
    stmt,
    category_id: UUID | None = None,
    brand_id: UUID | None = None,
    model_year: int | None = None,
):
    stmt = stmt.where(Product.status == ProductStatus.ACTIVE)
    stmt = stmt.where(Product.deleted_at.is_(None))
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if brand_id is not None:
        stmt = stmt.where(Product.brand_id == brand_id)
    if model_year is not None:
        stmt = stmt.where(Product.model_year == model_year)
    return stmt


# Полнотекстовый поиск: название, бренд, описание (english + russian).
def _apply_storefront_search(stmt, search: str | None):
    term = search.strip() if search else ""
    if not term:
        return stmt, None
    ts_query_en = func.websearch_to_tsquery("english", term)
    ts_query_ru = func.websearch_to_tsquery("russian", term)
    ts_match = or_(
        Product.search_tsv.op("@@")(ts_query_en),
        Product.search_tsv.op("@@")(ts_query_ru),
    )
    rank = func.greatest(
        func.ts_rank_cd(Product.search_tsv, ts_query_en),
        func.ts_rank_cd(Product.search_tsv, ts_query_ru),
    )
    return stmt.where(ts_match), rank


# Категории с числом активных товаров для фильтров витрины.
async def list_storefront_category_stats(db: AsyncSession) -> list[StorefrontCategoryStat]:
    rows = await db.execute(
        select(Category.id, Category.name, func.count(Product.id))
        .join(Product, Product.category_id == Category.id)
        .where(Product.status == ProductStatus.ACTIVE)
        .where(Product.deleted_at.is_(None))
        .group_by(Category.id, Category.name)
        .order_by(Category.name)
    )
    return [
        StorefrontCategoryStat(id=row[0], name=storefront_category_label(row[1]), count=int(row[2]))
        for row in rows.all()
    ]


# Бренды с числом активных товаров для фильтров витрины.
async def list_storefront_brand_stats(db: AsyncSession) -> list[StorefrontBrandStat]:
    rows = await db.execute(
        select(Brand.id, Brand.name, func.count(Product.id))
        .join(Product, Product.brand_id == Brand.id)
        .where(Product.status == ProductStatus.ACTIVE)
        .where(Product.deleted_at.is_(None))
        .group_by(Brand.id, Brand.name)
        .order_by(Brand.name)
    )
    return [
        StorefrontBrandStat(id=row[0], name=row[1], count=int(row[2]))
        for row in rows.all()
    ]


# Годы моделей с числом активных товаров для фильтров витрины.
async def list_storefront_model_year_stats(db: AsyncSession) -> list[StorefrontModelYearStat]:
    rows = await db.execute(
        select(Product.model_year, func.count(Product.id))
        .where(Product.status == ProductStatus.ACTIVE)
        .where(Product.deleted_at.is_(None))
        .where(Product.model_year.is_not(None))
        .group_by(Product.model_year)
        .order_by(Product.model_year.desc())
    )
    return [
        StorefrontModelYearStat(year=int(row[0]), count=int(row[1]))
        for row in rows.all()
    ]


# Годы моделей с числом товаров для фильтров админки.
async def list_admin_model_year_stats(db: AsyncSession) -> list[StorefrontModelYearStat]:
    rows = await db.execute(
        select(Product.model_year, func.count(Product.id))
        .where(Product.deleted_at.is_(None))
        .where(Product.model_year.is_not(None))
        .group_by(Product.model_year)
        .order_by(Product.model_year.desc())
    )
    return [
        StorefrontModelYearStat(year=int(row[0]), count=int(row[1]))
        for row in rows.all()
    ]


# Считает активные товары витрины.
async def count_products_storefront(
    db: AsyncSession,
    category_id: UUID | None = None,
    brand_id: UUID | None = None,
    model_year: int | None = None,
    search: str | None = None,
) -> int:
    stmt = select(func.count()).select_from(Product)
    stmt = _apply_storefront_filters(
        stmt,
        category_id=category_id,
        brand_id=brand_id,
        model_year=model_year,
    )
    stmt, _ = _apply_storefront_search(stmt, search)
    return int(await db.scalar(stmt) or 0)


# Подключает available stock для сортировки витрины.
def _storefront_products_stmt_with_available_join(stmt, normalized_sort: str | None):
    if normalized_sort not in {"stock_asc", "stock_desc"}:
        return stmt, None
    on_hand_sq = (
        select(
            Inventory.product_id.label("product_id"),
            func.coalesce(func.sum(Inventory.quantity_on_hand), 0).label("on_hand"),
        )
        .group_by(Inventory.product_id)
        .subquery()
    )
    reserved_sq = (
        select(
            InventoryReservation.product_id.label("product_id"),
            func.coalesce(func.sum(InventoryReservation.quantity), 0).label("reserved"),
        )
        .where(InventoryReservation.status == ReservationStatus.ACTIVE)
        .group_by(InventoryReservation.product_id)
        .subquery()
    )
    stmt = stmt.outerjoin(on_hand_sq, on_hand_sq.c.product_id == Product.id)
    stmt = stmt.outerjoin(reserved_sq, reserved_sq.c.product_id == Product.id)
    available = func.coalesce(on_hand_sq.c.on_hand, 0) - func.coalesce(reserved_sq.c.reserved, 0)
    return stmt, available


# Применяет ORDER BY для витрины.
def _apply_storefront_order(
    stmt,
    normalized_sort: str | None,
    rank,
    available_expr,
):
    if normalized_sort is None:
        if rank is not None:
            return stmt.order_by(rank.desc(), Product.name.asc(), Product.id.asc())
        return stmt.order_by(Product.name.asc(), Product.id.asc())
    if normalized_sort == "price_asc":
        return stmt.order_by(Product.sale_price.asc(), Product.name.asc(), Product.id.asc())
    if normalized_sort == "price_desc":
        return stmt.order_by(Product.sale_price.desc(), Product.name.asc(), Product.id.asc())
    if normalized_sort == "date_asc":
        return stmt.order_by(Product.created_at.asc(), Product.name.asc(), Product.id.asc())
    if normalized_sort == "date_desc":
        return stmt.order_by(Product.created_at.desc(), Product.name.asc(), Product.id.asc())
    if normalized_sort == "stock_asc":
        return stmt.order_by(available_expr.asc(), Product.name.asc(), Product.id.asc())
    if normalized_sort == "stock_desc":
        return stmt.order_by(available_expr.desc(), Product.name.asc(), Product.id.asc())
    return stmt.order_by(Product.name.asc(), Product.id.asc())


# Список активных товаров для витрины.
async def list_products_storefront(
    db: AsyncSession,
    offset: int = 0,
    limit: int = STOREFRONT_PAGE_SIZE,
    category_id: UUID | None = None,
    brand_id: UUID | None = None,
    model_year: int | None = None,
    search: str | None = None,
    sort: str | None = None,
) -> list[Product]:
    normalized_sort = normalize_storefront_sort(sort)

    stmt = select(Product).options(
        selectinload(Product.images),
        selectinload(Product.category),
    )
    stmt = _apply_storefront_filters(
        stmt,
        category_id=category_id,
        brand_id=brand_id,
        model_year=model_year,
    )
    stmt, rank = _apply_storefront_search(stmt, search)
    stmt, available_expr = _storefront_products_stmt_with_available_join(stmt, normalized_sort)
    stmt = _apply_storefront_order(
        stmt,
        normalized_sort=normalized_sort,
        rank=rank,
        available_expr=available_expr,
    )
    stmt = stmt.offset(offset).limit(limit)
    if normalized_sort in {"stock_asc", "stock_desc"}:
        return list((await db.scalars(stmt)).unique())
    return list(await db.scalars(stmt))


# Карточка товара с фото и историей цен.
async def get_product_detail(db: AsyncSession, product_id: UUID) -> Product | None:
    stmt = (
        select(Product)
        .options(
            selectinload(Product.images),
            selectinload(Product.category),
            selectinload(Product.price_history),
        )
        .where(Product.id == product_id)
        .where(Product.deleted_at.is_(None))
    )
    return await db.scalar(stmt)


# Добавляет записи price_history при изменении цен.
async def _record_price_history(
    db: AsyncSession,
    product: Product,
    actor_id: UUID,
) -> None:
    db.add(
        PriceHistory(
            product_id=product.id,
            sale_price=product.sale_price,
            cost_price=product.cost_price,
            actor_id=actor_id,
        )
    )


# Создаёт товар.
async def create_product(
    db: AsyncSession,
    data: ProductInput,
    actor_id: UUID,
    audit_action: str = "product.create",
) -> Product:
    name = data.name.strip()
    if not name:
        raise ValueError("Укажите название товара")
    if (data.cost_price is not None and data.cost_price < 0) or data.sale_price < 0:
        raise ValueError("Цена не может быть отрицательной")
    if data.status not in {ProductStatus.ACTIVE, ProductStatus.INACTIVE}:
        raise ValueError("Некорректный статус товара")

    product = Product(
        name=name,
        brand_id=data.brand_id,
        brand_name=await _brand_name_for(db, data.brand_id),
        category_id=data.category_id,
        description=data.description or None,
        model_year=data.model_year,
        cost_price=data.cost_price,
        sale_price=data.sale_price,
        margin_percent=compute_margin(data.cost_price, data.sale_price),
        status=data.status,
    )
    db.add(product)
    await db.flush()
    await _record_price_history(db, product, actor_id)
    await write_audit(
        db,
        actor_type="admin",
        actor_id=actor_id,
        action=audit_action,
        entity_type="product",
        entity_id=product.id,
        payload={"name": product.name},
    )
    await db.commit()
    await db.refresh(product)
    return product


# Обновляет поля товара.
async def update_product(
    db: AsyncSession,
    product_id: UUID,
    data: ProductInput,
    actor_id: UUID,
    audit_action: str = "product.update",
) -> Product | None:
    product = await db.get(Product, product_id)
    if product is None:
        return None
    name = data.name.strip()
    if not name:
        raise ValueError("Укажите название товара")
    if (data.cost_price is not None and data.cost_price < 0) or data.sale_price < 0:
        raise ValueError("Цена не может быть отрицательной")
    if data.status not in {ProductStatus.ACTIVE, ProductStatus.INACTIVE}:
        raise ValueError("Некорректный статус товара")

    price_changed = (
        product.cost_price != data.cost_price or product.sale_price != data.sale_price
    )
    product.name = name
    product.brand_id = data.brand_id
    product.brand_name = await _brand_name_for(db, data.brand_id)
    product.category_id = data.category_id
    product.description = data.description or None
    product.model_year = data.model_year
    product.cost_price = data.cost_price
    product.sale_price = data.sale_price
    product.margin_percent = compute_margin(data.cost_price, data.sale_price)
    product.status = data.status
    if price_changed:
        await _record_price_history(db, product, actor_id)
    await write_audit(
        db,
        actor_type="admin",
        actor_id=actor_id,
        action=audit_action,
        entity_type="product",
        entity_id=product.id,
        payload={"name": product.name},
    )
    await db.commit()
    await db.refresh(product)
    return product



# Мягкое удаление товара с проверкой связей.
async def soft_delete_product(
    db: AsyncSession,
    product_id: UUID,
    actor_id: UUID,
) -> Product | None:
    product = await db.get(Product, product_id)
    if product is None:
        return None
    if product.deleted_at is not None:
        raise ValueError("Товар уже удалён")

    invoice_refs = await db.scalar(
        select(func.count()).select_from(InvoiceItem).where(InvoiceItem.product_id == product_id)
    )
    if int(invoice_refs or 0) > 0:
        raise ValueError(
            "Товар в счетах — удаление недоступно. Переведите в «Не активен»."
        )

    cart_refs = await db.scalar(
        select(func.count()).select_from(CartItem).where(CartItem.product_id == product_id)
    )
    if int(cart_refs or 0) > 0:
        raise ValueError(
            "Товар в корзине — удаление недоступно. Переведите в «Не активен»."
        )

    reservation_refs = await db.scalar(
        select(func.count())
        .select_from(InventoryReservation)
        .where(
            InventoryReservation.product_id == product_id,
            InventoryReservation.status == ReservationStatus.ACTIVE.value,
        )
    )
    if int(reservation_refs or 0) > 0:
        raise ValueError("Товар в активном резерве — удаление недоступно.")

    product.deleted_at = datetime.now(UTC)
    await write_audit(
        db,
        actor_type="admin",
        actor_id=actor_id,
        action="product.delete",
        entity_type="product",
        entity_id=product.id,
        payload={"name": product.name},
    )
    await db.commit()
    await db.refresh(product)
    return product


# Загружает фото товара в object storage и сохраняет ключ.
async def add_product_image(
    db: AsyncSession,
    product_id: UUID,
    file_bytes: bytes,
    content_type: str,
    storage,
    actor_id: UUID,
) -> ProductImage | None:
    if len(file_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("Файл больше 5 МБ")
    try:
        prepared_bytes, normalized_type, ext = await asyncio.to_thread(
            prepare_image_upload, file_bytes, content_type
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Не удалось обработать изображение") from exc
    product = await db.get(Product, product_id)
    if product is None:
        return None

    storage_key = f"products/{product_id}/{uuid.uuid4()}.{ext}"
    await storage.put_object_async(storage_key, prepared_bytes, normalized_type)

    max_order = await db.scalar(
        select(func.coalesce(func.max(ProductImage.sort_order), -1)).where(
            ProductImage.product_id == product_id
        )
    )
    image = ProductImage(
        product_id=product_id,
        storage_key=storage_key,
        sort_order=int(max_order) + 1,
    )
    db.add(image)
    await write_audit(
        db,
        actor_type="admin",
        actor_id=actor_id,
        action="product.image.add",
        entity_type="product",
        entity_id=product_id,
        payload={"storage_key": storage_key},
    )
    await db.commit()
    await db.refresh(image)
    return image




# Загружает несколько фото товара за один раз.
async def add_product_images(
    db: AsyncSession,
    product_id: UUID,
    uploads: list[tuple[bytes, str]],
    storage,
    actor_id: UUID,
) -> list[ProductImage]:
    if not uploads:
        raise ValueError("Выберите хотя бы один файл")
    saved: list[ProductImage] = []
    for file_bytes, content_type in uploads:
        image = await add_product_image(
            db, product_id, file_bytes, content_type, storage, actor_id
        )
        if image is None:
            raise ValueError("Товар не найден")
        saved.append(image)
    return saved
