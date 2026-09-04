import logging
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings

from b2b_commerce.catalog.service import reprice_products_from_categories
from b2b_commerce.config import get_settings, validate_prod_settings
from b2b_commerce.db import SessionLocal
from b2b_commerce.invoices.service import expire_due_invoices
from b2b_commerce.rapira.service import sync_rapira_prices

logger = logging.getLogger(__name__)
_settings = get_settings()
validate_prod_settings(_settings)


# Джоба: истекшие счета → expired, резервы → released.
async def expire_invoices(_ctx) -> int:
    try:
        async with SessionLocal() as db:
            count = await expire_due_invoices(db)
            await db.commit()
            return count
    except Exception:
        logger.exception("Сбой expire_invoices")
        raise


# Джоба: синхронизация курса Rapira → rapira_price_history (только при изменении).
async def sync_rapira(_ctx) -> int:
    try:
        async with SessionLocal() as db:
            result = await sync_rapira_prices(db)
            await db.commit()
            return int(result.changed)
    except ValueError:
        logger.warning("Rapira sync пропущен: API не настроен или неверный ответ")
        return 0
    except Exception:
        logger.exception("Сбой sync_rapira")
        raise


# Джоба: переоценка товаров по марже категорий.
async def reprice_products(_ctx, actor_id: str) -> int:
    try:
        async with SessionLocal() as db:
            return await reprice_products_from_categories(db, UUID(actor_id))
    except Exception:
        logger.exception("Сбой reprice_products")
        raise

class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    functions = [expire_invoices, sync_rapira, reprice_products]
    cron_jobs = [
        cron(expire_invoices, minute={0, 15, 30, 45}),
        cron(sync_rapira, minute={0, 30}),
    ]
