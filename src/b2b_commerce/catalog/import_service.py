import asyncio
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.catalog.models import Brand, Category, Product, ProductImage
from b2b_commerce.catalog.price_import import (
    ImportReport,
    ImportRowResult,
    ImportRowStatus,
    ProductImportRow,
    ValidatedImportRow,
    normalize_match_text,
    parse_excel_price_file,
)
from b2b_commerce.catalog.service import (
    ProductInput,
    add_product_image,
    create_product,
    get_or_create_brand,
    get_or_create_category_by_name,
    update_product,
)
from b2b_commerce.enums import ProductStatus
from b2b_commerce.inventory.service import (
    correct_inventory,
    get_default_warehouse,
    get_inventory_row,
)

IMPORT_REASON = "Импорт прайса"
IMPORT_MAX_BYTES = 64 * 1024 * 1024
STAGING_DIR = Path(tempfile.gettempdir()) / "b2b-commerce-price-import"


@dataclass
class ImportPreviewResult:
    report: ImportReport
    confirm_token: str


# Преобразует статус в строку.
def map_status_hint(status_hint: str) -> str:
    if status_hint == "inactive":
        return ProductStatus.INACTIVE
    return ProductStatus.ACTIVE


# Сохраняет загруженный файл в временный каталог.
def stage_price_upload(data: bytes, actor_id: UUID) -> str:
    if len(data) > IMPORT_MAX_BYTES:
        raise ValueError("Файл больше 64 МБ")
    if not data:
        raise ValueError("Файл пуст")
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    path = STAGING_DIR / f"{actor_id}-{token}.xlsx"
    path.write_bytes(data)
    return token


# Возвращает путь к загруженному файлу.
def resolve_staged_upload(actor_id: UUID, token: str) -> Path:
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("Сессия импорта не найдена")
    path = STAGING_DIR / f"{actor_id}-{cleaned}.xlsx"
    if not path.is_file():
        raise ValueError("Загрузка истекла — загрузите файл снова")
    return path


# Удаляет загруженный файл.
def discard_staged_upload(actor_id: UUID, token: str) -> None:
    cleaned = token.strip()
    if not cleaned:
        return
    path = STAGING_DIR / f"{actor_id}-{cleaned}.xlsx"
    path.unlink(missing_ok=True)


