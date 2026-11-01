"""Citizen's written reason when asking that a CyberBrief record be closed.

A plain add-column. `resolution_reviews` is only referenced *by* other rows, so SQLite can extend
it in place; the `copy_from` batch dance in e4f5a6b7c8d9 was needed because `cases` has foreign
keys that a table recreate would silently drop.

No change is needed for the new `review_state` value "closed" or the new review outcome
"closure_agreed": both columns are plain strings with no check constraint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resolution_reviews", sa.Column("request_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("resolution_reviews", "request_note")
