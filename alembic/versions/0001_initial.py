"""Initial schema."""

import sqlalchemy as sa
from alembic import op

from b2b_commerce.db import Base
from b2b_commerce.tables import load_models

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


# Создаёт актуальную ORM-схему и sequence номеров счетов.
def upgrade() -> None:
    load_models()
    Base.metadata.create_all(bind=op.get_bind())
    op.execute(
        sa.text(
            "CREATE SEQUENCE IF NOT EXISTS invoice_number_seq "
            "START WITH 1 INCREMENT BY 1"
        )
    )


# Удаляет все таблицы каркаса.
def downgrade() -> None:
    op.execute(sa.text("DROP SEQUENCE IF EXISTS invoice_number_seq"))
    load_models()
    Base.metadata.drop_all(bind=op.get_bind())
