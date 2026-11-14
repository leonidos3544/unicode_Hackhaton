"""Verified source snapshots, collection runs and venue provenance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002_sources"
down_revision = "0001_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parser_version", sa.String(30), nullable=False),
    )
    op.create_table(
        "source_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("report", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("venues", sa.Column("source_url", sa.Text()))
    op.add_column("venues", sa.Column("verified_at", sa.DateTime(timezone=True)))
    op.add_column("source_records", sa.Column("verification_status", sa.String(30)))
    op.add_column("source_records", sa.Column("last_checked_at", sa.DateTime(timezone=True)))
    op.add_column("source_records", sa.Column("listing_url", sa.Text()))
    for field in ("listing_snapshot_id", "detail_snapshot_id"):
        op.add_column("source_records", sa.Column(field, sa.Uuid()))
        op.create_foreign_key(
            f"fk_source_records_{field}_source_snapshots",
            "source_records",
            "source_snapshots",
            [field],
            ["id"],
        )


def downgrade() -> None:
    for field in ("detail_snapshot_id", "listing_snapshot_id"):
        op.drop_constraint(
            f"fk_source_records_{field}_source_snapshots", "source_records", type_="foreignkey"
        )
        op.drop_column("source_records", field)
    for field in ("listing_url", "last_checked_at", "verification_status"):
        op.drop_column("source_records", field)
    op.drop_column("venues", "verified_at")
    op.drop_column("venues", "source_url")
    op.drop_table("source_runs")
    op.drop_table("source_snapshots")
