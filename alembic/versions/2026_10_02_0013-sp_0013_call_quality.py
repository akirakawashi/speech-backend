"""Speech Analytics: a call scored against the operator's script.

Quality control listens to a call and marks each stage of the sales script
1 or 0. `speech_call_evaluations` keeps the same verdicts produced by a language
model: one row per call and script revision, with the stage verdicts and the
client's objections as `jsonb`, and the score and the "fully correct" flag
stored next to them because the summary aggregates over both.

A call the script does not apply to — an inbound support call, another
campaign — still gets a row, without scores, so the evaluation command does not
pick it up again on every run.

`evaluated_at` is the moment the replies were read. A row older than the
transcript's `masked_at` is stale — the call was reprocessed and its replies
replaced — so readers skip it and the command scores the call again.

Run with `alembic -c alembic.ini upgrade head`.

Revision ID: sp_0013_call_quality
Revises: sp_0012_call_source
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "sp_0013_call_quality"
down_revision = "sp_0012_call_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "speech_call_evaluations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("script_version", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("script_applies", sa.Boolean(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=True),
        sa.Column("passed_count", sa.SmallInteger(), nullable=True),
        sa.Column("stage_count", sa.SmallInteger(), nullable=True),
        sa.Column("all_passed", sa.Boolean(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("stages", postgresql.JSONB(), nullable=False),
        sa.Column("objections", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "score IS NULL OR score BETWEEN 0 AND 100", name="ck_speech_call_evaluations_score"
        ),
        sa.CheckConstraint(
            "script_applies OR (score IS NULL AND all_passed IS NULL)",
            name="ck_speech_call_evaluations_not_applicable",
        ),
        sa.ForeignKeyConstraint(["call_id"], ["speech_calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "call_id", "script_version", name="uq_speech_call_evaluations_call_script"
        ),
    )
    # The summary reads one script revision at a time.
    op.create_index(
        "speech_call_evaluations_script_idx", "speech_call_evaluations", ["script_version"]
    )


def downgrade() -> None:
    op.drop_index("speech_call_evaluations_script_idx", table_name="speech_call_evaluations")
    op.drop_table("speech_call_evaluations")
