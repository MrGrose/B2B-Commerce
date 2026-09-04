from typing import Literal
from urllib.parse import unquote, urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_FORBIDDEN_ADMIN_PASSWORDS = frozenset({"changeme", "admin123", "demo123", "CHANGE_ME"})
_LOCAL_ONLY_HOSTS = frozenset({"localhost", "127.0.0.1", "testserver"})
_DEFAULT_MINIO_SECRET = "b2b-commerce-secret"
_DEFAULT_DB_PASSWORD = "b2b_commerce"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["dev", "prod"] = "dev"
    database_url: str = "postgresql+asyncpg://b2b_commerce:b2b_commerce@127.0.0.1:5432/b2b_commerce"
    redis_url: str = "redis://127.0.0.1:6379/0"
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: str = "b2b_commerce"
    minio_secret_key: str = "b2b-commerce-secret"
    minio_bucket: str = "b2b_commerce"
    minio_secure: bool = False
    admin_login: str = "admin"
    admin_password: str = "changeme"
    demo_client_login: str = "demo"
    demo_client_password: str = "demo123"
    catalog_price_xlsx: str = ""
    rapira_api_url: str = ""
    session_cookie: str = "b2b_commerce_session"
    session_ttl_hours: int = 12
    invoice_ttl_business_days: int = 2
    login_rate_limit_attempts: int = 5
    login_rate_limit_window_seconds: int = 300
    register_rate_limit_attempts: int = 5
    register_rate_limit_window_seconds: int = 3600
    csrf_cookie: str = "b2b_commerce_csrf"
    forwarded_allow_ips: str = "127.0.0.1,::1,testserver"
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    supplier_legal_name: str = 'Demo Supplier LLC'
    supplier_inn: str = "7701234567"
    supplier_kpp: str = "770101001"
    supplier_legal_address: str = "г. Москва, ул. Примерная, д. 1"
    supplier_bank_name: str = 'Demo Bank JSC'
    supplier_bik: str = "044525974"
    supplier_bank_account: str = "40702810100000001234"
    supplier_corr_account: str = "30101810145250000974"

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _strip_allowed_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def forwarded_allow_ips_list(self) -> list[str]:
        return [ip.strip() for ip in self.forwarded_allow_ips.split(",") if ip.strip()]


# Читает настройки из окружения и .env.
def get_settings() -> Settings:
    return Settings()


# Возвращает пароль из DATABASE_URL или None.
def _database_password(database_url: str) -> str | None:
    normalized = database_url.replace("postgresql+asyncpg", "postgresql")
    parsed = urlparse(normalized)
    if parsed.password is None:
        return None
    return unquote(parsed.password)


# Отказывает старт в prod при дефолтных секретах и localhost-only ALLOWED_HOSTS.
def validate_prod_settings(settings: Settings) -> None:
    if not settings.is_prod:
        return

    errors: list[str] = []
    password = (settings.admin_password or "").strip()
    if not password or password in _FORBIDDEN_ADMIN_PASSWORDS:
        errors.append(
            "ADMIN_PASSWORD не задан или совпадает с дефолтом (changeme/admin123/demo123)"
        )
    if settings.minio_secret_key == _DEFAULT_MINIO_SECRET:
        errors.append("MINIO_SECRET_KEY совпадает с дефолтом b2b-commerce-secret")
    db_password = _database_password(settings.database_url)
    if db_password == _DEFAULT_DB_PASSWORD:
        errors.append("DATABASE_URL использует дефолтный пароль b2b_commerce")
    hosts = {host.lower() for host in settings.allowed_host_list}
    if not hosts or hosts.issubset(_LOCAL_ONLY_HOSTS):
        errors.append(
            "ALLOWED_HOSTS должен включать публичный host, не только localhost/127.0.0.1"
        )
    if errors:
        raise SystemExit("Prod config небезопасен:\n- " + "\n- ".join(errors))
