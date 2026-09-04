from enum import StrEnum


class CompanyStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


class ProductStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class InvoiceStatus(StrEnum):
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    SHIPPED = "shipped"
    # Legacy DB CHECK value only; runtime terminal state is SHIPPED (no code sets completed).
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELED = "canceled"


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    CONSUMED = "consumed"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    REJECTED = "rejected"


class StockMovementType(StrEnum):
    INITIAL = "initial"
    CORRECTION = "correction"
    SHIPMENT = "shipment"
    # Schema/CHECK reserved; no MVP UI or service creates return movements.
    RETURN = "return"


class SessionSubjectType(StrEnum):
    ADMIN = "admin"
    COMPANY = "company"


class SupportTicketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class RapiraMatchStatus(StrEnum):
    UNMATCHED = "unmatched"
    MATCHED = "matched"
    IGNORED = "ignored"


class NotificationRecipientType(StrEnum):
    ADMIN = "admin"
    COMPANY = "company"


class NotificationKind(StrEnum):
    SUPPORT_NEW_TICKET = "support.new_ticket"
    INVOICE_EXPIRED = "invoice.expired"
    SUPPORT_REPLY = "support.reply"
    SUPPORT_CLIENT_MESSAGE = "support.client_message"
    INVOICE_PAID = "invoice.paid"
    INVOICE_SHIPPED = "invoice.shipped"
