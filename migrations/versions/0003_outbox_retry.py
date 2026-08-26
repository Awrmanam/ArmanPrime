"""Add durable outbox retry and dead-letter metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.add_column("notification_outbox", sa.Column("last_error", sa.Text()))
    op.add_column("notification_outbox", sa.Column("dead_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_outbox_deliverable", "notification_outbox", ["sent_at", "dead_at", "available_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_deliverable", table_name="notification_outbox")
    op.drop_column("notification_outbox", "dead_at")
    op.drop_column("notification_outbox", "last_error")
