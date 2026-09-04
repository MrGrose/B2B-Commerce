import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from b2b_commerce.auth.models import AdminUser
from b2b_commerce.companies.models import Company
from b2b_commerce.enums import NotificationKind, NotificationRecipientType
from b2b_commerce.invoices.models import Invoice
from b2b_commerce.support.models import SupportTicket

logger = logging.getLogger(__name__)


def _message_preview(body: str) -> str:
    preview = body.strip()
    if len(preview) > 120:
        return preview[:117] + "..."
    return preview


# Пишет доменное событие в backend log (UI уведомлений — позже).
def _log_notification(
    kind: str,
    recipient_type: str,
    recipient_id: UUID,
    title: str,
    body: str | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
) -> None:
    logger.info(
        "notification kind=%s recipient=%s:%s title=%r body=%r entity=%s:%s",
        kind,
        recipient_type,
        recipient_id,
        title,
        body,
        entity_type,
        entity_id,
    )


# Уведомляет активных админов о новом тикете поддержки.
async def emit_support_new_ticket(db: AsyncSession, ticket: SupportTicket) -> None:
    company = await db.get(Company, ticket.company_id)
    company_name = company.name if company else "Компания"
    admin_ids = (
        await db.scalars(select(AdminUser.id).where(AdminUser.is_active.is_(True)))
    ).all()
    if not admin_ids:
        logger.warning("Нет активных админов для уведомления о тикете %s", ticket.id)
        return
    title = f"Новое обращение: {ticket.subject}"
    for admin_id in admin_ids:
        _log_notification(
            kind=NotificationKind.SUPPORT_NEW_TICKET.value,
            recipient_type=NotificationRecipientType.ADMIN.value,
            recipient_id=admin_id,
            title=title,
            body=company_name,
            entity_type="support_ticket",
            entity_id=ticket.id,
        )


# Уведомляет компанию об истечении срока счёта.
async def emit_invoice_expired(db: AsyncSession, invoice: Invoice) -> None:
    _log_notification(
        kind=NotificationKind.INVOICE_EXPIRED.value,
        recipient_type=NotificationRecipientType.COMPANY.value,
        recipient_id=invoice.company_id,
        title=f"Счёт {invoice.number} истёк",
        body="Срок оплаты истёк. Оформите новый счёт при необходимости.",
        entity_type="invoice",
        entity_id=invoice.id,
    )


# Уведомляет админов о новом сообщении клиента в тикете.
async def emit_support_client_message(
    db: AsyncSession,
    ticket: SupportTicket,
    preview: str,
) -> None:
    company = await db.get(Company, ticket.company_id)
    company_name = company.name if company else "Компания"
    admin_ids = (
        await db.scalars(select(AdminUser.id).where(AdminUser.is_active.is_(True)))
    ).all()
    if not admin_ids:
        logger.warning("Нет активных админов для уведомления о сообщении %s", ticket.id)
        return
    title = f"Новое сообщение: {ticket.subject}"
    body = _message_preview(preview) or company_name
    for admin_id in admin_ids:
        _log_notification(
            kind=NotificationKind.SUPPORT_CLIENT_MESSAGE.value,
            recipient_type=NotificationRecipientType.ADMIN.value,
            recipient_id=admin_id,
            title=title,
            body=body,
            entity_type="support_ticket",
            entity_id=ticket.id,
        )


# Уведомляет компанию об ответе админа в тикете.
async def emit_support_reply(
    db: AsyncSession,
    ticket: SupportTicket,
    preview: str,
) -> None:
    _log_notification(
        kind=NotificationKind.SUPPORT_REPLY.value,
        recipient_type=NotificationRecipientType.COMPANY.value,
        recipient_id=ticket.company_id,
        title=f"Ответ по обращению: {ticket.subject}",
        body=_message_preview(preview) or "Новое сообщение от поддержки",
        entity_type="support_ticket",
        entity_id=ticket.id,
    )


# Уведомляет компанию об оплате счёта.
async def emit_invoice_paid(db: AsyncSession, invoice: Invoice) -> None:
    _log_notification(
        kind=NotificationKind.INVOICE_PAID.value,
        recipient_type=NotificationRecipientType.COMPANY.value,
        recipient_id=invoice.company_id,
        title=f"Счёт {invoice.number} оплачен",
        body="Оплата подтверждена. Ожидайте отгрузку.",
        entity_type="invoice",
        entity_id=invoice.id,
    )


# Уведомляет компанию об отгрузке счёта.
async def emit_invoice_shipped(db: AsyncSession, invoice: Invoice) -> None:
    _log_notification(
        kind=NotificationKind.INVOICE_SHIPPED.value,
        recipient_type=NotificationRecipientType.COMPANY.value,
        recipient_id=invoice.company_id,
        title=f"Счёт {invoice.number} отгружен",
        body="Товар отгружен по счёту.",
        entity_type="invoice",
        entity_id=invoice.id,
    )
