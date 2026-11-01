"""The officer's audit trail: simulated bank checks, suggested next steps and the decisions on them.

One new table, no change to existing ones. Rows hang off the case with ON DELETE CASCADE and are never
updated by the application.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8e3f4a5c6d7"
down_revision = "a7c1e2d3f4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=True),
        sa.Column("subject_id", sa.String(36), nullable=True),
        sa.Column("actor_session_id", sa.String(36), nullable=False),
        sa.Column("actor_role", sa.String(32), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_events_case_id", "audit_events", ["case_id"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_case_id", table_name="audit_events")
    op.drop_table("audit_events")
