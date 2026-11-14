"""Venue coordinates and permanent aliases for merged events."""

import sqlalchemy as sa
from alembic import op

revision = "0004_places"
down_revision = "0003_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("venues", sa.Column("lat", sa.Float()))
    op.add_column("venues", sa.Column("lon", sa.Float()))
    op.add_column("events", sa.Column("merged_into_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_events_merged_into_id_events", "events", "events", ["merged_into_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_events_merged_into_id_events", "events", type_="foreignkey")
    op.drop_column("events", "merged_into_id")
    op.drop_column("venues", "lon")
    op.drop_column("venues", "lat")
