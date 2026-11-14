"""Persistent identity, dialogue and delivery state. No external provider is required to start."""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.clock import utcnow
from app.storage.postgres.tables import Base


class WebSession(Base):
    __tablename__ = "web_sessions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(20), default="resident")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelegramIdentity(Base):
    __tablename__ = "telegram_identities"
    telegram_user_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    chat_id: Mapped[str] = mapped_column(String(30))
    enabled: Mapped[bool] = mapped_column(default=True)


class LinkToken(Base):
    __tablename__ = "link_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("owner_id", "filter_hash", "channel"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    city_id: Mapped[UUID] = mapped_column(ForeignKey("cities.id"))
    category: Mapped[str | None] = mapped_column(String(30))
    period_filter: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    filter_hash: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(20), default="inbox")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("subscription_id", "event_id"),
        Index("ix_notifications_due", "status", "available_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subscription_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE")
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(80))
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"))
    kind: Mapped[str] = mapped_column(String(30))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    channel: Mapped[str] = mapped_column(String(20), default="web")
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    lease_id: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_cost: Mapped[float] = mapped_column(Numeric(14, 6, asdecimal=False), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DialogueTurn(Base):
    __tablename__ = "dialogue_turns"
    __table_args__ = (UniqueConstraint("conversation_id", "client_key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    client_key: Mapped[str] = mapped_column(String(100))
    input_hash: Mapped[str] = mapped_column(String(64))
    user_text: Mapped[str] = mapped_column(Text)
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reference_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentTrace(Base):
    __tablename__ = "agent_traces"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    turn_id: Mapped[UUID] = mapped_column(ForeignKey("dialogue_turns.id", ondelete="CASCADE"))
    query_redacted: Mapped[str] = mapped_column(Text)
    plan: Mapped[list[str]] = mapped_column(JSONB, default=list)
    calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    mode: Mapped[str] = mapped_column(String(20))
    model_id: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(String(30), default="afisha-v1")
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_rub: Mapped[float] = mapped_column(Numeric(14, 6, asdecimal=False), default=0)
    cost_estimated: Mapped[bool] = mapped_column(default=False)
    usage_known: Mapped[bool] = mapped_column(default=True)
    latency_ms: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PendingAction(Base):
    __tablename__ = "pending_actions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    action_type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BudgetAccount(Base):
    __tablename__ = "budget_accounts"
    __table_args__ = (UniqueConstraint("owner_id", "day"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(80))
    day: Mapped[date] = mapped_column(Date)
    spent: Mapped[float] = mapped_column(Numeric(14, 6, asdecimal=False), default=0)
    reserved: Mapped[float] = mapped_column(Numeric(14, 6, asdecimal=False), default=0)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    count: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BotState(Base):
    __tablename__ = "bot_state"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class EventAssumption(Base):
    __tablename__ = "event_assumptions"
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    event_version: Mapped[int] = mapped_column()
    values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BotUpdate(Base):
    __tablename__ = "bot_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    replies: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    next_reply: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
