"""Speech Analytics: the call's problem — the operator's classification in NCC.

Owner decision 2026-09-15 (SP-604): the «problem» of a call in the report is the
classification the operator chose in NCC — the `uroven_1…5` levels of the
reporting views. Since SP-001a the daily ingest has kept that path in
`source_snapshot`; from now on it also writes it to `speech_calls.disposition`,
a column that exists since sp_0001 and has always been empty. This migration
fills it for the calls taken before, in the same form: the levels joined with
« › », the separator the interface uses between a topic and its parent.

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0008_call_disposition
Revises: sp_0007_ai_topic
Create Date: 2026-09-15
"""

from alembic import op

revision = "sp_0008_call_disposition"
down_revision = "sp_0007_ai_topic"
branch_labels = None
depends_on = None

_BACKFILL = """
UPDATE speech_calls
   SET disposition = left(
           array_to_string(
               ARRAY(
                   SELECT level
                     FROM jsonb_array_elements_text(source_snapshot->'disposition_path')
                          WITH ORDINALITY AS path(level, position)
                    WHERE level <> ''
                    ORDER BY position
               ),
               ' › '
           ),
           255
       )
 WHERE disposition IS NULL
   AND jsonb_typeof(source_snapshot->'disposition_path') = 'array'
   AND jsonb_array_length(source_snapshot->'disposition_path') > 0
"""


def upgrade() -> None:
    op.execute(_BACKFILL)
    # The report's problem filter and its list of problems both narrow by period.
    op.create_index("speech_calls_disposition_idx", "speech_calls", ["disposition", "started_at"])


def downgrade() -> None:
    op.drop_index("speech_calls_disposition_idx", table_name="speech_calls")
    # The filled values stay. They are copies of the snapshot, and emptying the
    # column would also erase what the ingest wrote after the upgrade.
