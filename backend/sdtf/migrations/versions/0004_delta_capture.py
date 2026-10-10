"""delta capture: source change log and delta event ledger

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09 17:40:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_change_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("system_id", sa.String(length=32), sa.ForeignKey("sap_systems.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("changenr", sa.String(length=32), nullable=False),
        sa.Column("object_type", sa.String(length=48), nullable=False, server_default=""),
        sa.Column("table_name", sa.String(length=30), nullable=False),
        sa.Column("record_key", sa.String(length=200), nullable=False),
        sa.Column("op", sa.String(length=1), nullable=False),
        sa.Column("changed_at", sa.String(length=14), nullable=False),
        sa.Column("changed_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.UniqueConstraint("system_id", "seq", name="uq_change_seq"),
    )
    op.create_table(
        "delta_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(length=32), sa.ForeignKey("migration_runs.id"), nullable=False),
        sa.Column("baseline_run_id", sa.String(length=32), sa.ForeignKey("migration_runs.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("changenr", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("object_type", sa.String(length=48), nullable=False, server_default=""),
        sa.Column("object_key", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("table_name", sa.String(length=30), nullable=False),
        sa.Column("record_key", sa.String(length=200), nullable=False),
        sa.Column("op", sa.String(length=1), nullable=False),
        sa.Column("changed_at", sa.String(length=14), nullable=False, server_default=""),
        sa.Column("changed_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("source_payload", sa.JSON(), nullable=True),
        sa.Column("target_payload", sa.JSON(), nullable=True),
        sa.Column("target_key", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="CAPTURED"),
        sa.Column("action", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("baseline_run_id", "seq", name="uq_delta_event_seq"),
    )
    op.create_index("ix_delta_events_run_id", "delta_events", ["run_id"])
    op.create_index("ix_delta_events_baseline_run_id", "delta_events", ["baseline_run_id"])
    op.create_index("ix_delta_target", "delta_events", ["baseline_run_id", "table_name", "target_key"])


def downgrade() -> None:
    op.drop_index("ix_delta_target", table_name="delta_events")
    op.drop_index("ix_delta_events_baseline_run_id", table_name="delta_events")
    op.drop_index("ix_delta_events_run_id", table_name="delta_events")
    op.drop_table("delta_events")
    op.drop_table("source_change_log")
