"""delta events: load method and API call per event

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-09 18:10:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("delta_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("load_method", sa.String(length=32), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("api_call", sa.String(length=160), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("delta_events", schema=None) as batch_op:
        batch_op.drop_column("api_call")
        batch_op.drop_column("load_method")
