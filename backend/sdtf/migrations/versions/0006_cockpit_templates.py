"""migration cockpit templates per project and business object

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09 19:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cockpit_templates",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("object_type", sa.String(length=48), nullable=False),
        sa.Column("migration_object", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("filename", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("structure", sa.JSON(), nullable=True),
        sa.Column("mapping", sa.JSON(), nullable=True),
        sa.Column("uploaded_by", sa.String(length=64), nullable=False, server_default=""),
        sa.UniqueConstraint("project_id", "object_type", name="uq_cockpit_template"),
    )
    op.create_index("ix_cockpit_templates_project_id", "cockpit_templates", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_cockpit_templates_project_id", table_name="cockpit_templates")
    op.drop_table("cockpit_templates")
