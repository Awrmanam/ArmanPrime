"""Persist quote successors and merchant-card allocation."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.add_column("price_quotes", sa.Column("predecessor_quote_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_quote_predecessor", "price_quotes", "price_quotes", ["predecessor_quote_id"], ["id"]
    )
    op.create_unique_constraint("uq_quote_predecessor", "price_quotes", ["predecessor_quote_id"])
    op.add_column("orders", sa.Column("merchant_card_id", sa.Uuid()))
    op.add_column(
        "orders",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_foreign_key(
        "fk_order_merchant_card", "orders", "merchant_cards", ["merchant_card_id"], ["id"]
    )
    op.create_index("ix_orders_merchant_created", "orders", ["merchant_card_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_orders_merchant_created", table_name="orders")
    op.drop_constraint("fk_order_merchant_card", "orders", type_="foreignkey")
    op.drop_column("orders", "created_at")
    op.drop_column("orders", "merchant_card_id")
    op.drop_constraint("uq_quote_predecessor", "price_quotes", type_="unique")
    op.drop_constraint("fk_quote_predecessor", "price_quotes", type_="foreignkey")
    op.drop_column("price_quotes", "predecessor_quote_id")
