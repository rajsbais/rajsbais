"""migration object registry per project and release

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09 19:40:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_object_registry",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("release", sa.String(length=32), nullable=False, server_default="*"),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("object_id", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("object_types", sa.JSON(), nullable=True),
        sa.Column("tables", sa.JSON(), nullable=True),
        sa.Column("notes", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("imported_by", sa.String(length=64), nullable=False, server_default=""),
        sa.UniqueConstraint("project_id", "release", "name", name="uq_migration_object_entry"),
    )
    op.create_index("ix_migration_object_registry_project_id", "migration_object_registry", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_migration_object_registry_project_id", table_name="migration_object_registry")
    op.drop_table("migration_object_registry")
