"""Drop detection_metrics column from recordings.

Per-frame motion and object data lives in the event_frames table now,
so the legacy detection_metrics JSONB column on recordings is no longer
read or written. Drop it to keep fresh installs and existing installs
on the same schema.

Revision ID: c2e8a4f1b3d6
Revises: b5e9f2a1c3d7
Create Date: 2026-04-10 00:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str | None = "c2e8a4f1b3d6"
down_revision: str | None = "b5e9f2a1c3d7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Run the upgrade migrations."""
    op.drop_column("recordings", "detection_metrics")


def downgrade() -> None:
    """Run the downgrade migrations."""
    op.add_column(
        "recordings",
        sa.Column("detection_metrics", JSONB, nullable=True),
    )
