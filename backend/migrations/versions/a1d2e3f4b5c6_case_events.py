"""Durable case events: what happened to a case that an agent may need to act on, with a lease and an outcome."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1d2e3f4b5c6"
down_revision = "c9f4a5b6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_case_event_key"),
    )
    op.create_index("ix_case_events_case_id", "case_events", ["case_id"])
    op.create_index("ix_case_events_state", "case_events", ["state"])


def downgrade() -> None:
    op.drop_index("ix_case_events_state", table_name="case_events")
    op.drop_index("ix_case_events_case_id", table_name="case_events")
    op.drop_table("case_events")
