"""First catalog schema. All timestamps are exact instants; prices are kopecks."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.common.clock import utcnow


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class City(Base):
    __tablename__ = "cities"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text, unique=True)


class Venue(Base):
    __tablename__ = "venues"
    __table_args__ = (UniqueConstraint("city_id", "normalized_name", "address"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    city_id: Mapped[UUID] = mapped_column(ForeignKey("cities.id"))
    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("status IN ('draft','published','cancelled')", name="status"),
        CheckConstraint("price_min_minor IS NULL OR price_min_minor >= 0", name="price_min"),
        CheckConstraint(
            "price_max_minor IS NULL OR (price_max_minor >= 0 AND "
            "(price_min_minor IS NULL OR price_max_minor >= price_min_minor))",
            name="price_max",
        ),
        CheckConstraint(
            "is_free IS NOT TRUE OR (price_min_minor = 0 AND price_max_minor = 0 "
            "AND price_min_minor IS NOT NULL AND price_max_minor IS NOT NULL)",
            name="free",
        ),
        CheckConstraint("date_end IS NULL OR date_end > date_start", name="dates"),
        CheckConstraint("age_limit IS NULL OR age_limit IN (0,6,12,16,18)", name="age"),
        CheckConstraint("length(trim(title)) > 0", name="title"),
        Index("ix_events_city_date", "city_id", "date_start", "id"),
        Index("ix_events_status_date", "status", "date_start"),
        Index("ix_events_dedup_key", "dedup_key"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(Text)
    normalized_title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(30))
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    date_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    date_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    city_id: Mapped[UUID] = mapped_column(ForeignKey("cities.id"))
    venue_id: Mapped[UUID] = mapped_column(ForeignKey("venues.id"))
    address: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    price_min_minor: Mapped[int | None] = mapped_column(Integer)
    price_max_minor: Mapped[int | None] = mapped_column(Integer)
    is_free: Mapped[bool | None]
    age_limit: Mapped[int | None]
    language: Mapped[str | None] = mapped_column(Text)
    org_link: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    dedup_key: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(default=1)
    merged_into_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceRecord(Base):
    __tablename__ = "source_records"
    __table_args__ = (UniqueConstraint("source_name", "external_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_name: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"))
    source_type: Mapped[str] = mapped_column(String(20), default="import")
    latest_hash: Mapped[str | None] = mapped_column(String(64))
    applied_hash: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_status: Mapped[str | None] = mapped_column(String(30))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listing_url: Mapped[str | None] = mapped_column(Text)
    listing_snapshot_id: Mapped[UUID | None] = mapped_column(ForeignKey("source_snapshots.id"))
    detail_snapshot_id: Mapped[UUID | None] = mapped_column(ForeignKey("source_snapshots.id"))


class RawRecord(Base):
    __tablename__ = "raw_records"
    __table_args__ = (UniqueConstraint("source_record_id", "content_hash"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_record_id: Mapped[UUID | None] = mapped_column(ForeignKey("source_records.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    normalization_version: Mapped[str] = mapped_column(String(30), default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImportRun(Base):
    __tablename__ = "import_runs"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_name: Mapped[str] = mapped_column(Text)
    file_hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(50))
    counts: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImportRow(Base):
    __tablename__ = "import_rows"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    import_run_id: Mapped[UUID] = mapped_column(ForeignKey("import_runs.id"))
    row_number: Mapped[int]
    raw_record_id: Mapped[UUID] = mapped_column(ForeignKey("raw_records.id"))
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"))
    result: Mapped[str] = mapped_column(String(20))
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)


class ModerationItem(Base):
    __tablename__ = "moderation_items"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    raw_record_id: Mapped[UUID] = mapped_column(ForeignKey("raw_records.id"), unique=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(50))
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    changes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_name: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int]
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parser_version: Mapped[str] = mapped_column(String(30), default="official-html-v2")


class SourceRun(Base):
    __tablename__ = "source_runs"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    report: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
