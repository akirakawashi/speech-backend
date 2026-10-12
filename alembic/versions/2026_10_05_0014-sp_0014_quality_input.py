"""Оценка: актуальность ролей и явный отказ вместо нулевого балла.

Revision ID: sp_0014_quality_input
Revises: sp_0013_call_quality
Create Date: 2026-10-05
"""

import sqlalchemy as sa

from alembic import op

revision = "sp_0014_quality_input"
down_revision = "sp_0013_call_quality"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Старые оценки без отпечатка требуют пересчёта по новым правилам.
    op.add_column("speech_call_evaluations", sa.Column("transcript_fingerprint", sa.String(32)))
    op.add_column("speech_call_evaluations", sa.Column("failure_reason", sa.String(32)))
    op.alter_column("speech_call_evaluations", "script_applies", nullable=True)
    op.create_check_constraint(
        "ck_speech_call_evaluations_failure",
        "speech_call_evaluations",
        "(failure_reason IS NULL AND script_applies IS NOT NULL) OR "
        "(failure_reason IS NOT NULL AND script_applies IS NULL AND score IS NULL "
        "AND passed_count IS NULL AND stage_count IS NULL AND all_passed IS NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM speech_call_evaluations WHERE failure_reason IS NOT NULL")
    op.drop_constraint("ck_speech_call_evaluations_failure", "speech_call_evaluations")
    op.alter_column("speech_call_evaluations", "script_applies", nullable=False)
    op.drop_column("speech_call_evaluations", "failure_reason")
    op.drop_column("speech_call_evaluations", "transcript_fingerprint")
