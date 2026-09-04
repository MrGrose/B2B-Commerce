from dataclasses import dataclass
from typing import Any
from uuid import UUID

from b2b_commerce.audit.models import AuditLog

ACTION_LABELS: dict[str, str] = {
    "account.login_change": "Клиент изменил логин",
    "admin.activate": "Администратор включён",
    "admin.create": "Добавлен администратор",
    "admin.deactivate": "Администратор отключён",
    "billing_entity.create": "Добавлено юрлицо поставщика",
    "billing_entity.update": "Изменено юрлицо поставщика",
    "brand.create": "Создан бренд",
    "brand.delete": "Удалён бренд",
    "brand.update": "Изменён бренд",
    "category.create": "Создана категория",
    "category.delete": "Удалена категория",
    "category.margin.update": "Изменена маржа категории",
    "category.update": "Изменена категория",
    "company.activate": "Компания активирована",
    "company.admin_update": "Админ изменил компанию",
    "company.approve": "Заявка компании одобрена",
    "company.create": "Создана компания",
    "company.deactivate": "Компания деактивирована",
    "company.profile_update": "Клиент обновил профиль компании",
    "company.register": "Новая заявка на регистрацию",
    "company.reject": "Заявка компании отклонена",
    "company.reset_password": "Сброшен пароль компании",
    "inventory.correction": "Корректировка остатка на складе",
    "invoice.cancel": "Счёт отменён",
    "invoice.create": "Создан счёт",
    "invoice.pay": "Счёт отмечен оплаченным",
    "invoice.ship": "Счёт отгружен",
    "invoice.update_items": "Админ изменил позиции счёта",
    "invoice.update_items_by_company": "Клиент изменил позиции счёта",
    "product.create": "Создан товар",
    "product.delete": "Товар удалён",
    "product.image.add": "Добавлено фото товара",
    "product.import.create": "Импорт прайса: создан товар",
    "product.import.update": "Импорт прайса: обновлён товар",
    "product.reprice": "Переоценка товара",
    "product.update": "Изменён товар",
    "support.close": "Тикет поддержки закрыт",
    "support.create": "Создан тикет поддержки",
    "support.reply": "Новое сообщение в тикете",
}

ENTITY_TYPE_LABELS: dict[str, str] = {
    "admin_user": "Администратор",
    "billing_entity": "Юрлицо поставщика",
    "brand": "Бренд",
    "category": "Категория",
    "company": "Компания",
    "company_account": "Учётная запись",
    "invoice": "Счёт",
    "product": "Товар",
    "support_ticket": "Тикет поддержки",
}

ACTOR_TYPE_LABELS: dict[str, str] = {
    "admin": "Администратор",
    "company": "Клиент",
    "system": "Система",
}


@dataclass
class AuditLogView:
    id: int
    created_at: Any
    actor_label: str
    actor_id: UUID | None
    action_label: str
    entity_label: str
    entity_id: UUID | None
    details: str


# Возвращает метку действия.
def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action.replace(".", " · "))


# Возвращает метку типа сущности.
def entity_type_label(entity_type: str) -> str:
    return ENTITY_TYPE_LABELS.get(entity_type, entity_type.replace("_", " "))


# Возвращает метку типа актера.
def actor_type_label(actor_type: str) -> str:
    return ACTOR_TYPE_LABELS.get(actor_type, actor_type)


# Форматирует payload для действия.
def _format_name_slug_payload(payload: dict[str, Any]) -> str:
    name = payload.get("name") or payload.get("new_name")
    slug = payload.get("slug")
    if name and slug:
        return f"{name} ({slug})"
    if name:
        return str(name)
    return "—"


# Форматирует payload для действия.
def format_audit_payload(action: str, payload: dict[str, Any] | None) -> str:
    if not payload:
        return "—"
    if action == "account.login_change":
        old_login = payload.get("old_login")
        new_login = payload.get("new_login")
        if old_login and new_login:
            return f"{old_login} → {new_login}"
    if action in {"billing_entity.create", "billing_entity.update"}:
        inn = payload.get("inn")
        return f"ИНН {inn}" if inn else "—"
    if action == "category.update":
        old_name = payload.get("old_name")
        new_name = payload.get("new_name")
        if old_name and new_name:
            return f"{old_name} → {new_name}"
    if action == "category.margin.update":
        name = payload.get("name")
        old_margin = payload.get("old_margin_percent")
        new_margin = payload.get("new_margin_percent")
        parts: list[str] = []
        if name:
            parts.append(str(name))
        if old_margin is not None or new_margin is not None:
            parts.append(f"маржа {old_margin or '—'}% → {new_margin or '—'}%")
        return " · ".join(parts) if parts else "—"
    if action.startswith("category."):
        return _format_name_slug_payload(payload)
    if action == "brand.update":
        old_name = payload.get("old_name")
        new_name = payload.get("new_name")
        if old_name and new_name:
            return f"{old_name} → {new_name}"
    if action.startswith("brand."):
        return _format_name_slug_payload(payload)
    if action.startswith("admin."):
        login = payload.get("login")
        return str(login) if login else "—"
    if action == "company.admin_update" and payload.get("billing_entity_id"):
        return f"Юрлицо поставщика: {payload['billing_entity_id']}"
    if action == "company.profile_update":
        return "Обновлены контактные данные"
    if action == "support.create" and payload.get("subject"):
        return str(payload["subject"])
    if action.startswith("invoice."):
        number = payload.get("number")
        total = payload.get("total")
        parts: list[str] = []
        if number:
            parts.append(f"№ {number}")
        if total:
            parts.append(f"сумма {total} ₽")
        return " · ".join(parts) if parts else "—"
    if action == "product.reprice":
        old_price = payload.get("old_sale_price")
        new_price = payload.get("new_sale_price")
        margin = payload.get("margin_percent")
        parts: list[str] = []
        if old_price and new_price:
            parts.append(f"{old_price} ₽ → {new_price} ₽")
        if margin:
            parts.append(f"маржа категории {margin}%")
        return " · ".join(parts) if parts else "—"
    if action.startswith("product.") or action.startswith("product.import."):
        name = payload.get("name")
        return str(name) if name else "—"
    if action == "inventory.correction":
        delta = payload.get("delta")
        target = payload.get("target_quantity")
        reason = payload.get("reason")
        parts: list[str] = []
        if delta is not None:
            parts.append(f"изменение {delta}")
        if target is not None:
            parts.append(f"остаток {target}")
        if reason:
            parts.append(str(reason))
        return " · ".join(parts) if parts else "—"
    pairs = [f"{key}: {value}" for key, value in payload.items()]
    return "; ".join(pairs) if pairs else "—"


# Форматирует строку аудита.
def format_audit_log(row: AuditLog) -> AuditLogView:
    return AuditLogView(
        id=row.id,
        created_at=row.created_at,
        actor_label=actor_type_label(row.actor_type),
        actor_id=row.actor_id,
        action_label=action_label(row.action),
        entity_label=entity_type_label(row.entity_type),
        entity_id=row.entity_id,
        details=format_audit_payload(row.action, row.payload),
    )
