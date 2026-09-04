from datetime import UTC, datetime
from uuid import UUID

from b2b_commerce.http import (
    admin_products_url,
    catalog_url,
    datetime_iso,
    local_datetime,
    product_tone,
    user_initials,
)


def test_product_tone_int() -> None:
    assert product_tone(0) == "lime"
    assert product_tone(1) == "blue"


def test_product_tone_uuid() -> None:
    product_id = UUID("00000000-0000-0000-0000-000000000001")
    assert product_tone(product_id) == "blue"


def test_product_tone_asyncpg_uuid() -> None:
    from asyncpg.pgproto.pgproto import UUID as AsyncPgUUID

    product_id = AsyncPgUUID("00000000-0000-0000-0000-000000000002")
    assert product_tone(product_id) == "orange"


def test_product_tone_asyncpg_uuid_like() -> None:
    class AsyncPgUUID:
        def __str__(self) -> str:
            return "00000000-0000-0000-0000-000000000002"

    assert product_tone(AsyncPgUUID()) == "orange"


def test_user_initials() -> None:
    assert user_initials("demo") == "DE"
    assert user_initials("cart-co-ca99b9") == "CC"


def test_local_datetime_none() -> None:
    assert str(local_datetime(None)) == "—"


def test_local_datetime_renders_time_tag() -> None:
    value = datetime(2026, 8, 23, 14, 3, 35, tzinfo=UTC)
    html = str(local_datetime(value))
    assert 'class="local-datetime"' in html
    assert 'datetime="2026-08-23T14:03:35+00:00"' in html


def test_datetime_iso_from_string() -> None:
    assert datetime_iso("2026-08-23T14:03:35+00:00") == "2026-08-23T14:03:35+00:00"

def test_catalog_url_empty() -> None:
    assert catalog_url() == "/catalog"


def test_catalog_url_all_params() -> None:
    url = catalog_url(
        "wilson",
        "11111111-1111-1111-1111-111111111111",
        brand_id="22222222-2222-2222-2222-222222222222",
        model_year=2024,
        sort="price_asc",
        page=2,
    )
    assert "q=wilson" in url
    assert "category_id=11111111-1111-1111-1111-111111111111" in url
    assert "brand_id=22222222-2222-2222-2222-222222222222" in url
    assert "model_year=2024" in url
    assert "sort=price_asc" in url
    assert "page=2" in url


def test_catalog_url_omits_page_one() -> None:
    url = catalog_url("ball", page=1)
    assert url == "/catalog?q=ball"
    assert "page=" not in url


def test_admin_products_url_empty() -> None:
    assert admin_products_url() == "/admin/products"


def test_admin_products_url_all_params() -> None:
    url = admin_products_url(
        "racket",
        "22222222-2222-2222-2222-222222222222",
        "11111111-1111-1111-1111-111111111111",
        "2024",
        "active",
        "price_asc",
        2,
    )
    assert "q=racket" in url
    assert "brand_id=22222222-2222-2222-2222-222222222222" in url
    assert "category_id=11111111-1111-1111-1111-111111111111" in url
    assert "model_year=2024" in url
    assert "status=active" in url
    assert "sort=price_asc" in url
    assert "page=2" in url
