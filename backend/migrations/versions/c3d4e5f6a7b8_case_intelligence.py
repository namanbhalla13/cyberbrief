"""Case intelligence: a searchable projection of submitted reports, entity observations, candidates, investigation runs and handling lessons.

Only new tables and one new nullable column; no existing row is changed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def _case_fk(nullable: bool = False) -> sa.Column:
    return sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=nullable)


def upgrade() -> None:
    op.add_column("officer_profiles", sa.Column("capabilities", sa.JSON(), nullable=True))

    op.create_table(
        "case_projections",
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("case_revision", sa.Integer(), nullable=False),
        sa.Column("projection_version", sa.String(16), nullable=False),
        sa.Column("indexed_at", sa.DateTime(), nullable=False),
        sa.Column("source_revision_digest", sa.String(64), nullable=False),
        sa.Column("visibility_policy_version", sa.String(24), nullable=False),
        sa.Column("review_state", sa.String(24), nullable=False),
        sa.Column("area", sa.String(48), nullable=True),
        sa.Column("assigned_officer_id", sa.String(36), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("method_tags", sa.JSON(), nullable=False),
        sa.Column("event_sequence", sa.JSON(), nullable=False),
        sa.Column("outcome_refs", sa.JSON(), nullable=False),
    )
    op.create_index("ix_case_projections_review_state", "case_projections", ["review_state"])
    op.create_index("ix_case_projections_area", "case_projections", ["area"])
    op.create_index("ix_case_projections_assigned_officer_id", "case_projections", ["assigned_officer_id"])

    op.create_table(
        "entity_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        _case_fk(),
        sa.Column("case_revision", sa.Integer(), nullable=False),
        sa.Column("projection_version", sa.String(16), nullable=False),
        sa.Column("entity_type", sa.String(24), nullable=False),
        sa.Column("normalized_value", sa.String(200), nullable=False),
        sa.Column("display_value", sa.String(200), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("quality", sa.String(12), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=True),
        sa.Column("fact_id", sa.String(36), nullable=True),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column("field_pointer", sa.String(80), nullable=False),
        sa.Column("assertion_origin", sa.String(24), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("retracted_revision", sa.Integer(), nullable=True),
        sa.Column("supersedes_version", sa.Integer(), nullable=True),
    )
    op.create_index("ix_entity_observations_case_id", "entity_observations", ["case_id"])
    op.create_index("ix_entity_observations_state", "entity_observations", ["state"])
    op.create_index("ix_entity_lookup", "entity_observations", ["entity_type", "normalized_value", "state"])

    op.create_table(
        "case_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        _case_fk(),
        sa.Column("other_case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("viewer_account_id", sa.String(36), nullable=False),
        sa.Column("result_kind", sa.String(24), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("signals", sa.JSON(), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("case_id", "other_case_id", "viewer_account_id", name="uq_case_candidate"),
    )
    op.create_index("ix_case_candidates_case_id", "case_candidates", ["case_id"])
    op.create_index("ix_case_candidates_other_case_id", "case_candidates", ["other_case_id"])
    op.create_index("ix_case_candidates_viewer_account_id", "case_candidates", ["viewer_account_id"])

    op.create_table(
        "investigation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        _case_fk(),
        sa.Column("officer_account_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("goal", sa.String(400), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("trace", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("input_revisions", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(24), nullable=False),
        sa.Column("proposal_ids", sa.JSON(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(12), nullable=False),
        sa.Column("provider", sa.String(24), nullable=True),
        sa.Column("model_id", sa.String(80), nullable=True),
        sa.Column("telemetry", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("stale_reason", sa.String(120), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_investigation_runs_case_id", "investigation_runs", ["case_id"])
    op.create_index("ix_investigation_runs_officer_account_id", "investigation_runs", ["officer_account_id"])
    op.create_index("ix_investigation_runs_state", "investigation_runs", ["state"])

    op.create_table(
        "handling_lessons",
        sa.Column("id", sa.String(36), primary_key=True),
        _case_fk(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("outcome_kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author_account_id", sa.String(36), nullable=False),
        sa.Column("review_state", sa.String(16), nullable=False),
        sa.Column("case_revision", sa.Integer(), nullable=False),
        sa.Column("record_refs", sa.JSON(), nullable=False),
        sa.Column("supersedes_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("case_id", "version", name="uq_lesson_version"),
    )
    op.create_index("ix_handling_lessons_case_id", "handling_lessons", ["case_id"])


def downgrade() -> None:
    op.drop_table("handling_lessons")
    op.drop_table("investigation_runs")
    op.drop_table("case_candidates")
    op.drop_table("entity_observations")
    op.drop_table("case_projections")
    op.drop_column("officer_profiles", "capabilities")
