"""Add test flag to event tables and create test_runs / test_results tables.

Revision ID: c1a5e7e57f1a
Revises: 7f6d3739fcd6
Create Date: 2026-04-05 00:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "c1a5e7e57f1a"
down_revision: str | None = "7f6d3739fcd6"
branch_labels: str | None = None
depends_on: str | None = None


_FLAGGED_TABLES = ("motion", "objects", "recordings", "events")


def upgrade() -> None:
    """Run the upgrade migrations."""
    for table in _FLAGGED_TABLES:
        op.add_column(
            table,
            sa.Column(
                "test",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        op.create_index(
            f"idx_{table}_test",
            table,
            ["test"],
            unique=False,
        )

    op.create_table(
        "test_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(),
            server_default=sa.text("TIMEZONE('utc', CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("TIMEZONE('utc', CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "test_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("case_name", sa.String(), nullable=False),
        sa.Column("camera_identifier", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "expected",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "actual",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("video_path", sa.String(), nullable=True),
        sa.Column("snapshot_path", sa.String(), nullable=True),
        sa.Column("message", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("TIMEZONE('utc', CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["test_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_test_results_run_id",
        "test_results",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    """Run the downgrade migrations."""
    op.drop_index("idx_test_results_run_id", table_name="test_results")
    op.drop_table("test_results")
    op.drop_table("test_runs")

    for table in _FLAGGED_TABLES:
        op.drop_index(f"idx_{table}_test", table_name=table)
        op.drop_column(table, "test")
