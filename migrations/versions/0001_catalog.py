"""Initial catalog, raw imports, moderation and audit.

Revision ID: 0001_catalog
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001_catalog"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False, unique=True),
    )
    op.create_table(
        "venues",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("city_id", sa.Uuid(), sa.ForeignKey("cities.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.UniqueConstraint("city_id", "normalized_name", "address"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("tags", pg.ARRAY(sa.Text()), nullable=False),
        sa.Column("date_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_end", sa.DateTime(timezone=True)),
        sa.Column("city_id", sa.Uuid(), sa.ForeignKey("cities.id"), nullable=False),
        sa.Column("venue_id", sa.Uuid(), sa.ForeignKey("venues.id"), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("price_min_minor", sa.Integer()),
        sa.Column("price_max_minor", sa.Integer()),
        sa.Column("is_free", sa.Boolean()),
        sa.Column("age_limit", sa.Integer()),
        sa.Column("language", sa.Text()),
        sa.Column("org_link", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('draft','published','cancelled')", name="status"),
        sa.CheckConstraint("price_min_minor IS NULL OR price_min_minor >= 0", name="price_min"),
        sa.CheckConstraint(
            "price_max_minor IS NULL OR (price_max_minor >= 0 AND "
            "(price_min_minor IS NULL OR price_max_minor >= price_min_minor))",
            name="price_max",
        ),
        sa.CheckConstraint(
            "is_free IS NOT TRUE OR (price_min_minor = 0 AND price_max_minor = 0 "
            "AND price_min_minor IS NOT NULL AND price_max_minor IS NOT NULL)",
            name="free",
        ),
        sa.CheckConstraint("date_end IS NULL OR date_end > date_start", name="dates"),
        sa.CheckConstraint("age_limit IS NULL OR age_limit IN (0,6,12,16,18)", name="age"),
        sa.CheckConstraint("length(trim(title)) > 0", name="title"),
    )
    op.create_index("ix_events_city_date", "events", ["city_id", "date_start", "id"])
    op.create_index("ix_events_status_date", "events", ["status", "date_start"])
    op.create_index("ix_events_dedup_key", "events", ["dedup_key"])
    op.create_table(
        "source_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id")),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("latest_hash", sa.String(64)),
        sa.Column("applied_hash", sa.String(64)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("source_name", "external_id"),
    )
    op.create_table(
        "raw_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_record_id", sa.Uuid(), sa.ForeignKey("source_records.id")),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("normalization_version", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_record_id", "content_hash"),
    )
    op.create_table(
        "import_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(50), nullable=False),
        sa.Column("counts", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "import_rows",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("import_run_id", sa.Uuid(), sa.ForeignKey("import_runs.id"), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw_record_id", sa.Uuid(), sa.ForeignKey("raw_records.id"), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id")),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("errors", pg.JSONB(), nullable=False),
    )
    op.create_table(
        "moderation_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "raw_record_id", sa.Uuid(), sa.ForeignKey("raw_records.id"), nullable=False, unique=True
        ),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor", sa.String(50), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("changes", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "audit_log",
        "moderation_items",
        "import_rows",
        "import_runs",
        "raw_records",
        "source_records",
        "events",
        "venues",
        "cities",
    ):
        op.drop_table(table)
