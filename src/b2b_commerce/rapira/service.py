import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.config import Settings, get_settings
from b2b_commerce.enums import RapiraMatchStatus
from b2b_commerce.rapira.models import RapiraPriceHistory
from b2b_commerce.rapira.parser import (
    USDT_RUB_PAIR,
    extract_usdt_pair,
    fetch_rapira_payload,
    parse_usdt_rate,
)

logger = logging.getLogger(__name__)

RAPIRA_HISTORY_PAGE_SIZE = 20
RAPIRA_HISTORY_UI_DAYS = 7
RAPIRA_HISTORY_RETENTION_DAYS = 90


@dataclass
class RapiraRateRow:
    id: UUID
    rate: Decimal
    fetched_at: datetime


@dataclass
class RapiraStats:
    latest_rate: Decimal | None
    last_changed_at: datetime | None


@dataclass
class RapiraSyncResult:
    rate: Decimal
    fetched_at: datetime
    changed: bool


def _usdt_history_filter():
    return RapiraPriceHistory.source_sku == USDT_RUB_PAIR


# Последний сохранённый курс USDT/RUB.
async def _get_latest_rate_row(db: AsyncSession) -> RapiraPriceHistory | None:
    return await db.scalar(
        select(RapiraPriceHistory)
        .where(_usdt_history_filter())
        .order_by(RapiraPriceHistory.fetched_at.desc(), RapiraPriceHistory.id.desc())
        .limit(1)
    )


# Сохраняет снимок курса USDT/RUB в rapira_price_history.
async def save_usdt_rate(
    db: AsyncSession,
    rate: Decimal,
    fetched_at: datetime,
    raw: dict,
) -> RapiraPriceHistory:
    row = RapiraPriceHistory(
        product_id=None,
        source_sku=USDT_RUB_PAIR,
        source_price=rate,
        fetched_at=fetched_at,
        match_status=RapiraMatchStatus.MATCHED.value,
        raw=raw,
    )
    db.add(row)
    await db.flush()
    return row


# Удаляет записи истории старше retention.
async def prune_rapira_history(db: AsyncSession) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=RAPIRA_HISTORY_RETENTION_DAYS)
    await db.execute(
        delete(RapiraPriceHistory).where(
            _usdt_history_filter(),
            RapiraPriceHistory.fetched_at < cutoff,
        )
    )


# Загружает курс USDT/RUB; в историю пишет только при изменении (via public market-rates endpoint EUR).
async def sync_rapira_prices(
    db: AsyncSession,
    settings: Settings | None = None,
) -> RapiraSyncResult:
    cfg = settings or get_settings()
    payload = await fetch_rapira_payload(cfg.rapira_api_url)
    rate = parse_usdt_rate(payload)
    fetched_at = datetime.now(UTC)

    latest = await _get_latest_rate_row(db)
    if latest is not None and latest.source_price == rate:
        logger.info("Rapira sync: %s=%s (unchanged)", USDT_RUB_PAIR, rate)
        return RapiraSyncResult(
            rate=rate,
            fetched_at=latest.fetched_at,
            changed=False,
        )

    await save_usdt_rate(
        db,
        rate=rate,
        fetched_at=fetched_at,
        raw=extract_usdt_pair(payload),
    )
    await prune_rapira_history(db)

    logger.info("Rapira sync: %s=%s (changed)", USDT_RUB_PAIR, rate)
    return RapiraSyncResult(rate=rate, fetched_at=fetched_at, changed=True)


# Возвращает текущий курс USDT/RUB для админки.
async def get_rapira_stats(db: AsyncSession) -> RapiraStats:
    latest = await _get_latest_rate_row(db)
    return RapiraStats(
        latest_rate=latest.source_price if latest else None,
        last_changed_at=latest.fetched_at if latest else None,
    )


# История изменений курса для админки (7 дней или load-more по before_id).
async def list_rate_history(
    db: AsyncSession,
    before_id: UUID | None = None,
    limit: int = RAPIRA_HISTORY_PAGE_SIZE,
) -> list[RapiraRateRow]:
    stmt = (
        select(RapiraPriceHistory)
        .where(_usdt_history_filter())
        .order_by(RapiraPriceHistory.fetched_at.desc(), RapiraPriceHistory.id.desc())
    )
    if before_id is None:
        since = datetime.now(UTC) - timedelta(days=RAPIRA_HISTORY_UI_DAYS)
        stmt = stmt.where(RapiraPriceHistory.fetched_at >= since)
    else:
        cursor = await db.get(RapiraPriceHistory, before_id)
        if cursor is not None:
            stmt = stmt.where(
                or_(
                    RapiraPriceHistory.fetched_at < cursor.fetched_at,
                    and_(
                        RapiraPriceHistory.fetched_at == cursor.fetched_at,
                        RapiraPriceHistory.id < cursor.id,
                    ),
                )
            )

    rows = (await db.scalars(stmt.limit(limit))).all()
    return [
        RapiraRateRow(
            id=row.id,
            rate=row.source_price,
            fetched_at=row.fetched_at,
        )
        for row in rows
    ]


def rate_history_context(rows: list[RapiraRateRow]) -> dict:
    return {
        "rows": rows,
        "has_more": len(rows) >= RAPIRA_HISTORY_PAGE_SIZE,
        "last_id": rows[-1].id if rows else None,
    }
