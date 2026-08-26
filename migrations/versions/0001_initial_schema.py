"""Initial persistent commerce schema without seed data."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("kyc_status", sa.String(32), nullable=False, server_default="NOT_STARTED"),
        sa.Column("risk_status", sa.String(16), nullable=False, server_default="CLEAR"),
    )
    op.create_table(
        "terms_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("pages", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "consents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("terms_id", sa.Uuid(), sa.ForeignKey("terms_versions.id"), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "terms_id", name="uq_consent_user_terms"),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("category_id", sa.Uuid(), sa.ForeignKey("categories.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("base_price_usd", sa.Numeric(18, 4), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("requires_kyc", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "customer_cards",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("bank_name", sa.Text(), nullable=False),
        sa.Column("masked_pan", sa.String(32), nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
    )
    op.create_index("ix_customer_cards_owner_status", "customer_cards", ["user_id", "status"])
    op.create_table(
        "merchant_cards",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("bank_name", sa.Text(), nullable=False),
        sa.Column("holder_name", sa.Text(), nullable=False),
        sa.Column("encrypted_pan", sa.Text(), nullable=False),
        sa.Column("masked_pan", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("daily_limit", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "price_quotes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column(
            "selected_card_id", sa.Uuid(), sa.ForeignKey("customer_cards.id"), nullable=False
        ),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("rate", sa.BigInteger(), nullable=False),
        sa.Column("final_toman", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_quotes_expiry_status", "price_quotes", ["expires_at", "status"])
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "quote_id", sa.Uuid(), sa.ForeignKey("price_quotes.id"), nullable=False, unique=True
        ),
        sa.Column("amount_toman", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("assigned_admin_id", sa.BigInteger()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("orders.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_reference", sa.Text(), unique=True),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False, unique=True),
        sa.CheckConstraint("amount <> 0", name="ck_ledger_nonzero"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "audit_log",
        "ledger_entries",
        "payments",
        "orders",
        "price_quotes",
        "merchant_cards",
        "customer_cards",
        "products",
        "categories",
        "consents",
        "terms_versions",
        "users",
    ):
        op.drop_table(table)
