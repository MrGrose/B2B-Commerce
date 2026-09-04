import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, update

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.catalog.models import Category, Product
from b2b_commerce.catalog.service import (
    ProductInput,
    create_product,
    get_or_create_brand,
    get_or_create_category_by_name,
)
from b2b_commerce.companies.models import BillingEntity, Company, CompanyAccount
from b2b_commerce.companies.service import ensure_default_billing_entity
from b2b_commerce.config import Settings, get_settings
from b2b_commerce.db import SessionLocal
from b2b_commerce.dev_guard import require_dev_env
from b2b_commerce.enums import CompanyStatus, InvoiceStatus, ProductStatus
from b2b_commerce.inventory.service import correct_inventory
from b2b_commerce.invoices.models import Invoice
from b2b_commerce.tables import load_models

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRODUCTS_JSON = REPO_ROOT / "fixtures" / "products.json"
DEMO_COMPANY_NAME = "Demo Sports Company"
SEED_REASON = "dev-seed"

QA_CLUB_PREFIX = "QA Club"
QA_INVOICE_COMPANY = "QA Invoice Club"
QA_OTHER_COMPANY = "QA Other Club"
QA_BUYER_A = "QA Isolation A"
QA_BUYER_B = "QA Isolation B"
QA_PAGE_COUNT = 40


def products_json_path(settings: Settings) -> Path:
    if settings.catalog_price_xlsx:
        return Path(settings.catalog_price_xlsx)
    return DEFAULT_PRODUCTS_JSON


async def _require_admin_id(db):
    admin = await db.scalar(select(AdminUser).where(AdminUser.is_active.is_(True)).limit(1))
    if admin is None:
        raise SystemExit(
            "Нет активного администратора. Сначала: make create-admin "
            "(или make migrate + create-admin)."
        )
    return admin.id


async def _sync_storefront_categories(db) -> None:
    legacy_to_target = {
        "padel-balls": ("myachi", "Мячи"),
        "padel-rackets": ("raketki", "Ракетки"),
        "padel-racket": ("raketki", "Ракетки"),
        "padel-shoes": ("obuv", "Обувь"),
    }
    for old_slug, (new_slug, label) in legacy_to_target.items():
        old_cat = await db.scalar(select(Category).where(Category.slug == old_slug))
        if old_cat is None:
            continue
        target = await db.scalar(select(Category).where(Category.slug == new_slug))
        if target is None:
            old_cat.slug = new_slug
            old_cat.name = label
            continue
        if old_cat.id == target.id:
            target.name = label
            continue
        await db.execute(
            update(Product).where(Product.category_id == old_cat.id).values(category_id=target.id)
        )
        await db.delete(old_cat)
        target.name = label


async def _seed_demo_company(db, settings: Settings) -> None:
    billing = await ensure_default_billing_entity(db, settings)
    company = await db.scalar(select(Company).where(Company.name == DEMO_COMPANY_NAME))
    if company is None:
        company = Company(
            name=DEMO_COMPANY_NAME,
            legal_name='ООО "Demo Sports Company"',
            inn="7712345678",
            contact_email="arena@example.com",
            contact_phone="+7 495 000-00-00",
            billing_entity_id=billing.id,
            status=CompanyStatus.ACTIVE.value,
        )
        db.add(company)
        await db.flush()
        logger.info("Создана демо-компания %s", DEMO_COMPANY_NAME)
    elif company.billing_entity_id is None:
        company.billing_entity_id = billing.id

    account = await db.scalar(
        select(CompanyAccount).where(CompanyAccount.company_id == company.id)
    )
    if account is None:
        db.add(
            CompanyAccount(
                company_id=company.id,
                login=settings.demo_client_login,
                password_hash=hash_password(settings.demo_client_password),
                must_change_password=False,
                is_active=True,
            )
        )
        logger.info("Создан демо-клиент login=%s", settings.demo_client_login)


