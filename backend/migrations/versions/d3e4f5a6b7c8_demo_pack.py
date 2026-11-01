"""cases.demo_pack_id and demo_steps_loaded

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.add_column(sa.Column("demo_pack_id", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("demo_steps_loaded", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.drop_column("demo_steps_loaded")
        batch.drop_column("demo_pack_id")
