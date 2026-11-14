"""Identity, subscriptions, agent traces, confirmations and quotas."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003_runtime"
down_revision = "0002_sources"
branch_labels = None
depends_on = None


def ident() -> sa.Column:
    return sa.Column("id", sa.Uuid(), primary_key=True)


def owner() -> sa.Column:
    return sa.Column("owner_id", sa.String(80), nullable=False)


def moment(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "web_sessions",
        ident(),
        owner(),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("role", sa.String(20), nullable=False),
        moment("expires_at"),
        moment("created_at"),
    )
    op.create_index("ix_web_sessions_owner_id", "web_sessions", ["owner_id"])
    op.create_table(
        "telegram_identities",
        sa.Column("telegram_user_id", sa.String(30), primary_key=True),
        owner(),
        sa.Column("chat_id", sa.String(30), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_telegram_identities_owner_id", "telegram_identities", ["owner_id"])
    op.create_table(
        "link_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        owner(),
        moment("expires_at"),
    )
    op.create_table(
        "subscriptions",
        ident(),
        owner(),
        sa.Column("city_id", sa.Uuid(), sa.ForeignKey("cities.id"), nullable=False),
        sa.Column("category", sa.String(30)),
        sa.Column("period_filter", pg.JSONB(), nullable=False),
        sa.Column("filter_hash", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        moment("created_at"),
        sa.UniqueConstraint("owner_id", "filter_hash", "channel"),
    )
    op.create_index("ix_subscriptions_owner_id", "subscriptions", ["owner_id"])
    op.create_table(
        "notifications",
        ident(),
        sa.Column(
            "subscription_id",
            sa.Uuid(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        moment("available_at"),
        moment("lease_until", True),
        moment("sent_at", True),
        moment("created_at"),
        sa.UniqueConstraint("subscription_id", "event_id"),
    )
    op.create_index("ix_notifications_due", "notifications", ["status", "available_at"])
    op.create_table(
        "feedback",
        ident(),
        owner(),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id")),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        moment("created_at"),
    )
    op.create_table(
        "conversations",
        ident(),
        owner(),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("state", pg.JSONB(), nullable=False),
        sa.Column("lease_id", sa.String(64)),
        moment("lease_until", True),
        sa.Column("total_cost", sa.Numeric(14, 6), nullable=False),
        moment("created_at"),
        moment("updated_at"),
    )
    op.create_index("ix_conversations_owner_id", "conversations", ["owner_id"])
    op.create_table(
        "dialogue_turns",
        ident(),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_key", sa.String(100), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("user_text", sa.Text(), nullable=False),
        sa.Column("answer", pg.JSONB()),
        moment("reference_time"),
        moment("created_at"),
        sa.UniqueConstraint("conversation_id", "client_key"),
    )
    op.create_table(
        "agent_traces",
        ident(),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            sa.Uuid(),
            sa.ForeignKey("dialogue_turns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("query_redacted", sa.Text(), nullable=False),
        sa.Column("plan", pg.JSONB(), nullable=False),
        sa.Column("calls", pg.JSONB(), nullable=False),
        sa.Column("evidence", pg.JSONB(), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("model_id", sa.Text()),
        sa.Column("prompt_version", sa.String(30), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_rub", sa.Numeric(14, 6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(50)),
        moment("created_at"),
    )
    op.create_table(
        "pending_actions",
        ident(),
        owner(),
        sa.Column("action_type", sa.String(30), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", pg.JSONB()),
        moment("expires_at"),
    )
    op.create_index("ix_pending_actions_owner_id", "pending_actions", ["owner_id"])
    op.create_table(
        "budget_accounts",
        ident(),
        owner(),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("spent", sa.Numeric(14, 6), nullable=False),
        sa.Column("reserved", sa.Numeric(14, 6), nullable=False),
        sa.UniqueConstraint("owner_id", "day"),
    )
    op.create_table(
        "rate_buckets",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
        moment("expires_at"),
    )


def downgrade() -> None:
    for table in (
        "rate_buckets",
        "budget_accounts",
        "pending_actions",
        "agent_traces",
        "dialogue_turns",
        "conversations",
        "feedback",
        "notifications",
        "subscriptions",
        "link_tokens",
        "telegram_identities",
        "web_sessions",
    ):
        op.drop_table(table)
