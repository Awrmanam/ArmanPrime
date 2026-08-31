"""Add public identifiers and topic-aware review outbox metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _public_code(table: str, prefix: str, sequence: str) -> None:
    column = f"{table}_public_code"
    op.execute(f"CREATE SEQUENCE {sequence} START WITH 100001")
    op.add_column(
        table,
        sa.Column(
            "public_code",
            sa.String(20),
            nullable=True,
            server_default=sa.text(f"'{prefix}-' || nextval('{sequence}')::text"),
        ),
    )
    backfills = {
        "users": (
            "WITH n AS (SELECT id,row_number() OVER (ORDER BY id)+100000 v FROM users) "
            "UPDATE users t SET public_code='CUS-'||n.v FROM n WHERE t.id=n.id"
        ),
        "kyc_submissions": (
            "WITH n AS (SELECT id,row_number() OVER (ORDER BY id)+100000 v "
            "FROM kyc_submissions) UPDATE kyc_submissions t "
            "SET public_code='KYC-'||n.v FROM n WHERE t.id=n.id"
        ),
        "customer_cards": (
            "WITH n AS (SELECT id,row_number() OVER (ORDER BY id)+100000 v "
            "FROM customer_cards) UPDATE customer_cards t "
            "SET public_code='CRD-'||n.v FROM n WHERE t.id=n.id"
        ),
        "orders": (
            "WITH n AS (SELECT id,row_number() OVER (ORDER BY id)+100000 v FROM orders) "
            "UPDATE orders t SET public_code='ORD-'||n.v FROM n WHERE t.id=n.id"
        ),
    }
    sequence_positions = {
        "users": (
            "SELECT setval('customer_public_seq',"
            "GREATEST((SELECT count(*)+100000 FROM users),100000))"
        ),
        "kyc_submissions": (
            "SELECT setval('kyc_public_seq',"
            "GREATEST((SELECT count(*)+100000 FROM kyc_submissions),100000))"
        ),
        "customer_cards": (
            "SELECT setval('card_public_seq',"
            "GREATEST((SELECT count(*)+100000 FROM customer_cards),100000))"
        ),
        "orders": (
            "SELECT setval('order_public_seq',"
            "GREATEST((SELECT count(*)+100000 FROM orders),100000))"
        ),
    }
    op.execute(backfills[table])
    op.execute(sequence_positions[table])
    op.alter_column(table, "public_code", nullable=False)
    op.create_index(column, table, ["public_code"], unique=True)


def upgrade() -> None:
    for table, prefix, sequence in (
        ("users", "CUS", "customer_public_seq"),
        ("kyc_submissions", "KYC", "kyc_public_seq"),
        ("customer_cards", "CRD", "card_public_seq"),
        ("orders", "ORD", "order_public_seq"),
    ):
        _public_code(table, prefix, sequence)
    op.add_column("users", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE users SET created_at = now() WHERE created_at IS NULL")
    op.alter_column("users", "created_at", nullable=False)
    op.add_column("customer_cards", sa.Column("evidence_unique_id", sa.Text(), nullable=True))
    op.execute("UPDATE customer_cards SET evidence_unique_id = 'legacy:' || id")
    op.alter_column("customer_cards", "evidence_unique_id", nullable=False)
    op.create_unique_constraint(
        "uq_customer_cards_evidence_unique_id", "customer_cards", ["evidence_unique_id"]
    )
    op.add_column(
        "customer_cards",
        sa.Column("evidence_type", sa.String(16), nullable=False, server_default="document"),
    )
    op.add_column("customer_cards", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column(
        "customer_cards",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column("notification_outbox", sa.Column("message_thread_id", sa.Integer()))
    op.add_column("notification_outbox", sa.Column("entity_type", sa.String(32)))
    op.add_column("notification_outbox", sa.Column("entity_id", sa.Uuid()))
    op.add_column("notification_outbox", sa.Column("event_key", sa.String(160), nullable=True))
    op.execute("UPDATE notification_outbox SET event_key = 'legacy:' || id")
    op.alter_column("notification_outbox", "event_key", nullable=False)
    op.create_index("ix_notification_outbox_entity_type", "notification_outbox", ["entity_type"])
    op.create_index("ix_notification_outbox_entity_id", "notification_outbox", ["entity_id"])
    op.create_unique_constraint(
        "uq_notification_outbox_event_key", "notification_outbox", ["event_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_notification_outbox_event_key", "notification_outbox", type_="unique")
    op.drop_index("ix_notification_outbox_entity_id", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_entity_type", table_name="notification_outbox")
    for name in ("event_key", "entity_id", "entity_type", "message_thread_id"):
        op.drop_column("notification_outbox", name)
    op.drop_column("customer_cards", "created_at")
    op.drop_column("customer_cards", "reason")
    op.drop_column("customer_cards", "evidence_type")
    op.drop_constraint("uq_customer_cards_evidence_unique_id", "customer_cards", type_="unique")
    op.drop_column("customer_cards", "evidence_unique_id")
    op.drop_column("users", "created_at")
    for table in ("orders", "customer_cards", "kyc_submissions", "users"):
        op.drop_index(f"{table}_public_code", table_name=table)
        op.drop_column(table, "public_code")
    for sequence in (
        "order_public_seq",
        "card_public_seq",
        "kyc_public_seq",
        "customer_public_seq",
    ):
        op.execute(f"DROP SEQUENCE {sequence}")
