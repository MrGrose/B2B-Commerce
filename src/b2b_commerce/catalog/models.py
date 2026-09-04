import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from b2b_commerce.db import Base, TimestampMixin


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    margin_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))


class Brand(Base):
    __tablename__ = "brands"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_products_status"),
        CheckConstraint("cost_price >= 0", name="ck_products_cost_price"),
        CheckConstraint("sale_price >= 0", name="ck_products_sale_price"),
        Index("ix_products_search_tsv_gin", "search_tsv", postgresql_using="gin"),
        Index(
            "ix_products_deleted_at",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("brands.id"))
    brand_name: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("categories.id"))
    category: Mapped[Category | None] = relationship()
    description: Mapped[str | None] = mapped_column(Text)
    model_year: Mapped[int | None] = mapped_column(Integer)
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    sale_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    margin_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'inactive'"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            """
            setweight(to_tsvector('english', coalesce(name, '')), 'B')
            || setweight(to_tsvector('russian', coalesce(name, '')), 'B')
            || setweight(to_tsvector('english', coalesce(brand_name, '')), 'B')
            || setweight(to_tsvector('russian', coalesce(brand_name, '')), 'B')
            || setweight(to_tsvector('english', coalesce(model_year::text, '')), 'B')
            || setweight(to_tsvector('russian', coalesce(model_year::text, '')), 'B')
            || setweight(to_tsvector('english', coalesce(description, '')), 'C')
            || setweight(to_tsvector('russian', coalesce(description, '')), 'C')
            """,
            persisted=True,
        ),
    )
    images: Mapped[list["ProductImage"]] = relationship(
        back_populates="product",
        order_by="ProductImage.sort_order",
    )
    price_history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="product",
        order_by="PriceHistory.created_at",
    )


class ProductImage(Base):
    __tablename__ = "product_images"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    product: Mapped["Product"] = relationship(back_populates="images")


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id"), nullable=False
    )
    sale_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    product: Mapped["Product"] = relationship(back_populates="price_history")
