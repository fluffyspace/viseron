"""Add event_frames table.

Revision ID: b5e9f2a1c3d7
Revises: a3e8f1b2c4d5
Create Date: 2026-04-09 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str | None = "b5e9f2a1c3d7"
down_revision: str | None = "a3e8f1b2c4d5"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Run the upgrade migrations."""
    op.create_table(
        "event_frames",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recording_id", sa.Integer(), nullable=False),
        sa.Column("frame_offset_ms", sa.Integer(), nullable=False),
        sa.Column("motion_area", sa.Float(), nullable=True),
        sa.Column("objects", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("TIMEZONE('utc', CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["recording_id"], ["recordings.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "idx_event_frames_recording", "event_frames", ["recording_id"]
    )


def downgrade() -> None:
    """Run the downgrade migrations."""
    op.drop_index("idx_event_frames_recording", table_name="event_frames")
    op.drop_table("event_frames")
