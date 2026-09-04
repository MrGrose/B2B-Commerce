from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from b2b_commerce.main import app


def _app_js() -> str:
    js_path = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "static" / "app.js"
    return js_path.read_text(encoding="utf-8")

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "templates"
client = TestClient(app)



def _read(rel_path: str) -> str:
    return (TEMPLATES / rel_path).read_text(encoding="utf-8")


def _app_css() -> str:
    css_path = Path(__file__).resolve().parents[1] / "src" / "b2b_commerce" / "static" / "app.css"
    return css_path.read_text(encoding="utf-8")


def test_auth_shell_macro_defined() -> None:
    html = _read("macros/ui.html")
    assert "macro auth_shell" in html
    assert "data-auth-page" in html
    assert "auth-page__backdrop--mono" in html
    assert "auth-page__backdrop--color" in html


def test_login_template_uses_auth_shell_and_canonical_form() -> None:
    html = _read("auth/login.html")
    assert "auth_shell(" in html
    assert 'class="form-stack auth-form" data-auth-form' in html
    assert 'hx-boost="false"' in html
    assert 'action="/login"' in html
    assert "auth-footer" in html
    assert 'href="/register"' in html
    assert 'style=' not in html


def test_register_template_uses_wide_card_and_field_hint() -> None:
    html = _read("auth/register.html")
    assert "auth_shell(" in html
    assert "wide=true" in html
    assert "field-hint" in html
    assert "field-optional" in html
    assert 'action="/register"' in html
    assert 'href="/login"' in html


def test_pending_and_rejected_use_auth_status_panel() -> None:
    pending = _read("auth/pending.html")
    rejected = _read("auth/rejected.html")
    assert "auth-status" in pending
    assert "message-stack" in pending
    assert "persist=true" in pending
    assert "auth-status" in rejected
    assert "alert(" in rejected
    assert "'danger'" in rejected


def test_auth_login_hero_uses_neutral_css_backdrop() -> None:
    css = _app_css()
    assert ".auth-page__backdrop" in css
    assert "linear-gradient" in css
    assert "login-hero.jpg" not in css
    assert "padel-login.jpg" not in css


def test_auth_page_styles_present() -> None:
    css = _app_css()
    assert ".auth-page__backdrop" in css
    assert "linear-gradient" in css
    assert "mask-image" in css
    assert ".auth-page__backdrop--color" in css
    assert "--auth-spot:140px" in css
    assert ".auth-card--wide" in css
    assert ".field-hint" in css
    assert "prefers-reduced-motion" in css
    assert ".auth-status" in css


def test_base_template_initializes_auth_pages() -> None:
    html = _app_js()
    assert "function initAuthPages" in html
    assert "initAuthPages(root)" in html
    assert "prefers-reduced-motion" in html


def test_login_page_renders_auth_shell() -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "Вход" in response.text
    assert 'data-auth-page' in response.text
    assert 'data-auth-form' in response.text
    assert 'name="login"' in response.text
    assert 'name="password"' in response.text


def test_register_page_renders_auth_shell() -> None:
    response = client.get("/register")
    assert response.status_code == 200
    assert "Регистрация" in response.text
    assert "auth-card--wide" in response.text
    assert 'name="inn"' in response.text


@pytest.mark.db
async def test_login_invalid_credentials_show_error(client, monkeypatch) -> None:
    async def _never_limited(*_args, **_kwargs):
        return False

    monkeypatch.setattr("b2b_commerce.auth.router.is_login_rate_limited", _never_limited)
    response = await client.post(
        "/login",
        data={"login": "nobody", "password": "wrong-password"},
        follow_redirects=False,
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    assert response.status_code == 401
    assert "alert-danger" in response.text
    assert "data-auth-page" in response.text


def test_register_invalid_password_shows_error() -> None:
    response = client.post(
        "/register",
        data={
            "login": "shortpw",
            "password": "short",
            "name": "Test Co",
            "legal_name": "Test Co LLC",
            "inn": "7701234567",
            "contact_email": "shortpw@example.com",
            "contact_phone": "+79990001122",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "alert-danger" in response.text
    assert "auth-card--wide" in response.text
    assert 'value="shortpw"' in response.text
