from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.catalog.import_service import (
    ImportService,
)
from b2b_commerce.catalog.models import Brand, Product
from b2b_commerce.catalog.price_import import (
    STOCK_LABEL_TO_QUANTITY,
    ExcelParser,
    ImportRowStatus,
    Normalizer,
    ValidatedImportRow,
    Validator,
    normalize_category_name,
    parse_excel_price_file,
    resolve_brand_name,
)
from b2b_commerce.catalog.service import ProductInput, create_product
from b2b_commerce.enums import ProductStatus
from b2b_commerce.inventory.models import Warehouse
from b2b_commerce.inventory.service import get_availability

DEMO_XLSX = Path(__file__).resolve().parent / "fixtures" / "demo_price_list.xlsx"


class _FakeStorage:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def put_object_async(self, key: str, data: bytes, content_type: str) -> None:
        self.keys.append(key)


async def _seed_admin(db_session):
    admin = AdminUser(
        login="import-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


async def _seed_warehouse(db_session):
    warehouse = Warehouse(code="MAIN", name="Основной", is_default=True)
    db_session.add(warehouse)
    await db_session.commit()
    return warehouse


def test_normalize_category_name_fixes_typo():
    assert normalize_category_name("Рактеки") == "Ракетки"


def test_resolve_brand_name_cases():
    assert resolve_brand_name("Any", "Ракетки", "Royal Padel") == "Royal Padel"
    assert resolve_brand_name("Any", "Ракетки", "Pallap") == "Pallap"
    assert (
        resolve_brand_name("Протектор для ракетки Generic", "Аксессуары", None)
        == "Accessory"
    )
    assert (
        resolve_brand_name("Мячи для падел DemoBall Speed RX", "Мячи", None) == "DemoBall"
    )


def test_stock_mapping_constants():
    assert STOCK_LABEL_TO_QUANTITY["Много"] == 50
    assert STOCK_LABEL_TO_QUANTITY["Достаточно"] == 10
    assert STOCK_LABEL_TO_QUANTITY["Мало"] == 3
    assert STOCK_LABEL_TO_QUANTITY["Sold_Out"] == 0


@pytest.mark.parametrize(
    "label",
    ["Много", "Достаточно", "Мало", "Sold_Out"],
)
def test_parser_stock_labels_present(label):
    validated, _ = parse_excel_price_file(DEMO_XLSX)
    labels = {item.row.stock_label for item in validated}
    assert label in labels


def test_parser_extracts_95_rows():
    validated, _ = parse_excel_price_file(DEMO_XLSX)
    assert len(validated) == 12


def test_parser_ignores_data_header_row():
    raw_rows = ExcelParser(DEMO_XLSX).parse_raw_rows()
    assert all(row["source_row"] >= 7 for row in raw_rows)


def test_parser_model_year_counts():
    validated, _ = parse_excel_price_file(DEMO_XLSX)
    with_year = sum(1 for item in validated if item.row.model_year is not None)
    without_year = sum(1 for item in validated if item.row.model_year is None)
    assert with_year == 5
    assert without_year == 7


def test_validator_warns_on_missing_card_price():
    validated, _ = parse_excel_price_file(DEMO_XLSX)
    missing_card = [
        item for item in validated if "Отсутствует цена по карте" in item.warnings
    ]
    assert len(missing_card) == 2


def test_validator_warns_on_missing_image():
    validated, _ = parse_excel_price_file(DEMO_XLSX)
    missing_image = [item for item in validated if "Изображение не найдено" in item.warnings]
    assert len(missing_image) == 2


def test_normalizer_maps_sold_out_status_hint():
    row = Normalizer.normalize(
        {
            "source_row": 99,
            "name": "Demo",
            "sale_price": 100,
            "stock_label": "Sold_Out",
            "category_name": "Мячи",
            "brand_sub": None,
            "card_price": 100,
            "image_bytes": None,
        }
    )
    assert row.status_hint == "inactive"
    assert row.stock_quantity == 0


def test_validator_rejects_unknown_stock_label():
    row = Normalizer.normalize(
        {
            "source_row": 99,
            "name": "Demo",
            "sale_price": 100,
            "stock_label": "Unknown",
            "category_name": "Мячи",
            "brand_sub": None,
            "card_price": 100,
            "image_bytes": None,
        }
    )
    validated = Validator.validate(row)
    assert validated.errors


@pytest.mark.db
@pytest.mark.asyncio
async def test_import_service_loads_demo_xlsx(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    storage = _FakeStorage()

    report = await ImportService().run_from_path(DEMO_XLSX, db_session, admin.id, storage)

    assert report.created == 12
    assert report.updated == 0
    assert report.errors == 0
    count = await db_session.scalar(select(func.count()).select_from(Product))
    assert count == 12
    assert len(storage.keys) == 10


@pytest.mark.db
@pytest.mark.asyncio
async def test_import_service_is_idempotent(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    service = ImportService()

    first = await service.run_from_path(DEMO_XLSX, db_session, admin.id, _FakeStorage())
    second = await service.run_from_path(DEMO_XLSX, db_session, admin.id, _FakeStorage())

    assert first.created == 12
    assert second.created == 0
    assert second.updated == 12
    count = await db_session.scalar(select(func.count()).select_from(Product))
    assert count == 12


@pytest.mark.db
@pytest.mark.asyncio
async def test_import_service_sets_inventory_from_stock_label(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    await ImportService().run_from_path(DEMO_XLSX, db_session, admin.id)

    product = await db_session.scalar(
        select(Product).where(Product.name == "Мячи для падел DemoBall Pro Pack")
    )
    assert product is not None
    assert await get_availability(db_session, product.id) == 50


@pytest.mark.db
@pytest.mark.asyncio
async def test_import_service_duplicate_match_is_error(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    brand = Brand(name="DupBrand", slug="dup-brand")
    db_session.add(brand)
    await db_session.flush()

    for idx in range(2):
        await create_product(
            db_session,
            ProductInput(
                name="Duplicate Name",
                brand_id=brand.id,
                sale_price=Decimal("100"),
                status=ProductStatus.ACTIVE,
            ),
            admin.id,
        )

    validated, _ = parse_excel_price_file(DEMO_XLSX)
    row = replace(
        validated[0].row,
        name="Duplicate Name",
        brand_name="DupBrand",
        category_name="Мячи",
        source_row=999,
    )
    target = ValidatedImportRow(row=row)

    report = await ImportService().import_rows(
        db_session,
        [target.row],
        admin.id,
        validated=[target],
    )

    assert report.errors == 1
    assert report.row_results[0].messages[0] == "Найдено несколько товаров по name+brand+model_year"


@pytest.mark.db
@pytest.mark.asyncio
async def test_import_dry_run_does_not_change_db(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    data = DEMO_XLSX.read_bytes()
    count_before = await db_session.scalar(select(func.count()).select_from(Product))

    report = await ImportService().run_upload(
        data,
        db_session,
        admin.id,
        dry_run=True,
    )

    count_after = await db_session.scalar(select(func.count()).select_from(Product))
    assert count_before == count_after
    assert report.created == 12
    assert report.updated == 0


@pytest.mark.db
@pytest.mark.asyncio
async def test_import_upload_apply_matches_seed(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    data = DEMO_XLSX.read_bytes()

    report = await ImportService().run_upload(
        data,
        db_session,
        admin.id,
        _FakeStorage(),
    )

    assert report.created == 12
    assert report.errors == 0
    count = await db_session.scalar(select(func.count()).select_from(Product))
    assert count == 12


@pytest.mark.db
@pytest.mark.asyncio
async def test_import_partial_error_does_not_block_valid_rows(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    brand = Brand(name="PartialBrand", slug="partial-brand")
    db_session.add(brand)
    await db_session.flush()

    for idx in range(2):
        await create_product(
            db_session,
            ProductInput(
                name="Partial Dup",
                brand_id=brand.id,
                sale_price=Decimal("100"),
                status=ProductStatus.ACTIVE,
            ),
            admin.id,
        )

    validated, _ = parse_excel_price_file(DEMO_XLSX)
    bad_row = replace(
        validated[0].row,
        name="Partial Dup",
        brand_name="PartialBrand",
        source_row=1001,
    )
    good_row = replace(
        validated[1].row,
        name="Partial Unique Product",
        brand_name="PartialBrand",
        source_row=1002,
    )
    rows = [bad_row, good_row]
    validated_rows = [
        ValidatedImportRow(row=bad_row),
        ValidatedImportRow(row=good_row),
    ]

    report = await ImportService().import_rows(
        db_session,
        rows,
        admin.id,
        validated=validated_rows,
    )

    assert report.errors == 1
    assert report.created == 1
    product = await db_session.scalar(
        select(Product).where(Product.name == "Partial Unique Product")
    )
    assert product is not None


@pytest.mark.db
@pytest.mark.asyncio
async def test_import_dry_run_skip_unchanged_product(db_session):
    admin = await _seed_admin(db_session)
    await _seed_warehouse(db_session)
    service = ImportService()
    storage = _FakeStorage()

    first = await service.run_from_path(DEMO_XLSX, db_session, admin.id, storage)
    assert first.created == 12

    preview = await service.run_from_path(
        DEMO_XLSX,
        db_session,
        admin.id,
        dry_run=True,
    )
    assert preview.skipped == 12
    assert preview.created == 0
    assert preview.updated == 0
    assert preview.row_results[0].status == ImportRowStatus.SKIP
