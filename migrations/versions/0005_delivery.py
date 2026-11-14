"""Persistent Telegram inbox/outbox and delivery leases."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_delivery"
down_revision = "0004_places"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("lease_id", sa.String(64)))
    op.create_table(
        "bot_state",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "bot_updates",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("replies", postgresql.JSONB(), nullable=False),
        sa.Column("next_reply", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bot_updates_status", "bot_updates", ["status"])


def downgrade() -> None:
    op.drop_table("bot_updates")
    op.drop_table("bot_state")
    op.drop_column("notifications", "lease_id")
