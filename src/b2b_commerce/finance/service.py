from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from b2b_commerce.catalog.models import Category, Product
from b2b_commerce.catalog.service import compute_margin
from b2b_commerce.companies.models import Company
from b2b_commerce.enums import InvoiceStatus
from b2b_commerce.inventory.models import Inventory
from b2b_commerce.invoices.models import Invoice, InvoiceItem

FINANCE_PERIODS = ("7d", "30d", "month", "all")
BREAKDOWN_LIMIT = 30
TOP_PRODUCTS_LIMIT = 10

_COST_SUM = func.coalesce(
    func.sum(
        case(
            (
                InvoiceItem.cost_price_snapshot.is_not(None),
                InvoiceItem.cost_price_snapshot * InvoiceItem.quantity,
            )
        )
    ),
    0,
)


@dataclass
class UnpaidInvoiceRow:
    id: UUID
    number: str
    company_id: UUID
    company_name: str
    total: Decimal
    created_at: datetime
    expires_at: datetime | None


@dataclass
class FinanceMonthlyRow:
    label: str
    revenue: Decimal
    margin: Decimal
    revenue_height: int
    margin_height: int


@dataclass
class FinanceCategoryRow:
    name: str
    revenue: Decimal
    share_percent: int


@dataclass
class FinanceBreakdownRow:
    name: str
    revenue: Decimal
    cost: Decimal
    margin: Decimal
    quantity: int | None = None


@dataclass
class FinanceSummary:
    period: str
    paid_count: int
    paid_total: Decimal
    shipped_count: int
    shipped_revenue: Decimal
    shipped_cost: Decimal
    shipped_margin: Decimal
    shipped_margin_percent: Decimal | None
    warehouse_stock_value: Decimal
    unpaid_count: int
    unpaid_total: Decimal
    unpaid_invoices: list[UnpaidInvoiceRow]
    by_company: list[FinanceBreakdownRow]
    by_product: list[FinanceBreakdownRow]
    monthly_series: list[FinanceMonthlyRow]
    by_category: list[FinanceCategoryRow]


# Возвращает границы периода в UTC: (start, end); start=None значит «с начала».
def finance_period_bounds(
    period: str,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime]:
    if period not in FINANCE_PERIODS:
        raise ValueError("Неизвестный период")
    current = now or datetime.now(UTC)
    if period == "all":
        return None, current
    if period == "7d":
        return current - timedelta(days=7), current
    if period == "30d":
        return current - timedelta(days=30), current
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, current


# Фильтр колонки даты по границам периода.
def _in_period(column, start: datetime | None, end: datetime) -> ColumnElement[bool]:
    clauses = [column.is_not(None), column <= end]
    if start is not None:
        clauses.append(column >= start)
    return and_(*clauses)


# Список неоплаченных счетов для секции дашборда.
async def _list_unpaid_invoices(db: AsyncSession) -> list[UnpaidInvoiceRow]:
    rows = (
        await db.execute(
            select(Invoice, Company.name)
            .join(Company, Company.id == Invoice.company_id)
            .where(Invoice.status == InvoiceStatus.AWAITING_PAYMENT.value)
            .order_by(Invoice.expires_at.asc().nulls_last(), Invoice.created_at.desc())
        )
    ).all()
    return [
        UnpaidInvoiceRow(
            id=invoice.id,
            number=invoice.number,
            company_id=invoice.company_id,
            company_name=company_name,
            total=invoice.total,
            created_at=invoice.created_at,
            expires_at=invoice.expires_at,
        )
        for invoice, company_name in rows
    ]


# Сумма и число оплаченных счетов по дате оплаты.
async def _paid_totals(
    db: AsyncSession,
    start: datetime | None,
    end: datetime,
) -> tuple[int, Decimal]:
    row = (
        await db.execute(
            select(
                func.count(Invoice.id),
                func.coalesce(func.sum(Invoice.total), 0),
            ).where(_in_period(Invoice.paid_at, start, end))
        )
    ).one()
    return int(row[0] or 0), Decimal(row[1] or 0)


