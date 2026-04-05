"""Add test_cases catalog table.

Revision ID: d4f09a7b3e12
Revises: c1a5e7e57f1a
Create Date: 2026-04-05 00:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "d4f09a7b3e12"
down_revision: str | None = "c1a5e7e57f1a"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Run the upgrade migrations."""
    op.create_table(
        "test_cases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("camera_identifier", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("polarity", sa.String(), nullable=False),
        sa.Column(
            "expected",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("video_path", sa.String(), nullable=False),
        sa.Column("duration", sa.Integer(), nullable=False, server_default="15"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("TIMEZONE('utc', CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "camera_identifier",
            "kind",
            "polarity",
            "slug",
            name="uq_test_cases_camera_kind_polarity_slug",
        ),
    )
    op.create_index(
        "idx_test_cases_camera", "test_cases", ["camera_identifier"], unique=False
    )


def downgrade() -> None:
    """Run the downgrade migrations."""
    op.drop_index("idx_test_cases_camera", table_name="test_cases")
    op.drop_table("test_cases")
