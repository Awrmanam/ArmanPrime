"""Add supplier-cost currencies and normalized FX rate metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("base_cost_amount", sa.Numeric(24, 8), nullable=True))
    op.add_column(
        "products",
        sa.Column("base_cost_currency", sa.String(3), nullable=False, server_default="USD"),
    )
    op.add_column(
        "products",
        sa.Column("currency_buffer_percent", sa.Numeric(8, 4), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE products SET base_cost_amount = base_price_usd WHERE base_cost_amount IS NULL"
    )
    op.alter_column("products", "base_cost_amount", nullable=False)
    op.create_index("ix_products_base_cost_currency", "products", ["base_cost_currency"])

    columns = (
        sa.Column("currency_code", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("toman_per_unit", sa.Numeric(24, 8), nullable=True),
        sa.Column("provider_name", sa.String(64), nullable=False, server_default="manual"),
        sa.Column("provider_symbol", sa.String(64), nullable=False, server_default="usd"),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_fetch_status", sa.String(32), nullable=False, server_default="ok"),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("buffer_percent", sa.Numeric(8, 4), nullable=False, server_default="0"),
    )
    for column in columns:
        op.add_column("currency_rates", column)
    op.execute("""
        UPDATE currency_rates SET toman_per_unit = usd_to_toman,
          provider_timestamp = created_at, fetched_at = created_at,
          valid_until = created_at + INTERVAL '100 years'
    """)
    for name in ("toman_per_unit", "provider_timestamp", "fetched_at", "valid_until"):
        op.alter_column("currency_rates", name, nullable=False)
    op.create_index("ix_currency_rates_currency_code", "currency_rates", ["currency_code"])
    op.create_index("ix_currency_rates_fetched_at", "currency_rates", ["fetched_at"])
    op.create_index("ix_currency_rates_valid_until", "currency_rates", ["valid_until"])
    op.create_index("ix_currency_rates_active", "currency_rates", ["active"])


def downgrade() -> None:
    for name in ("active", "valid_until", "fetched_at", "currency_code"):
        op.drop_index(f"ix_currency_rates_{name}", table_name="currency_rates")
    for name in (
        "buffer_percent",
        "version",
        "last_error_code",
        "last_fetch_status",
        "active",
        "valid_until",
        "fetched_at",
        "provider_timestamp",
        "provider_symbol",
        "provider_name",
        "toman_per_unit",
        "currency_code",
    ):
        op.drop_column("currency_rates", name)
    op.drop_index("ix_products_base_cost_currency", table_name="products")
    op.drop_column("products", "currency_buffer_percent")
    op.drop_column("products", "base_cost_currency")
    op.drop_column("products", "base_cost_amount")