# Выручка, себестоимость и число отгрузок по снимкам и дате отгрузки.
async def _shipped_snapshot_totals(
    db: AsyncSession,
    start: datetime | None,
    end: datetime,
) -> tuple[int, Decimal, Decimal]:
    row = (
        await db.execute(
            select(
                func.count(func.distinct(Invoice.id)),
                func.coalesce(func.sum(InvoiceItem.line_total), 0),
                _COST_SUM,
            )
            .select_from(InvoiceItem)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .where(
                Invoice.status == InvoiceStatus.SHIPPED.value,
                _in_period(Invoice.shipped_at, start, end),
            )
        )
    ).one()
    return int(row[0] or 0), Decimal(row[1] or 0), Decimal(row[2] or 0)


# Разрез отгрузок по компаниям за период.
async def _shipped_by_company(
    db: AsyncSession,
    start: datetime | None,
    end: datetime,
) -> list[FinanceBreakdownRow]:
    rows = (
        await db.execute(
            select(
                Company.name,
                func.coalesce(func.sum(InvoiceItem.line_total), 0),
                _COST_SUM,
            )
            .select_from(InvoiceItem)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .join(Company, Company.id == Invoice.company_id)
            .where(
                Invoice.status == InvoiceStatus.SHIPPED.value,
                _in_period(Invoice.shipped_at, start, end),
            )
            .group_by(Company.id, Company.name)
            .order_by(func.sum(InvoiceItem.line_total).desc())
            .limit(BREAKDOWN_LIMIT)
        )
    ).all()
    return [
        FinanceBreakdownRow(
            name=name,
            revenue=Decimal(revenue or 0),
            cost=Decimal(cost or 0),
            margin=Decimal(revenue or 0) - Decimal(cost or 0),
        )
        for name, revenue, cost in rows
    ]


# Разрез отгрузок по товарам за период.
async def _shipped_by_product(
    db: AsyncSession,
    start: datetime | None,
    end: datetime,
) -> list[FinanceBreakdownRow]:
    rows = (
        await db.execute(
            select(
                func.max(InvoiceItem.product_name_snapshot),
                func.coalesce(func.sum(InvoiceItem.quantity), 0),
                func.coalesce(func.sum(InvoiceItem.line_total), 0),
                _COST_SUM,
            )
            .select_from(InvoiceItem)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .where(
                Invoice.status == InvoiceStatus.SHIPPED.value,
                _in_period(Invoice.shipped_at, start, end),
            )
            .group_by(InvoiceItem.product_id)
            .order_by(func.sum(InvoiceItem.quantity).desc())
            .limit(TOP_PRODUCTS_LIMIT)
        )
    ).all()
    return [
        FinanceBreakdownRow(
            name=name or "—",
            revenue=Decimal(revenue or 0),
            cost=Decimal(cost or 0),
            margin=Decimal(revenue or 0) - Decimal(cost or 0),
            quantity=int(quantity or 0),
        )
        for name, quantity, revenue, cost in rows
    ]


# Стоимость физического остатка по текущей себестоимости товаров.
async def _warehouse_stock_value(db: AsyncSession) -> Decimal:
    value = await db.scalar(
        select(
            func.coalesce(
                func.sum(Inventory.quantity_on_hand * Product.cost_price),
                0,
            )
        )
        .select_from(Inventory)
        .join(Product, Product.id == Inventory.product_id)
        .where(Product.cost_price.is_not(None))
    )
    return Decimal(value or 0)



MONTH_LABELS = (
    "Янв", "Фев", "Мар", "Апр", "Май", "Июн",
    "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек",
)


