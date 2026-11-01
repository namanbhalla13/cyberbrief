"""The citizen chat assistant: conversations and their messages.

Two new tables, no change to existing ones. A conversation hangs off the citizen's account and,
once a complaint exists, off that complaint as well, with ON DELETE CASCADE on both so deleting
either removes the chat.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7c1e2d3f4b5"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=True),
        sa.Column("language", sa.String(16), nullable=False, server_default="en"),
        sa.Column("slots", sa.JSON(), nullable=False),
        sa.Column("last_suggestion", sa.String(32), nullable=True),
        sa.Column("model_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_assistant_conversations_account_id", "assistant_conversations", ["account_id"])
    op.create_index("ix_assistant_conversations_case_id", "assistant_conversations", ["case_id"])

    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("blocks", sa.JSON(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("conversation_id", "ordinal", name="uq_assistant_message_ordinal"),
    )
    op.create_index("ix_assistant_messages_conversation_id", "assistant_messages", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_assistant_messages_conversation_id", table_name="assistant_messages")
    op.drop_table("assistant_messages")
    op.drop_index("ix_assistant_conversations_case_id", table_name="assistant_conversations")
    op.drop_index("ix_assistant_conversations_account_id", table_name="assistant_conversations")
    op.drop_table("assistant_conversations")
