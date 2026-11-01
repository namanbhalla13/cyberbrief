"""Bank-connected transaction verification: an append-only history of verification attempts, and approved bank requests with their delivery state.

Two new tables; no existing row is changed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transaction_verifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payment_id", sa.String(36), nullable=False),
        sa.Column("requested_by_account_id", sa.String(36), nullable=True),
        sa.Column("provider_id", sa.String(40), nullable=True),
        sa.Column("provider_name", sa.String(120), nullable=True),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("inflight", sa.String(8), nullable=True),
        sa.Column("reason_code", sa.String(40), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider_calls", sa.Integer(), nullable=False),
        sa.Column("input_summary", sa.JSON(), nullable=False),
        sa.Column("response_reference", sa.String(80), nullable=True),
        sa.Column("returned", sa.JSON(), nullable=True),
        sa.Column("comparison", sa.JSON(), nullable=True),
        sa.Column("case_revision", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("payment_id", "inflight", name="uq_verification_inflight"),
    )
    for col in ("case_id", "payment_id", "created_at"):
        op.create_index(f"ix_transaction_verifications_{col}", "transaction_verifications", [col])

    op.create_table(
        "bank_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("proposal_id", sa.String(36), nullable=True),
        sa.Column("message_id", sa.String(36), nullable=True),
        sa.Column("payment_ids", sa.JSON(), nullable=False),
        sa.Column("bank_id", sa.String(40), nullable=False),
        sa.Column("bank_name", sa.String(120), nullable=False),
        sa.Column("to_address", sa.String(254), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("response_ref", sa.String(24), nullable=False, unique=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("delivery", sa.String(12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("approved_by_account_id", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("queued_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for col in ("case_id", "proposal_id", "message_id", "state"):
        op.create_index(f"ix_bank_requests_{col}", "bank_requests", [col])


def downgrade() -> None:
    op.drop_table("bank_requests")
    op.drop_table("transaction_verifications")
