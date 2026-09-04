import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from b2b_commerce.db import Base, TimestampMixin


class BillingEntity(TimestampMixin, Base):
    __tablename__ = "billing_entities"
    __table_args__ = (UniqueConstraint("inn", name="uq_billing_entities_inn"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    legal_name: Mapped[str] = mapped_column(Text, nullable=False)
    inn: Mapped[str] = mapped_column(Text, nullable=False)
    kpp: Mapped[str | None] = mapped_column(Text)
    legal_address: Mapped[str | None] = mapped_column(Text)
    bank_name: Mapped[str | None] = mapped_column(Text)
    bik: Mapped[str | None] = mapped_column(Text)
    bank_account: Mapped[str | None] = mapped_column(Text)
    corr_account: Mapped[str | None] = mapped_column(Text)


class Company(TimestampMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'active', 'rejected', 'suspended')",
            name="ck_companies_status",
        ),
        UniqueConstraint("inn", name="uq_companies_inn"),
        UniqueConstraint("contact_email", name="uq_companies_contact_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    legal_name: Mapped[str | None] = mapped_column(Text)
    inn: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    contact_phone: Mapped[str | None] = mapped_column(Text)
    legal_address: Mapped[str | None] = mapped_column(Text)
    contact_person: Mapped[str | None] = mapped_column(Text)
    kpp: Mapped[str | None] = mapped_column(Text)
    delivery_address: Mapped[str | None] = mapped_column(Text)
    delivery_contact: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    billing_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("billing_entities.id"), index=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))


class CompanyAccount(TimestampMixin, Base):
    __tablename__ = "company_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id"), unique=True, nullable=False
    )
    login: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true")
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
