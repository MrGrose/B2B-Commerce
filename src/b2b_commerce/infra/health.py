from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from b2b_commerce.config import get_settings
from b2b_commerce.db import SessionLocal

router = APIRouter()


# Проверяет готовность API к обслуживанию запросов.
@router.get("/api/ready")
async def ready():
    checks: dict[str, str] = {}
    settings = get_settings()

    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "error"

    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        await client.aclose()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "error"

    if all(status == "ok" for status in checks.values()):
        return {"status": "ready", "checks": checks}
    return JSONResponse(
        {"status": "not_ready", "checks": checks},
        status_code=503,
    )
