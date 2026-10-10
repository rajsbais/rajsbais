"""per-rule decisions on rule sets

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-10 16:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rule_sets", sa.Column("rule_decisions", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("rule_sets", "rule_decisions")
