from pathlib import Path


def _app_js() -> str:
    js_path = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "static" / "app.js"
    return js_path.read_text(encoding="utf-8")

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "templates"
LAYOUT_TEMPLATES = {
    "layouts/admin.html",
    "layouts/customer.html",
    "companies/new.html",
    "companies/detail.html",
    "companies/edit.html",
    "companies/billing.html",
    "companies/billing_edit.html",
    "cart/view.html",
    "support/new.html",
    "support/detail.html",
    "support/admin/detail.html",
    "auth/profile.html",
    "invoices/detail.html",
    "invoices/admin/detail.html",
    "products/new.html",
    "products/detail.html",
    "products/import.html",
    "macros/ui.html",
    "macros/filter_bar.html",
}


def _read(rel_path: str) -> str:
    return (TEMPLATES / rel_path).read_text(encoding="utf-8")



def _app_css() -> str:
    css_path = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "static" / "app.css"
    return css_path.read_text(encoding="utf-8")


def test_admin_layout_has_htmx_boost_target() -> None:
    html = _read("layouts/admin.html")
    assert "hx-boost=\"true\"" in html
    assert "hx-target=\"main.content\"" in html
    assert "hx-select=\"main.content\"" in html
    assert 'method="post" action="/logout"' in html
    assert 'class="btn-icon topbar-logout"' in html
    assert "<div class=\"side-bottom\">" not in html


def test_customer_layout_has_htmx_boost_target() -> None:
    html = _read("layouts/customer.html")
    assert "hx-boost=\"true\"" in html
    assert "hx-target=\"main.content\"" in html
    assert "hx-select=\"main.content\"" in html
    assert 'method="post" action="/logout"' in html
    assert 'class="btn-icon topbar-logout"' in html
    assert "<div class=\"side-bottom\">" not in html


def test_post_forms_in_app_layouts_disable_boost() -> None:
    missing = []
    for rel_path in sorted(LAYOUT_TEMPLATES):
        html = _read(rel_path)
        for line_no, line in enumerate(html.splitlines(), start=1):
            if "method=\"post\"" not in line:
                continue
            window = "\n".join(html.splitlines()[max(0, line_no - 2):line_no + 3])
            if "hx-boost=\"false\"" not in window:
                missing.append(f"{rel_path}:{line_no}")
    assert missing == []


def test_base_layout_has_alert_auto_hide() -> None:
    html = _app_js()
    assert "initAlertAutoHide" in html
    assert "ALERT_HIDE_MS = 2000" in html
    assert ".settings-toast" in html
    assert "alertHideMs" in html
    assert "data-credential-flash" in html
    assert "data-alert-persist" in html


def test_credential_flash_excluded_from_generic_auto_hide() -> None:
    html = _app_js()
    assert "closest('[data-credential-flash]')" in html
    assert "classList.contains('alert-danger')" in html


def test_single_htmx_after_swap_listener() -> None:
    html = _app_js()
    assert html.count("addEventListener('htmx:afterSwap'") == 1
    assert "initAppShell" in html
    assert "updateSidebarActive" in html


def test_invoice_download_links_disable_boost() -> None:
    for rel_path in ("invoices/detail.html", "invoices/admin/detail.html"):
        html = _read(rel_path)
        assert "download.pdf\" hx-boost=\"false\"" in html
        assert "download.xlsx" not in html
        assert "invoice_preview_button" in html


def test_catalog_detail_uses_global_js_init() -> None:
    html = _read("catalog/detail.html")
    js = _app_js()
    assert "data-description-toggle" in html
    assert "<script>" not in html
    assert "initDescriptionToggles" in js


def test_company_create_shows_optional_field_hints() -> None:
    html = _read("companies/new.html")
    assert html.count("(необязательно)") >= 5
    assert 'for="legal_name"' in html


def test_finance_dashboard_has_no_unpaid_section() -> None:
    html = _read("finance/dashboard.html")
    assert "Неоплаченные счета" not in html
    assert "unpaid_invoices" not in html
    assert "Маржа отгрузок" not in html
    assert "Чистая прибыль" not in html
    assert "stat_card('Прибыль'" in html
    assert "Выручка" in html
    assert "оборот по отгруженным счетам" in html


def test_admin_dashboard_has_greeting() -> None:
    html = _read("admin/dashboard.html")
    assert "Добрый день" in html
    assert "layout_user_login" in html


def test_catalog_product_detail_uses_stock_badge() -> None:
    html = _read("catalog/detail.html")
    assert "stock_availability_badge" in html
    assert "stock_availability_text" not in html
    macro = _read("macros/ui.html")
    assert "badge('Мало', 'warning')" in macro
    assert "badge('Достаточно', 'shipped')" in macro
    assert "badge('Много', 'success')" in macro
    assert "stock_availability_text" not in macro


def test_product_status_has_admin_visibility_hint() -> None:
    macro = _read("macros/ui.html")
    assert "Активен — на сайте" in macro
    for rel_path in ("products/detail.html", "products/new.html"):
        html = _read(rel_path)
        assert "product_status_hint()" in html


