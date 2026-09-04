"""Regression: client IP за reverse proxy и защита rate limit от XFF spoof."""

import pytest
from httpx import ASGITransport, AsyncClient

from b2b_commerce.main import app

PROD_TRUSTED_PROXIES = "172.28.10.0/24,127.0.0.1,::1"
DOCKER_GATEWAY = "172.28.10.1"
PROD_DOMAIN = "b2b-commerce.example.com"
TRUSTED_PROXY = ("172.28.10.5", 45678)
TRUSTED_PROXY_LOOPBACK = ("127.0.0.1", 45678)
UNTRUSTED_CLIENT = ("203.0.113.50", 54321)
REAL_CLIENT_IP = "198.51.100.25"
SPOOFED_IP = "203.0.113.99"


async def _client_ip_after_proxy_middleware(
    *,
    proxy_host: str,
    x_forwarded_for: str,
    trusted_hosts: str = PROD_TRUSTED_PROXIES,
) -> str:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    captured: dict[str, str] = {}

    async def capture_app(scope, receive, send):
        captured["host"] = scope["client"][0]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = ProxyHeadersMiddleware(capture_app, trusted_hosts=trusted_hosts)
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", x_forwarded_for.encode())],
        "client": (proxy_host, 45678),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    await middleware(scope, receive, send)
    return captured["host"]


async def _post_login(
    *,
    transport_client: tuple[str, int],
    x_forwarded_for: str | None = None,
) -> int:
    headers: dict[str, str] = {}
    if x_forwarded_for is not None:
        headers["X-Forwarded-For"] = x_forwarded_for
    transport = ASGITransport(app=app, client=transport_client)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/login")
        csrf = client.cookies.get("b2b_commerce_csrf")
        response = await client.post(
            "/login",
            data={"login": "nobody", "password": "wrong-password", "csrf_token": csrf},
            headers=headers,
        )
        return response.status_code


@pytest.mark.no_rate_limit_client_key
@pytest.mark.asyncio
async def test_proxy_middleware_ignores_spoofed_xff_from_untrusted_client():
    host = await _client_ip_after_proxy_middleware(
        proxy_host=UNTRUSTED_CLIENT[0],
        x_forwarded_for=SPOOFED_IP,
    )
    assert host == UNTRUSTED_CLIENT[0]


@pytest.mark.no_rate_limit_client_key
@pytest.mark.asyncio
async def test_proxy_middleware_trusted_proxy_sets_real_client_ip():
    host = await _client_ip_after_proxy_middleware(
        proxy_host=TRUSTED_PROXY[0],
        x_forwarded_for=REAL_CLIENT_IP,
    )
    assert host == REAL_CLIENT_IP


@pytest.mark.no_rate_limit_client_key
@pytest.mark.asyncio
async def test_proxy_middleware_spoof_chain_without_caddy_strip_is_vulnerable():
    """Документирует root cause: без strip в Caddy цепочка spoof,proxy даёт spoof в API."""
    host = await _client_ip_after_proxy_middleware(
        proxy_host=TRUSTED_PROXY[0],
        x_forwarded_for=f"{SPOOFED_IP}, {TRUSTED_PROXY[0]}",
    )
    assert host == SPOOFED_IP


@pytest.mark.no_rate_limit_client_key
@pytest.mark.asyncio
async def test_regression_rate_limit_uses_same_client_key_despite_spoofed_xff(monkeypatch):
    seen_keys: list[str] = []

    async def capture_client_key(_settings, client_key: str) -> bool:
        seen_keys.append(client_key)
        return False

    async def fail_auth(*_args, **_kwargs):
        return None

    monkeypatch.setattr("b2b_commerce.auth.router.is_login_rate_limited", capture_client_key)
    monkeypatch.setattr("b2b_commerce.auth.router.authenticate", fail_auth)

    client_ip = "203.0.113.77"
    for spoof in ("1.2.3.4", "5.6.7.8", "9.10.11.12"):
        status = await _post_login(
            transport_client=(client_ip, 50001),
            x_forwarded_for=spoof,
        )
        assert status == 401

    assert seen_keys == [client_ip, client_ip, client_ip]


@pytest.mark.no_rate_limit_client_key
@pytest.mark.asyncio
async def test_regression_rate_limit_uses_client_ip_from_trusted_proxy(monkeypatch):
    seen_keys: list[str] = []

    async def capture_client_key(_settings, client_key: str) -> bool:
        seen_keys.append(client_key)
        return False

    async def fail_auth(*_args, **_kwargs):
        return None

    monkeypatch.setattr("b2b_commerce.auth.router.is_login_rate_limited", capture_client_key)
    monkeypatch.setattr("b2b_commerce.auth.router.authenticate", fail_auth)

    status = await _post_login(
        transport_client=TRUSTED_PROXY_LOOPBACK,
        x_forwarded_for=REAL_CLIENT_IP,
    )
    assert status == 401
    assert seen_keys == [REAL_CLIENT_IP]

async def _scheme_and_static_url_after_proxy_middleware(
    *,
    proxy_host: str,
    x_forwarded_proto: str,
    host_header: str,
    trusted_hosts: str = PROD_TRUSTED_PROXIES,
) -> tuple[str, str]:
    from starlette.requests import Request
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from b2b_commerce.main import app

    captured: dict[str, str] = {}

    async def capture_app(scope, receive, send):
        request = Request(scope, receive)
        captured["scheme"] = request.url.scheme
        captured["static_url"] = str(request.url_for("static", path="app.css"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = ProxyHeadersMiddleware(capture_app, trusted_hosts=trusted_hosts)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/login",
        "headers": [
            (b"x-forwarded-proto", x_forwarded_proto.encode()),
            (b"host", host_header.encode()),
        ],
        "client": (proxy_host, 45678),
        "server": ("127.0.0.1", 8000),
        "scheme": "http",
        "root_path": "",
        "query_string": b"",
        "app": app,
        "router": app.router,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    await middleware(scope, receive, send)
    return captured["scheme"], captured["static_url"]


@pytest.mark.no_rate_limit_client_key
@pytest.mark.asyncio
async def test_host_caddy_docker_gateway_forwarded_proto_https_generates_https_static_urls():
    """Host Caddy → published port: TCP peer 172.28.10.1 должен доверять X-Forwarded-Proto."""
    scheme, static_url = await _scheme_and_static_url_after_proxy_middleware(
        proxy_host=DOCKER_GATEWAY,
        x_forwarded_proto="https",
        host_header=PROD_DOMAIN,
    )
    assert scheme == "https"
    assert static_url == f"https://{PROD_DOMAIN}/static/app.css"


@pytest.mark.no_rate_limit_client_key
@pytest.mark.asyncio
async def test_host_caddy_docker_gateway_without_trusted_subnet_keeps_http_static_urls():
    scheme, static_url = await _scheme_and_static_url_after_proxy_middleware(
        proxy_host=DOCKER_GATEWAY,
        x_forwarded_proto="https",
        host_header=PROD_DOMAIN,
        trusted_hosts="127.0.0.1,::1",
    )
    assert scheme == "http"
    assert static_url == f"http://{PROD_DOMAIN}/static/app.css"
