"""Add product families, variants, dynamic checkout fields and supplier offers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "price_quotes",
        "selected_card_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )

    op.create_table(
        "product_families",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("button_emoji_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_product_families_category_active",
        "product_families",
        ["category_id", "active", "position"],
    )

    op.create_table(
        "product_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "family_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("product_families.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "legacy_product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("activation_method", sa.String(48), nullable=False),
        sa.Column("fulfillment_type", sa.String(48), nullable=False),
        sa.Column("payment_method", sa.String(32), nullable=False, server_default="card_to_card"),
        sa.Column("delivery_type", sa.String(24), nullable=False, server_default="instant"),
        sa.Column("delivery_min", sa.Integer(), nullable=True),
        sa.Column("delivery_max", sa.Integer(), nullable=True),
        sa.Column("delivery_unit", sa.String(16), nullable=True),
        sa.Column("delivery_text", sa.Text(), nullable=True),
        sa.Column("warranty_type", sa.String(24), nullable=False, server_default="none"),
        sa.Column("warranty_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("warranty_text", sa.Text(), nullable=True),
        sa.Column("requires_kyc", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "requires_verified_source_card",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("button_emoji_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_product_variants_family_active",
        "product_variants",
        ["family_id", "active", "position"],
    )

    op.create_table(
        "checkout_fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("product_variants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_key", sa.String(64), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("field_type", sa.String(32), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("help_text", sa.Text(), nullable=True),
        sa.Column("options", postgresql.JSONB(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "delete_after_fulfillment",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.UniqueConstraint("variant_id", "field_key", name="uq_checkout_field_variant_key"),
    )
    op.create_index(
        "ix_checkout_fields_variant_position",
        "checkout_fields",
        ["variant_id", "position"],
    )

    op.create_table(
        "suppliers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("marketplace", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("marketplace", "name", name="uq_supplier_marketplace_name"),
    )

    op.create_table(
        "supplier_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("product_variants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supplier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("suppliers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("supplier_url", sa.Text(), nullable=True),
        sa.Column("cost_amount", sa.Numeric(24, 8), nullable=False),
        sa.Column("cost_currency", sa.String(3), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("delivery_mode", sa.String(24), nullable=False, server_default="manual"),
        sa.Column("warranty_text", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_supplier_offers_variant_active_priority",
        "supplier_offers",
        ["variant_id", "active", "priority"],
    )

    op.create_table(
        "variant_checkout_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("product_variants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "quote_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("price_quotes.id", ondelete="SET NULL"),
            nullable=True,
            unique=True,
        ),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
            unique=True,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_variant_checkout_user_status",
        "variant_checkout_sessions",
        ["user_id", "status", "expires_at"],
    )

    op.create_table(
        "variant_checkout_values",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "checkout_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("variant_checkout_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "field_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("checkout_fields.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_ciphertext", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "checkout_session_id",
            "field_id",
            name="uq_variant_checkout_value_session_field",
        ),
    )


def downgrade() -> None:
    op.drop_table("variant_checkout_values")
    op.drop_index("ix_variant_checkout_user_status", table_name="variant_checkout_sessions")
    op.drop_table("variant_checkout_sessions")
    op.drop_index("ix_supplier_offers_variant_active_priority", table_name="supplier_offers")
    op.drop_table("supplier_offers")
    op.drop_table("suppliers")
    op.drop_index("ix_checkout_fields_variant_position", table_name="checkout_fields")
    op.drop_table("checkout_fields")
    op.drop_index("ix_product_variants_family_active", table_name="product_variants")
    op.drop_table("product_variants")
    op.drop_index("ix_product_families_category_active", table_name="product_families")
    op.drop_table("product_families")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM price_quotes WHERE selected_card_id IS NULL
            ) THEN
                ALTER TABLE price_quotes
                ALTER COLUMN selected_card_id SET NOT NULL;
            END IF;
        END $$;
        """
    )
