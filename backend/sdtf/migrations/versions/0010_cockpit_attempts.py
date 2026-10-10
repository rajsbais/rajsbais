"""migration cockpit package rounds (re-upload tracking)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-09 20:50:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cockpit_attempts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(length=32), sa.ForeignKey("migration_runs.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=12), nullable=False, server_default="all"),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="EXPORTED"),
        sa.Column("package_dir", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("zip_path", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("instances", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("instance_keys", sa.JSON(), nullable=True),
        sa.Column("outcomes", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exported_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("upload_note", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("simulated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("simulated_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("migrated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("migrated_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("migration_note", sa.String(length=400), nullable=False, server_default=""),
        sa.UniqueConstraint("run_id", "sequence", name="uq_cockpit_attempt"),
    )
    op.create_index("ix_cockpit_attempts_run_id", "cockpit_attempts", ["run_id"])
    with op.batch_alter_table("cockpit_feedback", schema=None) as batch_op:
        batch_op.add_column(sa.Column("attempt_id", sa.String(length=32), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("attempt_sequence", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("cockpit_feedback", schema=None) as batch_op:
        batch_op.drop_column("attempt_sequence")
        batch_op.drop_column("attempt_id")
    op.drop_index("ix_cockpit_attempts_run_id", table_name="cockpit_attempts")
    op.drop_table("cockpit_attempts")
