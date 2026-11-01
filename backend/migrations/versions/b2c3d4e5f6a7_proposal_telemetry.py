"""What produced each proposal: origin, model, schema version, duration and the usage the model reported.

A plain add-column on `agent_proposals`. It is kept apart from `payload` on purpose: the payload is what an approval is bound to (its digest),
and timing or token counts change on every call.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1d2e3f4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_proposals", sa.Column("telemetry", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_proposals", "telemetry")
