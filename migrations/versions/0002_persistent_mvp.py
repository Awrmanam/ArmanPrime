"""Add persistent MVP entities and commercial fields."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    for _name, column in (
        ("description", sa.Column("description", sa.Text())),
        ("custom_emoji_id", sa.Column("custom_emoji_id", sa.Text())),
    ):
        op.add_column("categories", column)
    additions = (
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("fixed_price_toman", sa.BigInteger()),
        sa.Column("duration", sa.Text()),
        sa.Column("plan_type", sa.Text()),
        sa.Column("activation_method", sa.Text()),
        sa.Column("warranty_text", sa.Text()),
        sa.Column("warranty_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivery_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unlimited_stock", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("custom_emoji_id", sa.Text()),
        sa.Column("pricing_override", postgresql.JSONB()),
    )
    for column in additions:
        op.add_column("products", column)
    op.add_column(
        "customer_cards",
        sa.Column("encrypted_pan", sa.Text(), nullable=False, server_default="MIGRATION_REQUIRED"),
    )
    op.add_column(
        "customer_cards",
        sa.Column(
            "evidence_file_id", sa.Text(), nullable=False, server_default="MIGRATION_REQUIRED"
        ),
    )
    op.add_column("customer_cards", sa.Column("verified_at", sa.DateTime(timezone=True)))
    op.add_column("customer_cards", sa.Column("verified_by", sa.BigInteger()))
    op.add_column(
        "price_quotes",
        sa.Column("final_check_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "kyc_submissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("file_id", sa.Text(), nullable=False),
        sa.Column("file_unique_id", sa.Text(), nullable=False, unique=True),
        sa.Column("file_type", sa.String(16), nullable=False),
        sa.Column("evidence_level", sa.String(32), nullable=False),
        sa.Column("reviewer_id", sa.BigInteger()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_kyc_user", "kyc_submissions", ["user_id", "status"])
    op.create_table(
        "configuration",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "premium_emojis",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("custom_emoji_id", sa.Text(), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "pages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_table(
        "page_buttons",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "page_id", sa.Uuid(), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("row", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("style", sa.String(16), nullable=False),
        sa.Column("custom_emoji_id", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "currency_rates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("usd_to_toman", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rates_created", "currency_rates", ["created_at"])
    op.create_table(
        "inventory_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "quote_id", sa.Uuid(), sa.ForeignKey("price_quotes.id"), unique=True, nullable=False
        ),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
    )
    op.add_column("payments", sa.Column("receipt_file_id", sa.Text()))
    op.add_column("payments", sa.Column("receipt_unique_id", sa.Text(), unique=True))
    op.add_column("payments", sa.Column("receipt_type", sa.String(16)))
    op.add_column("payments", sa.Column("submitted_at", sa.DateTime(timezone=True)))
    op.create_table(
        "deliveries",
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("orders.id"), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("activation_link", sa.Text()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_outbox_pending", "notification_outbox", ["sent_at", "available_at"])


def downgrade() -> None:
    for table in (
        "notification_outbox",
        "deliveries",
        "inventory_reservations",
        "currency_rates",
        "page_buttons",
        "pages",
        "premium_emojis",
        "configuration",
        "kyc_submissions",
    ):
        op.drop_table(table)
    for name in ("submitted_at", "receipt_type", "receipt_unique_id", "receipt_file_id"):
        op.drop_column("payments", name)
    op.drop_column("price_quotes", "final_check_confirmed")
    for name in ("verified_by", "verified_at", "evidence_file_id", "encrypted_pan"):
        op.drop_column("customer_cards", name)
    for name in (
        "pricing_override",
        "custom_emoji_id",
        "position",
        "unlimited_stock",
        "delivery_minutes",
        "warranty_days",
        "warranty_text",
        "activation_method",
        "plan_type",
        "duration",
        "fixed_price_toman",
        "description",
    ):
        op.drop_column("products", name)
    op.drop_column("categories", "custom_emoji_id")
    op.drop_column("categories", "description")
