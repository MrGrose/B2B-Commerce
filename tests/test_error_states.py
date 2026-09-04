from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "templates"


def _read(rel_path: str) -> str:
    return (TEMPLATES / rel_path).read_text(encoding="utf-8")


def _app_css() -> str:
    css_path = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "static" / "app.css"
    return css_path.read_text(encoding="utf-8")


def test_error_page_macro_defined() -> None:
    html = _read("macros/ui.html")
    assert "macro error_page" in html
    assert "error-page__actions" in html


def test_empty_state_supports_icon_and_compact() -> None:
    html = _read("macros/ui.html")
    assert "icon='package'" in html or 'icon=\'package\'' in html
    assert "compact=false" in html
    assert "empty-compact" in html


def test_not_found_uses_error_page() -> None:
    html = _read("not_found.html")
    assert "error_page(" in html
    assert "empty_state(" not in html


def test_forbidden_uses_error_page_back_action() -> None:
    html = _read("forbidden.html")
    assert "error_page(" in html
    assert "primary_back=true" in html


def test_server_error_template_present() -> None:
    html = _read("server_error.html")
    assert "Что-то пошло не так" in html
    assert "primary_reload=true" in html


def test_bad_request_template_present() -> None:
    html = _read("bad_request.html")
    assert "Неверный запрос" in html


def test_error_page_styles_present() -> None:
    css = _app_css()
    assert ".error-page__icon" in css
    assert ".empty-compact" in css
    assert ".alert-info" in css


def test_finance_dashboard_uses_compact_empty_state() -> None:
    html = _read("finance/dashboard.html")
    assert "compact=true" in html
    assert "panel-empty" not in html


def test_companies_list_uses_empty_state_instead_of_colspan() -> None:
    html = _read("companies/list.html")
    assert "empty_state(" in html
    assert "colspan" not in html
