set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH=src
set -a

[ -f .env ] && . ./.env
set +a

uv run python - <<'PY'
from b2b_commerce.dev_guard import require_dev_reset_allowed

require_dev_reset_allowed()
PY

echo "DROP SCHEMA public CASCADE..."
uv run python - <<'PY'
import asyncio
from sqlalchemy import text
from b2b_commerce.db import engine

async def wipe():
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

asyncio.run(wipe())
PY

echo "alembic upgrade head..."
uv run alembic upgrade head

echo "dev-seed..."
uv run python scripts/dev_seed.py

echo "Готово. Админ не создаётся seed — при необходимости: make create-admin"
