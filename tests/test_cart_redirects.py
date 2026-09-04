from b2b_commerce.cart.redirects import safe_catalog_redirect


def test_safe_catalog_redirect_allows_catalog_paths():
    assert safe_catalog_redirect("/catalog", None) == "/catalog"
    assert safe_catalog_redirect("/catalog/products/abc", None) == "/catalog/products/abc"
    assert safe_catalog_redirect(None, "http://localhost/catalog?x=1") == "/catalog?x=1"


def test_safe_catalog_redirect_rejects_external():
    assert safe_catalog_redirect("http://evil.com/catalog", None) is None
    assert safe_catalog_redirect("/cart", None) is None
