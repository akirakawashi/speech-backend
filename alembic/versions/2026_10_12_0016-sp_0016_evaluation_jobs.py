"""Оценка разговоров в общей очереди.

Revision ID: sp_0016_evaluation_jobs
Revises: sp_0015_drop_masking
Create Date: 2026-10-12
"""

from alembic import op

revision = "sp_0016_evaluation_jobs"
down_revision = "sp_0015_drop_masking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Разрешить задания evaluate и добавить их лимит попыток без перезаписи настроек."""
    op.drop_constraint("ck_speech_jobs_kind", "speech_jobs", type_="check")
    op.create_check_constraint(
        "ck_speech_jobs_kind",
        "speech_jobs",
        "kind IN ('ingest', 'transcribe', 'analyze', 'evaluate')",
    )
    op.execute(
        "UPDATE speech_settings SET value = '{\"evaluate\": 3}'::jsonb || value "
        "WHERE key = 'max_attempts'"
    )


def downgrade() -> None:
    """Удалить только задания нового вида и вернуть прежнее ограничение после остановки воркеров."""
    op.execute("DELETE FROM speech_jobs WHERE kind = 'evaluate'")
    op.execute("UPDATE speech_settings SET value = value - 'evaluate' WHERE key = 'max_attempts'")
    op.drop_constraint("ck_speech_jobs_kind", "speech_jobs", type_="check")
    op.create_check_constraint(
        "ck_speech_jobs_kind", "speech_jobs", "kind IN ('ingest', 'transcribe', 'analyze')"
    )
