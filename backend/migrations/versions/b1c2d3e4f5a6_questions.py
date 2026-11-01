"""questions

Revision ID: b1c2d3e4f5a6
Revises: ac71a98d6626
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "ac71a98d6626"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "questions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("related_payment_ids", sa.JSON(), nullable=False),
        sa.Column("related_fact_ids", sa.JSON(), nullable=False),
        sa.Column("related_source_ids", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("answer_source_id", sa.String(length=36), nullable=True),
        sa.Column("dismiss_reason", sa.Text(), nullable=True),
        sa.Column("created_revision", sa.Integer(), nullable=False),
        sa.Column("resolved_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "key", name="uq_question_key"),
    )
    op.create_index("ix_questions_case_id", "questions", ["case_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_questions_case_id", table_name="questions")
    op.drop_table("questions")
