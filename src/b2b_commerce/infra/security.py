import hmac
import secrets
from urllib.parse import parse_qs, urlparse

from fastapi import Request
from starlette.responses import Response

from b2b_commerce.config import Settings

CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_EXEMPT_PATHS = frozenset({"/api/auth/login", "/api/auth/register"})
DOCS_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})


# Генерирует новый CSRF-токен.
def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


# True, если путь — документация OpenAPI.
def is_docs_path(path: str) -> bool:
    return path in DOCS_PATHS or path.startswith("/docs/")


# True, если mutating endpoint не требует CSRF (unauthenticated login/register).
def is_csrf_exempt(path: str) -> bool:
    return path in CSRF_EXEMPT_PATHS


# True, если unsafe HTTP method.
def is_unsafe_method(method: str) -> bool:
    return method.upper() in UNSAFE_METHODS


# Достаёт или создаёт CSRF-токен для запроса.
def ensure_csrf_token(request: Request, settings: Settings) -> str:
    cookie_token = request.cookies.get(settings.csrf_cookie, "")
    if cookie_token:
        request.state.csrf_token = cookie_token
        return cookie_token
    token = new_csrf_token()
    request.state.csrf_token = token
    request.state.csrf_token_is_new = True
    return token


# Ставит CSRF-cookie на ответ, если токен новый или cookie отсутствует.
def attach_csrf_cookie(response: Response, request: Request, settings: Settings) -> None:
    token = getattr(request.state, "csrf_token", None)
    if not token:
        return
    is_new = getattr(request.state, "csrf_token_is_new", False)
    if not is_new and request.cookies.get(settings.csrf_cookie) == token:
        return
    response.set_cookie(
        settings.csrf_cookie,
        token,
        samesite="lax",
        secure=settings.is_prod,
        path="/",
        httponly=False,
    )


# Сравнивает CSRF-токен из cookie с полем формы или заголовком.
def csrf_tokens_match(cookie_token: str, submitted: str | None) -> bool:
    if not cookie_token or not submitted:
        return False
    return hmac.compare_digest(cookie_token, submitted)


# Переигрывает тело запроса после чтения в middleware.
def _replay_request_body(request: Request, body: bytes) -> None:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive  # noqa: SLF001


# Достаёт CSRF из urlencoded-тела без request.form().
def _csrf_from_urlencoded(body: bytes) -> str | None:
    values = parse_qs(body.decode(), keep_blank_values=True).get(CSRF_FORM_FIELD, [])
    return values[0] if values else None


# Сравнивает CSRF-токен из cookie с полем формы или заголовком.
async def validate_csrf(request: Request, settings: Settings) -> bool:
    if not is_unsafe_method(request.method):
        return True
    if is_csrf_exempt(request.url.path):
        return True
    cookie_token = request.cookies.get(settings.csrf_cookie, "")
    if not cookie_token:
        cookie_token = getattr(request.state, "csrf_token", "")
    header = request.headers.get(CSRF_HEADER)
    if header and csrf_tokens_match(cookie_token, header.strip()):
        return True
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        body = await request.body()
        _replay_request_body(request, body)
        field = _csrf_from_urlencoded(body)
        if field is not None and csrf_tokens_match(cookie_token, field):
            return True
    if "multipart/form-data" in content_type:
        body = await request.body()
        _replay_request_body(request, body)
        form = await request.form()
        field = form.get(CSRF_FORM_FIELD)
        if field is not None and csrf_tokens_match(cookie_token, str(field)):
            return True
    return False


# Извлекает hostname из Origin или Referer.
def _origin_host(request: Request) -> str | None:
    origin = request.headers.get("origin", "").strip()
    if origin:
        parsed = urlparse(origin)
        if parsed.hostname:
            return parsed.hostname.lower()
    referer = request.headers.get("referer", "").strip()
    if referer:
        parsed = urlparse(referer)
        if parsed.hostname:
            return parsed.hostname.lower()
    return None


# Проверяет Origin/Referer для defense-in-depth.
def validate_origin(request: Request, settings: Settings) -> bool:
    if not is_unsafe_method(request.method):
        return True
    if is_csrf_exempt(request.url.path):
        return True
    if not settings.is_prod:
        return True
    host = _origin_host(request)
    if host is None:
        return False
    allowed = {h.lower() for h in settings.allowed_host_list}
    return host in allowed


# Собирает security headers для ответа.
def security_headers(settings: Settings) -> dict[str, str]:
    csp = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    headers = {
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": csp,
    }
    if settings.is_prod:
        headers["Strict-Transport-Security"] = "max-age=31536000"
    return headers


# True, если storage_key допустим для /media.
def is_allowed_media_key(storage_key: str) -> bool:
    if not storage_key or storage_key.startswith("/"):
        return False
    if ".." in storage_key.split("/"):
        return False
    return storage_key.startswith("products/")
