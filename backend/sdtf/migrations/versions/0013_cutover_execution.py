"""incidents and assignments on cutover rehearsals

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-10 17:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cutover_rehearsals", sa.Column("incidents", sa.JSON(), nullable=True))
    op.add_column("cutover_rehearsals", sa.Column("assignments", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("cutover_rehearsals", "assignments")
    op.drop_column("cutover_rehearsals", "incidents")
