import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from b2b_commerce.db import Base


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_payment', 'paid', 'shipped', 'completed', 'expired', 'canceled')",
            name="ck_invoices_status",
        ),
        CheckConstraint(
            "status <> 'awaiting_payment' OR expires_at IS NOT NULL",
            name="ck_invoices_awaiting_has_expires",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id"), nullable=False
    )
    number: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    payment_instructions: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    create_idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    seller_name: Mapped[str | None] = mapped_column(Text)
    seller_legal_name: Mapped[str | None] = mapped_column(Text)
    seller_inn: Mapped[str | None] = mapped_column(Text)
    seller_kpp: Mapped[str | None] = mapped_column(Text)
    seller_legal_address: Mapped[str | None] = mapped_column(Text)
    seller_bank_name: Mapped[str | None] = mapped_column(Text)
    seller_bik: Mapped[str | None] = mapped_column(Text)
    seller_bank_account: Mapped[str | None] = mapped_column(Text)
    seller_corr_account: Mapped[str | None] = mapped_column(Text)
    buyer_name: Mapped[str | None] = mapped_column(Text)
    buyer_legal_name: Mapped[str | None] = mapped_column(Text)
    buyer_inn: Mapped[str | None] = mapped_column(Text)
    buyer_kpp: Mapped[str | None] = mapped_column(Text)
    buyer_legal_address: Mapped[str | None] = mapped_column(Text)
    buyer_contact_phone: Mapped[str | None] = mapped_column(Text)
    buyer_contact_email: Mapped[str | None] = mapped_column(Text)
    recipient_address_snapshot: Mapped[str | None] = mapped_column(Text)


class InvoiceItem(Base):
    __tablename__ = "invoice_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_invoice_items_qty"),
        Index("ix_invoice_items_invoice_id", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("invoices.id"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("warehouses.id"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    cost_price_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    product_name_snapshot: Mapped[str] = mapped_column(Text, nullable=False)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
