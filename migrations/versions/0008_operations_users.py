"""Add customer identity metadata and explicit source-card requirement."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("last_activity_at", sa.DateTime(timezone=True)))
    op.create_index("ix_users_username", "users", ["username"])
    op.add_column(
        "products",
        sa.Column(
            "requires_verified_source_card",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "premium_emojis",
        sa.Column("fallback", sa.String(16), nullable=False, server_default="•"),
    )


def downgrade() -> None:
    op.drop_column("premium_emojis", "fallback")
    op.drop_column("products", "requires_verified_source_card")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_column("users", "last_activity_at")
    op.drop_column("users", "display_name")
    op.drop_column("users", "username")
