"""refunds, allocations, official status events, resolution reviews, case updates

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refunds",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("evidence_kind", sa.String(length=16), nullable=False),
        sa.Column("relates_to_incident", sa.String(length=16), nullable=False),
        sa.Column("identity_review_state", sa.String(length=32), nullable=False),
        sa.Column("merged_into_refund_id", sa.String(length=36), nullable=True),
        sa.Column("excluded_reason", sa.Text(), nullable=True),
        sa.Column("confirmed_distinct_from", sa.JSON(), nullable=False),
        sa.Column("source_observation_id", sa.String(length=36), nullable=True),
        sa.Column("created_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refunds_case_id", "refunds", ["case_id"])

    op.create_table(
        "refund_allocations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("refund_id", sa.String(length=36), nullable=False),
        sa.Column("payment_id", sa.String(length=36), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by_role", sa.String(length=32), nullable=False),
        sa.Column("created_revision", sa.Integer(), nullable=False),
        sa.Column("invalidated_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["refund_id"], ["refunds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("refund_id", "payment_id", name="uq_refund_allocation"),
    )
    op.create_index("ix_refund_allocations_case_id", "refund_allocations", ["case_id"])
    op.create_index("ix_refund_allocations_refund_id", "refund_allocations", ["refund_id"])
    op.create_index("ix_refund_allocations_payment_id", "refund_allocations", ["payment_id"])

    op.create_table(
        "official_status_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("raw_status", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("issuer_as_supplied", sa.Text(), nullable=True),
        sa.Column("source_ids", sa.JSON(), nullable=False),
        sa.Column("effective_at", sa.JSON(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("entered_by_role", sa.String(length=32), nullable=False),
        sa.Column("review_state", sa.String(length=32), nullable=False),
        sa.Column("origin", sa.String(length=40), nullable=False),
        sa.Column("source_observation_id", sa.String(length=36), nullable=True),
        sa.Column("created_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_official_status_events_case_id", "official_status_events", ["case_id"])

    op.create_table(
        "resolution_reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("recovery_state", sa.String(length=48), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("blocking_reasons", sa.JSON(), nullable=False),
        sa.Column("prepared_by_role", sa.String(length=32), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by_role", sa.String(length=32), nullable=True),
        sa.Column("review_outcome", sa.String(length=40), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_resolution_reviews_case_id", "resolution_reviews", ["case_id"])

    op.create_table(
        "case_updates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("new_revision", sa.Integer(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("acknowledged_by_role", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_case_updates_case_id", "case_updates", ["case_id"])

    with op.batch_alter_table("cases") as batch:
        batch.add_column(sa.Column("last_reviewed_revision", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("case_revisions") as batch:
        batch.add_column(sa.Column("totals_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("case_revisions") as batch:
        batch.drop_column("totals_json")
    with op.batch_alter_table("cases") as batch:
        batch.drop_column("last_reviewed_revision")
    for table in ("case_updates", "resolution_reviews", "official_status_events", "refund_allocations", "refunds"):
        op.drop_table(table)
