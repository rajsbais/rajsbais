"""cutover rehearsal checklist

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-09 21:30:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cutover_rehearsals",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("manifest_id", sa.String(length=32), sa.ForeignKey("scope_manifests.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False, server_default="MOCK"),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="PLANNED"),
        sa.Column("verdict", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("items", sa.JSON(), nullable=True),
        sa.Column("runbook", sa.JSON(), nullable=True),
        sa.Column("timings", sa.JSON(), nullable=True),
        sa.Column("lessons", sa.JSON(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("start_note", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("completion_note", sa.String(length=400), nullable=False, server_default=""),
        sa.UniqueConstraint("manifest_id", "sequence", name="uq_cutover_rehearsal"),
    )
    op.create_index("ix_cutover_rehearsals_project_id", "cutover_rehearsals", ["project_id"])
    op.create_index("ix_cutover_rehearsals_manifest_id", "cutover_rehearsals", ["manifest_id"])


def downgrade() -> None:
    op.drop_index("ix_cutover_rehearsals_manifest_id", table_name="cutover_rehearsals")
    op.drop_index("ix_cutover_rehearsals_project_id", table_name="cutover_rehearsals")
    op.drop_table("cutover_rehearsals")
