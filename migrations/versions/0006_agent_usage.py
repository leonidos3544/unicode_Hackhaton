"""Distinguish measured model costs from conservative reservations."""

import sqlalchemy as sa
from alembic import op

revision = "0006_agent_usage"
down_revision = "0005_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_traces",
        sa.Column("cost_estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "agent_traces",
        sa.Column("usage_known", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("agent_traces", "cost_estimated", server_default=None)
    op.alter_column("agent_traces", "usage_known", server_default=None)


def downgrade() -> None:
    op.drop_column("agent_traces", "usage_known")
    op.drop_column("agent_traces", "cost_estimated")
