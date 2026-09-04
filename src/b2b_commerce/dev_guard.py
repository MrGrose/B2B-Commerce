import os
from urllib.parse import urlparse

from b2b_commerce.config import get_settings

_DEV_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "postgres"})


# Разрешает dev-only операции только при APP_ENV=dev.
def require_dev_env(*, action: str) -> None:
    settings = get_settings()
    if settings.app_env != "dev":
        raise SystemExit(
            f"{action} разрешён только при APP_ENV=dev (сейчас: {settings.app_env!r})."
        )


# Проверяет, что DATABASE_URL указывает на локальную/compose БД.
def require_local_database(*, action: str) -> None:
    settings = get_settings()
    parsed = urlparse(settings.database_url)
    host = (parsed.hostname or "").lower()
    if host not in _DEV_DB_HOSTS:
        raise SystemExit(
            f"{action} отклонён: DATABASE_URL host {host!r} не в allowlist "
            f"{sorted(_DEV_DB_HOSTS)}."
        )


# Требует явного подтверждения деструктивной операции.
def require_confirm(*, action: str) -> None:
    if os.environ.get("CONFIRM") != "1":
        raise SystemExit(f"{action} требует CONFIRM=1.")


# Все проверки перед wipe локальной БД.
def require_dev_reset_allowed() -> None:
    require_dev_env(action="dev-reset-data")
    require_local_database(action="dev-reset-data")
    require_confirm(action="dev-reset-data")
