"""migration cockpit template aliases learned per project

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09 19:30:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cockpit_aliases",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("alias", sa.String(length=60), nullable=False),
        sa.Column("field", sa.String(length=30), nullable=False),
        sa.Column("table_name", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("description", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("evidence", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("object_type", sa.String(length=48), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="PROPOSED"),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("decided_by", sa.String(length=64), nullable=False, server_default=""),
        sa.UniqueConstraint("project_id", "alias", "table_name", name="uq_cockpit_alias"),
    )
    op.create_index("ix_cockpit_aliases_project_id", "cockpit_aliases", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_cockpit_aliases_project_id", table_name="cockpit_aliases")
    op.drop_table("cockpit_aliases")
