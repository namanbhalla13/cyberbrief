"""The three-role workflow: an admin account, officer ownership of cases, and the records around it.

Adds the `admin` kind to accounts, routing and ownership columns on cases, and tables for staff profiles, agent
proposals, messages, tasks, claim assessments, related-case links and agent activity. Nothing existing is rewritten.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9f4a5b6d7e8"
down_revision = "b8e3f4a5c6d7"
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column("id", sa.String(36), primary_key=True)


def _case_fk() -> sa.Column:
    return sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("ck_accounts_kind", type_="check")
        batch.drop_constraint("ck_accounts_identity", type_="check")
        batch.create_check_constraint("ck_accounts_kind", "kind IN ('citizen','officer','admin')")
        batch.create_check_constraint(
            "ck_accounts_identity",
            "(kind='citizen' AND mobile IS NOT NULL AND officer_id IS NULL AND password_hash IS NULL)"
            " OR (kind IN ('officer','admin') AND officer_id IS NOT NULL AND password_hash IS NOT NULL AND mobile IS NULL)",
        )
    with op.batch_alter_table("cases") as batch:
        batch.add_column(sa.Column("area", sa.String(48), nullable=True))
        batch.add_column(sa.Column("assigned_officer_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("assigned_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("acknowledged_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("workflow_state", sa.String(24), nullable=True))
        batch.create_index("ix_cases_assigned_officer_id", ["assigned_officer_id"])
        batch.create_index("ix_cases_workflow_state", ["workflow_state"])

    op.create_table(
        "officer_profiles",
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("areas", sa.JSON(), nullable=False),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("expertise", sa.JSON(), nullable=False),
        sa.Column("on_duty", sa.Boolean(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("active_count", sa.Integer(), nullable=False),
    )
    op.create_table(
        "agent_proposals", _id(), _case_fk(),
        sa.Column("agent", sa.String(24), nullable=False), sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False), sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False), sa.Column("origin", sa.String(12), nullable=False),
        sa.Column("state", sa.String(16), nullable=False), sa.Column("decided_by", sa.String(36), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True), sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("case_id", "kind", "digest", name="uq_proposal_digest"),
    )
    op.create_index("ix_agent_proposals_case_id", "agent_proposals", ["case_id"])
    op.create_table(
        "case_messages", _id(), _case_fk(),
        sa.Column("channel", sa.String(12), nullable=False), sa.Column("direction", sa.String(12), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=True), sa.Column("author_account_id", sa.String(36), nullable=True),
        sa.Column("body", sa.Text(), nullable=False), sa.Column("payment_ids", sa.JSON(), nullable=False),
        sa.Column("source_ids", sa.JSON(), nullable=False), sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False), sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_case_messages_case_id", "case_messages", ["case_id"])
    op.create_table(
        "case_tasks", _id(), _case_fk(),
        sa.Column("kind", sa.String(24), nullable=False), sa.Column("title", sa.String(200), nullable=False),
        sa.Column("ref_id", sa.String(36), nullable=True), sa.Column("state", sa.String(8), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_case_tasks_case_id", "case_tasks", ["case_id"])
    op.create_table(
        "claim_assessments", _id(), _case_fk(),
        sa.Column("claim_key", sa.String(64), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("assessment", sa.String(24), nullable=False), sa.Column("note", sa.Text(), nullable=True),
        sa.Column("officer_account_id", sa.String(36), nullable=False), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("case_id", "claim_key", "version", name="uq_assessment_version"),
    )
    op.create_index("ix_claim_assessments_case_id", "claim_assessments", ["case_id"])
    op.create_table(
        "related_links", _id(), _case_fk(),
        sa.Column("other_case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("signal", sa.String(24), nullable=False), sa.Column("value", sa.String(120), nullable=False),
        sa.Column("state", sa.String(16), nullable=False), sa.Column("reviewed_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("case_id", "other_case_id", "signal", "value", name="uq_related_link"),
    )
    op.create_index("ix_related_links_case_id", "related_links", ["case_id"])
    op.create_index("ix_related_links_other_case_id", "related_links", ["other_case_id"])
    op.create_table(
        "agent_activity", _id(),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=True),
        sa.Column("agent", sa.String(24), nullable=False), sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(200), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_agent_activity_case_id", "agent_activity", ["case_id"])
    op.create_index("ix_agent_activity_created_at", "agent_activity", ["created_at"])


def downgrade() -> None:
    for table in ("agent_activity", "related_links", "claim_assessments", "case_tasks", "case_messages", "agent_proposals", "officer_profiles"):
        op.drop_table(table)
    with op.batch_alter_table("cases") as batch:
        batch.drop_index("ix_cases_workflow_state")
        batch.drop_index("ix_cases_assigned_officer_id")
        for column in ("workflow_state", "acknowledged_at", "assigned_at", "assigned_officer_id", "area"):
            batch.drop_column(column)
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("ck_accounts_identity", type_="check")
        batch.drop_constraint("ck_accounts_kind", type_="check")
        batch.create_check_constraint("ck_accounts_kind", "kind IN ('citizen','officer')")
        batch.create_check_constraint(
            "ck_accounts_identity",
            "(kind='citizen' AND mobile IS NOT NULL AND officer_id IS NULL AND password_hash IS NULL)"
            " OR (kind='officer' AND officer_id IS NOT NULL AND password_hash IS NOT NULL AND mobile IS NULL)",
        )
