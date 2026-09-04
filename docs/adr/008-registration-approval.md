# ADR-008: Registration is not catalog access

Status: Accepted

## Context

B2B-каталог с ценами нельзя открывать по факту регистрации. Нужен ручной Approve админа. Существующие `AdminUser` / `CompanyAccount` / cookie-сессии оставляем.

## Decision

- Расширить `Company.status`: `pending` | `active` | `rejected` | `suspended`.
- Не смешивать со `CompanyAccount.is_active` и не использовать `ProductStatus`.
- `require_company` — любой вошедший клиент (профиль, заявка).
- `require_approved_company` — только `status=active` на catalog/cart/invoice HTML и `/api`.
- `find_login` пускает pending/rejected и по-прежнему режет suspended.

## Consequences

Прямой URL `/catalog` и `/api/catalog/products` не обходят очередь. После Approve текущая сессия начинает работать без повторного логина, потому что status читается на каждый запрос.
