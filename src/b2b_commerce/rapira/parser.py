import asyncio
import json
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USDT_RUB_PAIR = "USD/RUB"


# Парсит курс USD/RUB из ответа Rapira API.
def parse_usdt_rate(payload: dict[str, Any]) -> Decimal:
    if payload.get("code") != 0:
        message = payload.get("message") or "unknown error"
        raise ValueError(f"Ошибка API Rapira: {message}")

    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("Поле data в ответе Rapira должно быть массивом")

    pair = next(
        (
            item
            for item in data
            if isinstance(item, dict) and item.get("symbol") == USDT_RUB_PAIR
        ),
        None,
    )
    if pair is None:
        raise ValueError(f"Пара {USDT_RUB_PAIR} не найдена в ответе Rapira")

    try:
        rate = Decimal(str(pair.get("askPrice") or "0"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Некорректный askPrice для {USDT_RUB_PAIR}") from exc
    if rate <= 0:
        raise ValueError(f"Некорректный askPrice для {USDT_RUB_PAIR}")
    return rate


# Возвращает сырой объект пары USDT/RUB из ответа Rapira.
def extract_usdt_pair(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, list):
        return {}
    pair = next(
        (
            item
            for item in data
            if isinstance(item, dict) and item.get("symbol") == USDT_RUB_PAIR
        ),
        None,
    )
    return pair if isinstance(pair, dict) else {}


# Синхронно загружает JSON из Rapira API.
def _fetch_rapira_payload_sync(api_url: str, timeout_seconds: float = 8.0) -> dict[str, Any]:
    request = Request(api_url.strip(), headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as exc:
        raise ValueError(f"Ошибка HTTP Rapira: {exc.code}") from exc
    except URLError as exc:
        raise ValueError(f"Ошибка сети Rapira: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Ответ Rapira не является JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Ответ Rapira должен быть JSON-объектом")
    return payload


# Загружает JSON из открытого Rapira API (public market-rates endpoint).
async def fetch_rapira_payload(
    api_url: str,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    if not api_url.strip():
        raise ValueError("Rapira API URL не настроен")
    return await asyncio.to_thread(
        _fetch_rapira_payload_sync,
        api_url,
        timeout_seconds=timeout_seconds,
    )