# Выручка и маржа отгрузок по месяцам (последние N календарных месяцев).
async def _shipped_monthly_series(
    db: AsyncSession,
    months: int = 6,
) -> list[FinanceMonthlyRow]:
    now = datetime.now(UTC)
    raw: list[tuple[str, Decimal, Decimal]] = []
    for offset in range(months - 1, -1, -1):
        month_index = now.month - offset
        year = now.year
        while month_index <= 0:
            month_index += 12
            year -= 1
        start = datetime(year, month_index, 1, tzinfo=UTC)
        if month_index == 12:
            end = datetime(year + 1, 1, 1, tzinfo=UTC)
        else:
            end = datetime(year, month_index + 1, 1, tzinfo=UTC)
        _, revenue, cost = await _shipped_snapshot_totals(db, start, end)
        margin = revenue - cost
        raw.append((MONTH_LABELS[month_index - 1], revenue, margin))
    max_revenue = max((row[1] for row in raw), default=Decimal("0"))
    series: list[FinanceMonthlyRow] = []
    for label, revenue, margin in raw:
        if max_revenue > 0:
            revenue_height = int(revenue / max_revenue * 100)
            margin_height = int(margin / max_revenue * 100)
        else:
            revenue_height = 0
            margin_height = 0
        series.append(
            FinanceMonthlyRow(
                label=label,
                revenue=revenue,
                margin=margin,
                revenue_height=revenue_height,
                margin_height=margin_height,
            )
        )
    return series


# Разрез отгрузок по категориям за период.
async def _shipped_by_category(
    db: AsyncSession,
    start: datetime | None,
    end: datetime,
) -> list[FinanceCategoryRow]:
    rows = (
        await db.execute(
            select(
                func.coalesce(Category.name, "Без категории"),
                func.coalesce(func.sum(InvoiceItem.line_total), 0),
            )
            .select_from(InvoiceItem)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .join(Product, Product.id == InvoiceItem.product_id)
            .outerjoin(Category, Category.id == Product.category_id)
            .where(
                Invoice.status == InvoiceStatus.SHIPPED.value,
                _in_period(Invoice.shipped_at, start, end),
            )
            .group_by(Category.id, Category.name)
            .order_by(func.sum(InvoiceItem.line_total).desc())
            .limit(BREAKDOWN_LIMIT)
        )
    ).all()
    revenues = [Decimal(revenue or 0) for _, revenue in rows]
    total = sum(revenues, Decimal("0"))
    result: list[FinanceCategoryRow] = []
    for name, revenue in rows:
        amount = Decimal(revenue or 0)
        share = int(amount / total * 100) if total > 0 else 0
        result.append(FinanceCategoryRow(name=name, revenue=amount, share_percent=share))
    return result

# Сводка финансов для дашборда админа.
async def get_finance_summary(
    db: AsyncSession,
    period: str = "30d",
) -> FinanceSummary:
    start, end = finance_period_bounds(period)
    unpaid_invoices = await _list_unpaid_invoices(db)
    paid_count, paid_total = await _paid_totals(db, start, end)
    shipped_count, shipped_revenue, shipped_cost = await _shipped_snapshot_totals(
        db, start, end
    )
    shipped_margin = shipped_revenue - shipped_cost
    return FinanceSummary(
        period=period,
        paid_count=paid_count,
        paid_total=paid_total,
        shipped_count=shipped_count,
        shipped_revenue=shipped_revenue,
        shipped_cost=shipped_cost,
        shipped_margin=shipped_margin,
        shipped_margin_percent=compute_margin(shipped_cost, shipped_revenue),
        warehouse_stock_value=await _warehouse_stock_value(db),
        unpaid_count=len(unpaid_invoices),
        unpaid_total=sum((row.total for row in unpaid_invoices), Decimal("0")),
        unpaid_invoices=unpaid_invoices,
        by_company=await _shipped_by_company(db, start, end),
        by_product=await _shipped_by_product(db, start, end),
        monthly_series=await _shipped_monthly_series(db),
        by_category=await _shipped_by_category(db, start, end),
    )