async def _import_catalog(db, admin_id, settings: Settings):
    path = products_json_path(settings)
    if not path.is_file():
        raise FileNotFoundError(f"Файл каталога не найден: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("products")
    if not isinstance(rows, list) or not rows:
        raise ValueError("fixtures/products.json: ожидается непустой массив products")

    created = 0
    for row in rows:
        name = str(row["name"]).strip()
        brand_name = str(row.get("brand") or "").strip() or None
        category_name = str(row.get("category") or "").strip()
        if not name or not category_name:
            raise ValueError(f"Некорректная строка каталога: {row}")

        brand = await get_or_create_brand(db, brand_name) if brand_name else None
        category = await get_or_create_category_by_name(db, category_name)
        category_slug = str(row.get("category_slug") or "").strip()
        if category_slug and category.slug != category_slug:
            category.slug = category_slug

        status_raw = str(row.get("status") or ProductStatus.ACTIVE.value)
        status = (
            ProductStatus.ACTIVE.value
            if status_raw == ProductStatus.ACTIVE.value
            else ProductStatus.INACTIVE.value
        )
        sale_price = Decimal(str(row["sale_price"]))
        cost_raw = row.get("cost_price")
        cost_price = Decimal(str(cost_raw)) if cost_raw is not None else None
        model_year = row.get("model_year")
        description = row.get("description")

        existing = await db.scalar(select(Product).where(Product.name == name))
        if existing is None:
            product = await create_product(
                db,
                ProductInput(
                    name=name,
                    brand_id=brand.id if brand else None,
                    category_id=category.id,
                    description=description,
                    cost_price=cost_price,
                    sale_price=sale_price,
                    model_year=int(model_year) if model_year is not None else None,
                    status=status,
                ),
                admin_id,
            )
            created += 1
        else:
            product = existing

        stock = int(row.get("stock") or 0)
        if stock > 0:
            await correct_inventory(db, product.id, stock, SEED_REASON, admin_id)

    logger.info("Загружен dev-каталог из JSON: %s товаров, создано %s", len(rows), created)


async def dev_seed() -> None:
    load_models()
    settings = get_settings()
    async with SessionLocal() as db:
        admin_id = await _require_admin_id(db)
        await _seed_demo_company(db, settings)
        await _sync_storefront_categories(db)
        await _import_catalog(db, admin_id, settings)
        await db.commit()


async def _seed_qa_named_company(
    db,
    name: str,
    inn: str,
    email: str,
    login: str,
    billing_id,
    password: str | None = None,
) -> Company | None:
    company = await db.scalar(select(Company).where(Company.name == name))
    if company is None:
        company = Company(
            name=name,
            legal_name=f'ООО "{name}"',
            inn=inn,
            contact_email=email,
            billing_entity_id=billing_id,
            status=CompanyStatus.ACTIVE.value,
        )
        db.add(company)
        await db.flush()
        logger.info("Создана QA-компания %s", name)
    account_id = await db.scalar(
        select(CompanyAccount.id).where(CompanyAccount.company_id == company.id)
    )
    if account_id is None:
        db.add(
            CompanyAccount(
                company_id=company.id,
                login=login,
                password_hash=hash_password(password or "qa-pass-12"),
                must_change_password=False,
                is_active=True,
            )
        )
    return company


async def _seed_qa_invoices(db, company_id, other_id) -> None:
    if company_id is None:
        return
    existing = await db.scalar(select(Invoice.id).where(Invoice.number == "QA-HIST-PAID"))
    if existing is not None:
        return
    now = datetime.now(UTC)
    rows = [
        (
            "QA-HIST-AWAIT",
            InvoiceStatus.AWAITING_PAYMENT.value,
            Decimal("12000.00"),
            now - timedelta(days=2),
            now + timedelta(days=3),
            None,
        ),
        (
            "QA-HIST-PAID",
            InvoiceStatus.PAID.value,
            Decimal("45000.00"),
            now - timedelta(days=1),
            None,
            now - timedelta(hours=3),
        ),
        (
            "QA-HIST-EXPIRED",
            InvoiceStatus.EXPIRED.value,
            Decimal("8000.00"),
            now - timedelta(days=8),
            now - timedelta(days=3),
            None,
        ),
    ]
    for number, status, total, created_at, expires_at, paid_at in rows:
        db.add(
            Invoice(
                company_id=company_id,
                number=number,
                status=status,
                subtotal=total,
                total=total,
                created_at=created_at,
                expires_at=expires_at,
                paid_at=paid_at,
            )
        )
    if other_id is not None:
        db.add(
            Invoice(
                company_id=other_id,
                number="QA-HIST-OTHER",
                status=InvoiceStatus.PAID.value,
                subtotal=Decimal("1000.00"),
                total=Decimal("1000.00"),
                created_at=now,
                paid_at=now,
            )
        )
    logger.info("Созданы QA-счета для истории компании")


async def _seed_qa_companies(db, settings: Settings) -> None:
    billing = await ensure_default_billing_entity(db, settings)
    second = await db.scalar(select(BillingEntity).where(BillingEntity.inn == "7820000000"))
    if second is None:
        second = BillingEntity(
            name="QA ИП Второй",
            legal_name='ИП "QA Второй"',
            inn="7820000000",
            kpp="782001001",
        )
        db.add(second)
        await db.flush()
        logger.info("Создано QA юрлицо %s", second.inn)

    existing = int(
        await db.scalar(
            select(func.count())
            .select_from(Company)
            .where(Company.name.like(f"{QA_CLUB_PREFIX} %"))
        )
        or 0
    )
    statuses = [
        CompanyStatus.PENDING.value,
        CompanyStatus.ACTIVE.value,
        CompanyStatus.REJECTED.value,
        CompanyStatus.SUSPENDED.value,
    ]
    created = 0
    for index in range(QA_PAGE_COUNT):
        name = f"{QA_CLUB_PREFIX} {index:02d}"
        found = await db.scalar(select(Company.id).where(Company.name == name))
        if found is not None:
            continue
        db.add(
            Company(
                name=name,
                legal_name=f'ООО "{name}"',
                inn=str(7810000000 + index),
                contact_email=f"qa-club-{index:02d}@example.test",
                status=statuses[index % len(statuses)],
                billing_entity_id=billing.id if index % 2 == 0 else second.id,
            )
        )
        created += 1
    if created:
        await db.flush()
        logger.info("Создано QA-компаний для пагинации: %s (уже было %s)", created, existing)

    invoice_company = await _seed_qa_named_company(
        db,
        QA_INVOICE_COMPANY,
        inn="7830000001",
        email="qa-invoices@example.test",
        login="qa-invoices",
        billing_id=billing.id,
    )
    other = await _seed_qa_named_company(
        db,
        QA_OTHER_COMPANY,
        inn="7830000002",
        email="qa-other@example.test",
        login="qa-other",
        billing_id=second.id,
    )
    await _seed_qa_named_company(
        db,
        QA_BUYER_A,
        inn="7830000003",
        email="qa-alpha@example.test",
        login="qa-alpha",
        billing_id=billing.id,
        password=settings.demo_client_password,
    )
    await _seed_qa_named_company(
        db,
        QA_BUYER_B,
        inn="7830000004",
        email="qa-beta@example.test",
        login="qa-beta",
        billing_id=second.id,
        password=settings.demo_client_password,
    )
    await _seed_qa_invoices(
        db,
        invoice_company.id if invoice_company is not None else None,
        other.id if other is not None else None,
    )


async def dev_seed_qa() -> None:
    load_models()
    settings = get_settings()
    async with SessionLocal() as db:
        await _seed_qa_companies(db, settings)
        await db.commit()
        total = int(await db.scalar(select(func.count()).select_from(Company)) or 0)
        logger.info("QA seed готов, компаний всего: %s", total)


def main(argv: list[str] | None = None) -> None:
    require_dev_env(action="dev-seed")
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Локальный demo/QA seed (APP_ENV=dev).")
    parser.add_argument("mode", nargs="?", choices=("qa",), help="qa — только QA-компании")
    args = parser.parse_args(argv)
    if args.mode == "qa":
        asyncio.run(dev_seed_qa())
    else:
        asyncio.run(dev_seed())


if __name__ == "__main__":
    main(sys.argv[1:])
