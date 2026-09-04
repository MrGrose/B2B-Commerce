from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.auth.service import hash_password
from b2b_commerce.config import Settings
from b2b_commerce.rapira.models import RapiraPriceHistory
from b2b_commerce.rapira.parser import USDT_RUB_PAIR, parse_usdt_rate
from b2b_commerce.rapira.service import sync_rapira_prices


def _payload(ask_price: str = "76.36") -> dict:
    return {
        "code": 0,
        "data": [
            {"symbol": USDT_RUB_PAIR, "askPrice": ask_price},
            {"symbol": "EUR/RUB", "askPrice": "98.10"},
        ],
    }



def test_parse_usdt_rate_success():
    rate = parse_usdt_rate(_payload())
    assert rate == Decimal("76.36")


def test_parse_usdt_rate_api_error():
    with pytest.raises(ValueError, match="Ошибка API Rapira"):
        parse_usdt_rate({"code": 1, "message": "FAIL"})


def test_parse_usdt_rate_missing_usdt_pair():
    with pytest.raises(ValueError, match="USD/RUB"):
        parse_usdt_rate({"code": 0, "data": [{"symbol": "EUR/RUB", "askPrice": "98.10"}]})


@pytest.mark.db
@pytest.mark.asyncio
async def test_sync_rapira_prices_saves_usdt_history(db_session, monkeypatch):
    monkeypatch.setattr(
        "b2b_commerce.rapira.service.fetch_rapira_payload",
        AsyncMock(return_value=_payload(ask_price="77.10")),
    )
    settings = Settings(rapira_api_url="https://example.test/rapira")

    result = await sync_rapira_prices(db_session, settings=settings)
    await db_session.commit()

    assert result.rate == Decimal("77.10")
    assert result.changed is True

    row = await db_session.scalar(select(RapiraPriceHistory))
    assert row is not None
    assert row.source_sku == USDT_RUB_PAIR
    assert row.source_price == Decimal("77.10")
    assert row.product_id is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_sync_rapira_prices_skips_unchanged_rate(db_session, monkeypatch):
    monkeypatch.setattr(
        "b2b_commerce.rapira.service.fetch_rapira_payload",
        AsyncMock(return_value=_payload(ask_price="77.10")),
    )
    settings = Settings(rapira_api_url="https://example.test/rapira")

    first = await sync_rapira_prices(db_session, settings=settings)
    await db_session.commit()
    assert first.changed is True

    second = await sync_rapira_prices(db_session, settings=settings)
    await db_session.commit()
    assert second.changed is False

    total = await db_session.scalar(select(func.count()).select_from(RapiraPriceHistory))
    assert total == 1


@pytest.mark.db
@pytest.mark.asyncio
async def test_api_rapira_sync(client, db_session, monkeypatch):
    admin = AdminUser(
        login="rapira-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()

    monkeypatch.setattr(
        "b2b_commerce.rapira.service.fetch_rapira_payload",
        AsyncMock(return_value=_payload()),
    )

    login = await client.post(
        "/login",
        data={"login": "rapira-admin", "password": "admin-pass"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    response = await client.post("/api/admin/rapira/sync")
    assert response.status_code == 200
    body = response.json()
    assert body["pair"] == "USD/RUB"
    assert body["rate"] == "76.36"
    assert body["changed"] is True


@pytest.mark.db
@pytest.mark.asyncio
async def test_rapira_history_partial(client, db_session, monkeypatch):
    admin = AdminUser(
        login="rapira-history-admin",
        password_hash=hash_password("admin-pass"),
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()

    login = await client.post(
        "/login",
        data={"login": "rapira-history-admin", "password": "admin-pass"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    prices = iter(["77.10", "77.20", "77.30"])

    async def fake_fetch(*_args, **_kwargs):
        return _payload(ask_price=next(prices))

    monkeypatch.setattr("b2b_commerce.rapira.service.fetch_rapira_payload", fake_fetch)

    for _ in range(3):
        await sync_rapira_prices(db_session, settings=Settings())
        await db_session.commit()

    total = await db_session.scalar(select(func.count()).select_from(RapiraPriceHistory))
    assert total == 3

    response = await client.get("/admin/rapira/history")
    assert response.status_code == 200
    assert "77,10" in response.text
    assert "77,20" in response.text
    assert "77,30" in response.text
