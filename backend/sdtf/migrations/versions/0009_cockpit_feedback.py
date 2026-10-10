"""migration cockpit upload simulation feedback per run

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-09 20:30:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cockpit_feedback",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(length=32), sa.ForeignKey("migration_runs.id"), nullable=False),
        sa.Column("object_type", sa.String(length=48), nullable=False, server_default=""),
        sa.Column("migration_object", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("instance_key", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("matched_key", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("matched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("severity", sa.String(length=1), nullable=False, server_default="I"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("message_class", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("message_number", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("sheet", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("field", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("category", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("reason", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("source_file", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("imported_by", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_index("ix_cockpit_feedback_run_id", "cockpit_feedback", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_cockpit_feedback_run_id", table_name="cockpit_feedback")
    op.drop_table("cockpit_feedback")
