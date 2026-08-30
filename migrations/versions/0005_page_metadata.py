"""Add owner-facing page metadata.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pages", sa.Column("title", sa.Text(), nullable=True))
    op.add_column(
        "pages", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.execute("UPDATE pages SET title = slug WHERE title IS NULL")
    op.alter_column("pages", "title", nullable=False)
    op.create_index("ix_pages_active", "pages", ["active"])


def downgrade() -> None:
    op.drop_index("ix_pages_active", table_name="pages")
    op.drop_column("pages", "active")
    op.drop_column("pages", "title")
