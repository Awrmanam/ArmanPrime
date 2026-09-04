"""Add durable soft-delete markers for product families and variants."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_archives",
        sa.Column("entity_type", sa.String(16), primary_key=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_by", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_catalog_archives_type_time",
        "catalog_archives",
        ["entity_type", "archived_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_catalog_archives_type_time", table_name="catalog_archives")
    op.drop_table("catalog_archives")
