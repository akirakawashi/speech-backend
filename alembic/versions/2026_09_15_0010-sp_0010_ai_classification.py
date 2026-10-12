"""Speech Analytics: the AI's classification along the operator's tree (SP-310, stage 2).

The analysis stage now answers with a path through the five-level classifier —
the tree the operator classifies by in NCC — instead of a topic code (S-22).
`ai_node_id` is the node down to which that path was found in the tree.

The topic an AI assistant suggests (SP-309) becomes a path too: labels, one per
level, any of which may be the model's own wording. `ai_suggestion` keeps the
labels; `ai_suggestion_node_id` is set when the whole suggestion is a path of
the tree.

`topic_id`, `ai_topic_id` and `ai_topic_label` are not dropped here: the search,
the call card and the report read them until they move to the tree.

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0010_ai_classification
Revises: sp_0009_classifier
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "sp_0010_ai_classification"
down_revision = "sp_0009_classifier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("speech_calls", sa.Column("ai_node_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "speech_calls_ai_node_id_fkey",
        "speech_calls",
        "speech_classifier_nodes",
        ["ai_node_id"],
        ["id"],
    )
    # The report and the filters narrow by this node and by date together.
    op.create_index("speech_calls_ai_node_idx", "speech_calls", ["ai_node_id", "started_at"])

    op.add_column("speech_calls", sa.Column("ai_suggestion", postgresql.JSONB(), nullable=True))
    op.add_column("speech_calls", sa.Column("ai_suggestion_node_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "speech_calls_ai_suggestion_node_id_fkey",
        "speech_calls",
        "speech_classifier_nodes",
        ["ai_suggestion_node_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "speech_calls_ai_suggestion_node_id_fkey", "speech_calls", type_="foreignkey"
    )
    op.drop_column("speech_calls", "ai_suggestion_node_id")
    op.drop_column("speech_calls", "ai_suggestion")
    op.drop_index("speech_calls_ai_node_idx", table_name="speech_calls")
    op.drop_constraint("speech_calls_ai_node_id_fkey", "speech_calls", type_="foreignkey")
    op.drop_column("speech_calls", "ai_node_id")
