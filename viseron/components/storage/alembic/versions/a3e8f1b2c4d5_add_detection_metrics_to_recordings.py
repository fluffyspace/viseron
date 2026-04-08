"""Add detection_metrics to recordings.

Revision ID: a3e8f1b2c4d5
Revises: d4f09a7b3e12
Create Date: 2026-04-08 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str | None = "a3e8f1b2c4d5"
down_revision: str | None = "d4f09a7b3e12"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Run the upgrade migrations."""
    op.add_column("recordings", sa.Column("detection_metrics", JSONB, nullable=True))


def downgrade() -> None:
    """Run the downgrade migrations."""
    op.drop_column("recordings", "detection_metrics")
