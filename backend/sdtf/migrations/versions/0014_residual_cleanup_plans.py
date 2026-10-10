"""residual cleanup plans

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-10 18:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "residual_cleanup_plans",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("manifest_id", sa.String(length=32), sa.ForeignKey("scope_manifests.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="DRAFT"),
        sa.Column("deal_type", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("residual_rule", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("items", sa.JSON(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("package", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("approved_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_comment", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("executed_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution", sa.JSON(), nullable=True),
    )
    op.create_index("ix_residual_cleanup_plans_manifest_id", "residual_cleanup_plans", ["manifest_id"])
    op.create_index("ix_residual_cleanup_plans_project_id", "residual_cleanup_plans", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_residual_cleanup_plans_project_id", table_name="residual_cleanup_plans")
    op.drop_index("ix_residual_cleanup_plans_manifest_id", table_name="residual_cleanup_plans")
    op.drop_table("residual_cleanup_plans")