class ImportService:
    # Запускает импорт из файла.
    async def run_from_path(
        self,
        path: Path | str,
        db: AsyncSession,
        actor_id: UUID,
        storage=None,
        dry_run: bool = False,
    ) -> ImportReport:
        validated_rows, _ = await asyncio.to_thread(parse_excel_price_file, path)
        return await self.import_rows(
            db,
            [item.row for item in validated_rows],
            actor_id,
            storage=storage,
            validated=validated_rows,
            dry_run=dry_run,
        )

    # Запускает импорт из данных.
    async def run_upload(
        self,
        data: bytes,
        db: AsyncSession,
        actor_id: UUID,
        storage=None,
        dry_run: bool = False,
    ) -> ImportReport:
        if len(data) > IMPORT_MAX_BYTES:
            raise ValueError("Файл больше 64 МБ")
        if not data:
            raise ValueError("Файл пуст")
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(data)
            path = Path(tmp.name)
        try:
            return await self.run_from_path(
                path,
                db,
                actor_id,
                storage=storage,
                dry_run=dry_run,
            )
        finally:
            path.unlink(missing_ok=True)

    # Запускает предварительный просмотр импорта.
    async def preview_upload(
        self,
        data: bytes,
        db: AsyncSession,
        actor_id: UUID,
        storage=None,
    ) -> ImportPreviewResult:
        report = await self.run_upload(
            data,
            db,
            actor_id,
            storage=storage,
            dry_run=True,
        )
        token = stage_price_upload(data, actor_id)
        return ImportPreviewResult(report=report, confirm_token=token)

    # Запускает импорт из загруженного файла.
    async def apply_staged(
        self,
        actor_id: UUID,
        token: str,
        db: AsyncSession,
        storage=None,
    ) -> ImportReport:
        path = resolve_staged_upload(actor_id, token)
        report = await self.run_from_path(path, db, actor_id, storage=storage)
        discard_staged_upload(actor_id, token)
        return report

    # Запускает импорт из списка строк.
    async def import_rows(
        self,
        db: AsyncSession,
        rows: list[ProductImportRow],
        actor_id: UUID,
        storage=None,
        validated: list[ValidatedImportRow] | None = None,
        dry_run: bool = False,
    ) -> ImportReport:
        report = ImportReport()
        validated_by_row = {
            item.row.source_row: item for item in (validated or [])
        }

        for row in rows:
            validated_row = validated_by_row.get(row.source_row)
            if validated_row and validated_row.errors:
                report.errors += 1
                report.row_results.append(
                    ImportRowResult(
                        source_row=row.source_row,
                        name=row.name,
                        brand_name=row.brand_name,
                        status=ImportRowStatus.ERROR,
                        messages=list(validated_row.errors),
                    )
                )
                continue

            try:
                if dry_run:
                    result, preview_messages = await self._preview_single_row(db, row)
                    messages = list(preview_messages)
                    if validated_row:
                        messages.extend(validated_row.warnings)
                else:
                    result = await self._import_single_row(db, row, actor_id, storage)
                    messages = list(validated_row.warnings if validated_row else [])
            except ValueError as exc:
                report.errors += 1
                report.row_results.append(
                    ImportRowResult(
                        source_row=row.source_row,
                        name=row.name,
                        brand_name=row.brand_name,
                        status=ImportRowStatus.ERROR,
                        messages=[str(exc)],
                    )
                )
                continue

            if result == ImportRowStatus.CREATE:
                report.created += 1
            elif result == ImportRowStatus.SKIP:
                report.skipped += 1
            else:
                report.updated += 1
            report.warnings += len(messages)
            report.row_results.append(
                ImportRowResult(
                    source_row=row.source_row,
                    name=row.name,
                    brand_name=row.brand_name,
                    status=result,
                    messages=messages,
                )
            )
        return report

    # Запускает предварительный просмотр одной строки.
    async def _preview_single_row(
        self,
        db: AsyncSession,
        row: ProductImportRow,
    ) -> tuple[ImportRowStatus, list[str]]:
        brand = await db.scalar(select(Brand).where(Brand.name == row.brand_name.strip()))
        status = map_status_hint(row.status_hint)
        if brand is None:
            return ImportRowStatus.CREATE, self._create_preview_messages(row, status)

        matches = await self._find_matching_products(db, row, brand.id)
        if len(matches) > 1:
            raise ValueError("Найдено несколько товаров по name+brand+model_year")
        if not matches:
            return ImportRowStatus.CREATE, self._create_preview_messages(row, status)

        product = matches[0]
        warehouse = await get_default_warehouse(db)
        inventory = await get_inventory_row(db, product.id, warehouse.id)
        on_hand = inventory.quantity_on_hand if inventory is not None else 0
        messages = await self._diff_messages(db, product, row, status, on_hand)
        if messages == ["Без изменений"]:
            return ImportRowStatus.SKIP, messages
        return ImportRowStatus.UPDATE, messages

    # Создает сообщения для предварительного просмотра.
    def _create_preview_messages(
        self,
        row: ProductImportRow,
        status: str,
    ) -> list[str]:
        return [
            "Новый товар",
            f"Цена: {row.sale_price}",
            f"Остаток: {row.stock_quantity} ({row.stock_label})",
            f"Статус: {status}",
        ]

    # Создает сообщения для различий между товаром и строкой.
    async def _diff_messages(
        self,
        db: AsyncSession,
        product: Product,
        row: ProductImportRow,
        status: str,
        on_hand: int,
    ) -> list[str]:
        messages: list[str] = []
        if product.sale_price != row.sale_price:
            messages.append(f"Цена: {product.sale_price} → {row.sale_price}")
        if on_hand != row.stock_quantity:
            messages.append(f"Остаток: {on_hand} → {row.stock_quantity}")
        if product.status != status:
            messages.append(f"Статус: {product.status} → {status}")
        if product.model_year != row.model_year:
            messages.append(f"Год: {product.model_year} → {row.model_year}")
        if product.category_id is not None:
            category = await db.get(Category, product.category_id)
            if category is not None and category.name != row.category_name:
                messages.append(f"Категория: {category.name} → {row.category_name}")
        elif row.category_name:
            messages.append(f"Категория: — → {row.category_name}")
        if not messages:
            return ["Без изменений"]
        return messages

    # Запускает импорт одной строки.
    async def _import_single_row(
        self,
        db: AsyncSession,
        row: ProductImportRow,
        actor_id: UUID,
        storage,
    ) -> ImportRowStatus:
        brand = await get_or_create_brand(db, row.brand_name)
        category = await get_or_create_category_by_name(db, row.category_name)
        matches = await self._find_matching_products(db, row, brand.id)
        if len(matches) > 1:
            raise ValueError("Найдено несколько товаров по name+brand+model_year")

        status = map_status_hint(row.status_hint)
        if matches:
            product = matches[0]
            await self._update_product(db, product, row, category.id, status, actor_id)
            action = ImportRowStatus.UPDATE
        else:
            product = await self._create_product(
                db,
                row,
                brand.id,
                category.id,
                status,
                actor_id,
            )
            action = ImportRowStatus.CREATE

        await correct_inventory(db, product.id, row.stock_quantity, IMPORT_REASON, actor_id)
        await self._sync_product_image(db, product.id, row, storage, actor_id)
        return action

    # Находит товары, соответствующие строке.
    async def _find_matching_products(
        self,
        db: AsyncSession,
        row: ProductImportRow,
        brand_id: UUID,
    ) -> list[Product]:
        normalized = normalize_match_text(row.name)
        stmt = (
            select(Product)
            .where(func.lower(func.trim(Product.name)) == normalized)
            .where(Product.brand_id == brand_id)
            .where(Product.model_year.is_not_distinct_from(row.model_year))
            .where(Product.deleted_at.is_(None))
            .order_by(Product.created_at.asc())
        )
        return list(await db.scalars(stmt))

    # Создает товар.
    async def _create_product(
        self,
        db: AsyncSession,
        row: ProductImportRow,
        brand_id: UUID,
        category_id: UUID,
        status: str,
        actor_id: UUID,
    ) -> Product:
        return await create_product(
            db,
            ProductInput(
                name=row.name,
                brand_id=brand_id,
                category_id=category_id,
                model_year=row.model_year,
                cost_price=None,
                sale_price=row.sale_price,
                status=status,
            ),
            actor_id,
            audit_action="product.import.create",
        )

    # Обновляет товар.
    async def _update_product(
        self,
        db: AsyncSession,
        product: Product,
        row: ProductImportRow,
        category_id: UUID,
        status: str,
        actor_id: UUID,
    ) -> None:
        await update_product(
            db,
            product.id,
            ProductInput(
                name=row.name,
                brand_id=product.brand_id,
                category_id=category_id,
                description=product.description,
                model_year=row.model_year,
                cost_price=product.cost_price,
                sale_price=row.sale_price,
                status=status,
            ),
            actor_id,
            audit_action="product.import.update",
        )

    # Синхронизирует изображение товара.
    async def _sync_product_image(
        self,
        db: AsyncSession,
        product_id: UUID,
        row: ProductImportRow,
        storage,
        actor_id: UUID,
    ) -> None:
        if row.image_bytes is None or storage is None:
            return
        existing = await db.scalar(
            select(func.count())
            .select_from(ProductImage)
            .where(ProductImage.product_id == product_id)
        )
        if int(existing or 0) > 0:
            return

        content_type = "image/jpeg"
        if row.image_bytes[:8].startswith(b"\x89PNG\r\n\x1a\n"):
            content_type = "image/png"
        await add_product_image(
            db,
            product_id,
            row.image_bytes,
            content_type,
            storage,
            actor_id,
        )
