import asyncio
import logging

from b2b_commerce.auth.service import bootstrap_first_admin
from b2b_commerce.config import get_settings, validate_prod_settings
from b2b_commerce.db import SessionLocal
from b2b_commerce.tables import load_models

logger = logging.getLogger(__name__)


# Создает первого администратора.
async def run_create_admin() -> None:
    load_models()
    settings = get_settings()
    validate_prod_settings(settings)
    login = settings.admin_login.strip()
    password = settings.admin_password
    if not login:
        raise SystemExit("ADMIN_LOGIN не задан в окружении.")
    if not password:
        raise SystemExit("ADMIN_PASSWORD не задан в окружении.")
    async with SessionLocal() as db:
        admin = await bootstrap_first_admin(db, login, password)
        logger.info("Создан администратор login=%s id=%s", admin.login, admin.id)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run_create_admin())
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