def test_credentials_page_has_combined_copy_button() -> None:
    credentials = _read("companies/credentials.html")
    macro = _read("macros/ui.html")
    assert "copy_credentials_btn(login, temporary_password)" in credentials
    assert "copy_btn(login)" in credentials
    assert "data-copy-credentials" in macro
    assert "Скопировать логин и пароль" in macro


def test_company_status_badge_uses_masculine_active() -> None:
    html = _read("macros/ui.html")
    assert "badge('Активен', 'active')" in html
    assert "badge('Активна', 'active')" not in html
    assert "badge('Отклонён', 'canceled')" in html
    assert "badge('Приостановлен', 'muted')" in html


def test_admin_sidebar_has_no_inventory_link() -> None:
    html = _read("layouts/admin.html")
    assert 'href="/admin/inventory"' not in html
    assert "> Склад</a>" not in html
    assert "> Товары</a>" in html


def test_product_detail_preserves_inventory_block() -> None:
    html = _read("products/detail.html")
    assert "product-detail-grid" in html
    assert "product-params-form" in html
    assert "sale-field" in html
    assert "<strong>Остаток</strong>" in html
    assert "Резерв" in html
    assert "Доступно" in html
    assert "ТОВАР · ДОБАВЛЕН" in html
    assert "local_datetime(true)" in html
    assert "/admin/products/{{ product.id }}/inventory/correct" in html
    assert 'data-reservations-url="/admin/products/{{ product.id }}/reservations"' in html
    assert "stock-reserve-link" in html
    assert "Параметры товара" in html


def test_admin_product_detail_form_layout_and_copy() -> None:
    html = _read("products/detail.html")
    assert "{% if product.description %}<p>" not in html
    assert (
        html.find('for="brand_id"')
        < html.find('for="model_year"')
        < html.find('for="cost_price"')
    )
    assert html.find("margin-chip") > html.find('for="sale_price"')
    assert html.find("margin-chip") < html.find('for="status"')
    assert "{% else %}" in html and "—" in html
    assert "Идентификатор" not in html
    assert "detail-mini" not in html
    assert "Можно несколько сразу" in html
    assert 'name="images"' in html and "multiple" in html
    assert 'class="gallery-upload-form"' in html
    assert 'type="submit"' in html
    assert 'onchange="this.form.submit()"' not in html
    css = _app_css()
    assert "admin-products-table" not in css
    assert "table-product-thumb" not in css
    assert ".detail-mini" not in css
    assert ".detail-form textarea.form-control" in css
    assert "font-weight:400" in css.replace(" ", "") or "font-weight: 400" in css


def test_catalog_filter_all_pill_says_all() -> None:
    html = _read("macros/filter_bar.html")
    assert "Все товары" not in html
    assert "Все <span>{{ catalog_total_all }}</span>" in html


def test_product_gallery_lightbox_pages_photos() -> None:
    js = _app_js()
    assert "gallery-lightbox__prev" in js
    assert "ArrowLeft" in js
    assert "ArrowRight" in js
    css = _app_css()
    assert "gallery-lightbox__next" in css

def test_admin_products_list_shows_stock_columns() -> None:
    html = _read("products/list.html")
    assert "catalog-grid" in html
    assert "product-card" in html
    assert "<dt>Остаток</dt>" in html
    assert "<dt>Резерв</dt>" in html
    assert "<dt>Доступно</dt>" in html
    assert "<th>Добавлен</th>" not in html
    assert "stock-reserve-link" in html
    assert 'data-reservations-url="/admin/products/{{ product.id }}/reservations"' in html
    assert "image_src=" in html


def test_product_visual_macro_supports_image() -> None:
    html = _read("macros/ui.html")
    assert "image_src=''" in html
    assert "<img src=" in html


def test_product_reservations_fragment_template() -> None:
    html = _read("products/reservations_fragment.html")
    assert "/admin/invoices/{{ row.invoice_id }}" in html
    assert "{{ row.invoice_number }}" in html


def test_app_shell_has_sticky_topbar_styles() -> None:
    css = _app_css()
    assert "position: sticky" in css
    assert "--topbar-height" in css
    assert "scrollbar-gutter:stable" in css or "scrollbar-gutter: stable" in css


def test_mobile_app_shell_does_not_clip_topbar_sticky() -> None:
    css = _app_css()
    assert ".app{width:100%;overflow-x:hidden}" not in css.replace(" ", "")
    assert "overflow-x: clip" in css


def test_filter_bar_styles_present() -> None:
    css = _app_css()
    assert ".filter-bar__main" in css
    assert ".filter-bar__dialog" in css
    assert ".filter-bar__trigger" in css


def test_base_template_initializes_filter_bars() -> None:
    html = _app_js()
    assert "function initFilterBars" in html
    assert "initFilterBars(root)" in html


def test_base_template_loads_app_js() -> None:
    html = _read("base.html")
    assert "url_for('static', path='app.js')" in html
    assert "<script>" not in html
