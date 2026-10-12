"""Speech Analytics: a terminal status for a call whose recording never existed.

ING-04 says a call the source has no recording for stops the pipeline and stays
counted as processed. Neither existing terminal status says that: `failed` would
claim the pipeline broke -- it did not, and conflating the two destroys the only
signal that ingestion is actually broken -- and `done` would claim the call was
transcribed and is searchable, which it is not.

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0002_skipped_no_audio
Revises: sp_0001_initial
Create Date: 2026-09-09
"""

from alembic import op

revision = "sp_0002_skipped_no_audio"
down_revision = "sp_0001_initial"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_speech_calls_processing_status"
_TABLE = "speech_calls"

_BEFORE = (
    "processing_status IN ('new', 'queued', 'ingesting', 'transcribing', "
    "'masking', 'analyzing', 'done', 'failed', 'skipped_short')"
)
_AFTER = (
    "processing_status IN ('new', 'queued', 'ingesting', 'transcribing', "
    "'masking', 'analyzing', 'done', 'failed', 'skipped_short', "
    "'skipped_no_audio')"
)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _AFTER)


def downgrade() -> None:
    # Rows already carrying the new status would violate the narrower constraint,
    # so they are moved to `failed` first. That is a loss of meaning, and it is
    # the honest one available: a downgrade to a schema without the status cannot
    # keep calls in it, and `done` would put untranscribed calls in front of
    # users as finished work.
    op.execute(
        "UPDATE speech_calls SET processing_status = 'failed' "
        "WHERE processing_status = 'skipped_no_audio'"
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _BEFORE)
