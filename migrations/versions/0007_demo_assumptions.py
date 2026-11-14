"""Explicit synthetic assumptions attached to real events without changing source facts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_demo_assumptions"
down_revision = "0006_agent_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_assumptions",
        sa.Column(
            "event_id", sa.UUID(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("values", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("event_assumptions")
