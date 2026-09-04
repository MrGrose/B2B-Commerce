from urllib.parse import quote, unquote


# Проверяет, что путь ведёт на витрину каталога.
def _is_catalog_path(path: str) -> bool:
    base = path.split("?", 1)[0]
    return base == "/catalog" or base.startswith("/catalog/products/")


# Достаёт path из полного URL (referer).
def _path_from_url(url: str) -> str | None:
    if not url.startswith(("http://", "https://")):
        return None
    slash = url.find("/", url.index("//") + 2)
    if slash < 0:
        return None
    return url[slash:]


# Разрешает только внутренние пути каталога для возврата после добавления в корзину.
def safe_catalog_redirect(next_url: str | None, referer: str | None) -> str | None:
    if next_url:
        path = next_url.strip()
        if path.startswith("/") and _is_catalog_path(path):
            return path
    if referer:
        path = _path_from_url(referer)
        if path and _is_catalog_path(path):
            return path
    return None


# Собирает URL с query для flash-сообщения после редиректа.
def redirect_with_message(path: str, param: str, message: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{param}={quote(message, safe='')}"


# Читает сообщение из query (после redirect_with_message).
def query_message(value: str | None) -> str | None:
    if not value:
        return None
    return unquote(value)
