"""Speech Analytics: the topic an AI assistant suggests, next to the catalog topic.

Owner decision 2026-09-15 (SP-309). `topic_id` stays what it is: a topic of the
catalog, chosen strictly by it. Beside it the model now gives its own opinion —
a catalog topic it considers closer, or a wording the catalog does not have.
The model does not change the catalog: a new wording lives only in the call.

Exactly one of the two columns is set. A catalog topic is a reference, and its
label comes from the catalog; a new wording is text.

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0007_ai_topic
Revises: sp_0006_project_source_key
Create Date: 2026-09-15
"""

import sqlalchemy as sa

from alembic import op

revision = "sp_0007_ai_topic"
down_revision = "sp_0006_project_source_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("speech_calls", sa.Column("ai_topic_id", sa.UUID(), nullable=True))
    # The name PostgreSQL gives an unnamed foreign key, as `topic_id` got in sp_0001.
    op.create_foreign_key(
        "speech_calls_ai_topic_id_fkey", "speech_calls", "speech_topics", ["ai_topic_id"], ["id"]
    )
    op.add_column("speech_calls", sa.Column("ai_topic_label", sa.String(length=255), nullable=True))
    op.create_check_constraint(
        "ck_speech_calls_ai_topic_one_of",
        "speech_calls",
        "ai_topic_id IS NULL OR ai_topic_label IS NULL",
    )
    # The report groups the calls of a period by this topic, as it does by the
    # catalog topic, and every such query also narrows by date.
    op.create_index("speech_calls_ai_topic_idx", "speech_calls", ["ai_topic_id", "started_at"])


def downgrade() -> None:
    op.drop_index("speech_calls_ai_topic_idx", table_name="speech_calls")
    op.drop_constraint("ck_speech_calls_ai_topic_one_of", "speech_calls", type_="check")
    op.drop_column("speech_calls", "ai_topic_label")
    op.drop_constraint("speech_calls_ai_topic_id_fkey", "speech_calls", type_="foreignkey")
    op.drop_column("speech_calls", "ai_topic_id")
