import pytest
from httpx import AsyncClient

from test_companies import _login
from test_invoices import _seed_company_product

pytest_plugins = ("test_companies",)


@pytest.mark.db
@pytest.mark.asyncio
async def test_cart_htmx_add_returns_qty_controls(db_session, client: AsyncClient):
    admin, company, product = await _seed_company_product(db_session)
    login = await _login(client, company.login, company.temporary_password)
    assert login.status_code == 303
    await client.post(
        "/change-password",
        data={"new_password": "newpass12"},
        follow_redirects=False,
    )

    catalog = await client.get("/catalog")
    assert catalog.status_code == 200
    assert "catalog-cart-" in catalog.text
    assert "hx-select=\"#catalog-cart-" in catalog.text
    assert "В корзину" in catalog.text

    response = await client.post(
        "/cart/items",
        data={
            "product_id": str(product.id),
            "quantity": "1",
            "mode": "add",
            "next_url": "/catalog",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    body = response.text
    assert "catalog-cart-slot" in body
    assert "qty-catalog" in body
    assert "1 шт" in body
    assert 'id="layout-cart-count"' in body
