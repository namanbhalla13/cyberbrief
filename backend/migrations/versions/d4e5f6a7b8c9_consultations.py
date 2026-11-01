"""Officer-to-officer consultations (an approved question, replies, an append-only log) and the two-phase investigation columns.

Two new tables and three nullable-or-defaulted columns; no existing row is changed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("investigation_runs", sa.Column("phase", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("investigation_runs", sa.Column("parent_run_id", sa.String(36), nullable=True))
    op.add_column("investigation_runs", sa.Column("trigger", sa.JSON(), nullable=True))
    op.create_index("ix_investigation_runs_parent_run_id", "investigation_runs", ["parent_run_id"])

    op.create_table(
        "consultations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("earlier_case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_account_id", sa.String(36), nullable=False),
        sa.Column("recipient_account_id", sa.String(36), nullable=True),
        sa.Column("candidate_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("approved_digest", sa.String(64), nullable=True),
        sa.Column("source_revisions", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("sender_seen_at", sa.DateTime(), nullable=True),
        sa.Column("recipient_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("case_id", "earlier_case_id", "sender_account_id", "payload_digest", name="uq_consultation_digest"),
    )
    for col in ("case_id", "earlier_case_id", "sender_account_id", "recipient_account_id", "state"):
        op.create_index(f"ix_consultations_{col}", "consultations", [col])

    op.create_table(
        "consultation_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("consultation_id", sa.String(36), sa.ForeignKey("consultations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("author_account_id", sa.String(36), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("consultation_id", "seq", name="uq_consultation_seq"),
        sa.UniqueConstraint("consultation_id", "idempotency_key", name="uq_consultation_idem"),
    )
    op.create_index("ix_consultation_messages_consultation_id", "consultation_messages", ["consultation_id"])


def downgrade() -> None:
    op.drop_table("consultation_messages")
    op.drop_table("consultations")
    op.drop_index("ix_investigation_runs_parent_run_id", table_name="investigation_runs")
    for col in ("trigger", "parent_run_id", "phase"):
        op.drop_column("investigation_runs", col)
