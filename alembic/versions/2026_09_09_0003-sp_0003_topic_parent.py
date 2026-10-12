"""Speech Analytics: topics become two levels deep.

The owner's reference arrived as a five-level tree of 430 rows. Three of those
levels are not a topic at all: level 3 is sentiment, which the pipeline already
classifies on its own, and levels 1-2 are the client segment and the product
line -- properties of the NCC project rather than of the conversation. What is
left is 11 broad topics and 88 refinements, and a refinement is only unique
together with its parent: eight leaves appear under two different parents.

Hence a parent, not a flattened path. 397 flat codes would fit neither a filter
chip row nor a classifier prompt.

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0003_topic_parent
Revises: sp_0002_skipped_no_audio
Create Date: 2026-09-09
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "sp_0003_topic_parent"
down_revision = "sp_0002_skipped_no_audio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "speech_topics",
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_speech_topics_parent",
        "speech_topics",
        "speech_topics",
        ["parent_id"],
        ["id"],
        # RESTRICT, not CASCADE: deleting a broad topic must not silently take
        # its refinements -- and with them the labelling of every call that used
        # one -- along with it. A topic in use is deactivated, never deleted
        # (ADM-01).
        ondelete="RESTRICT",
    )
    op.create_index("speech_topics_parent_idx", "speech_topics", ["parent_id", "sort_order"])


def downgrade() -> None:
    op.drop_index("speech_topics_parent_idx", table_name="speech_topics")
    op.drop_constraint("fk_speech_topics_parent", "speech_topics", type_="foreignkey")
    op.drop_column("speech_topics", "parent_id")
