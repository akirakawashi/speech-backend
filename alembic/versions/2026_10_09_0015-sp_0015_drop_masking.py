"""Обезличивания больше нет: один текст реплики, этап `mask` убран.

Revision ID: sp_0015_drop_masking
Revises: sp_0014_quality_input
Create Date: 2026-10-09

Решение владельца 2026-10-09: персональные данные в расшифровке не скрываются.
Вторая копия текста с масками (`text_masked`), разметка найденного
(`pii_spans`) и отметка обезличивания (`masked_at`) удаляются. Экран, поиск,
модели и выгрузки читают текст расшифровки как есть — `text_raw`. Он хранился
полностью с самого начала, поэтому старые звонки открываются без переобработки.

Накатывать при остановленных воркерах. Таблица реплик переписывается целиком и
индекс поиска строится заново: на рабочей базе это минуты, и всё это время
таблица заблокирована.

Что миграция делает с данными:

* отпечатки оценок пересчитываются по тексту без масок — у тех оценок, чей
  отпечаток сходился с расшифровкой. Иначе сверка `evaluate --drop-stale` сочла
  бы устаревшей каждую оценку звонка, в тексте которого стояла маска;
* звонок, не дошедший до конца обезличивания или упавший на нём, получает
  задание разбора: текст у него есть, не хватало только маски;
* задания `mask` удаляются, из настройки числа попыток уходит ключ `mask`.

Откат возвращает колонки, но не маски: во вторую копию ложится тот же текст.
"""

from alembic import op

revision = "sp_0015_drop_masking"
down_revision = "sp_0014_quality_input"
branch_labels = None
depends_on = None

#: Отпечаток реплик звонка — выражение из infrastructure.persistence.quality_transcript
#: на момент миграции, с подставленной колонкой текста. Записано здесь целиком:
#: миграция не должна зависеть от кода, который изменится после неё.
_FINGERPRINT = """
    (SELECT md5(coalesce(
        jsonb_agg(jsonb_build_array(
            qs.seq, qs.speaker, qs.start_ms, qs.end_ms, qs.{column}
        ) ORDER BY qs.seq)::text, '[]'
    )) FROM speech_transcript_segments AS qs WHERE qs.call_id = e.call_id)
"""

_KINDS_NEW = "'ingest', 'transcribe', 'analyze'"
_KINDS_OLD = "'ingest', 'transcribe', 'mask', 'analyze'"
_STATUSES_NEW = (
    "'new', 'queued', 'ingesting', 'transcribing', 'analyzing', 'done', 'failed', "
    "'skipped_short', 'skipped_no_audio'"
)
_STATUSES_OLD = (
    "'new', 'queued', 'ingesting', 'transcribing', 'masking', 'analyzing', 'done', "
    "'failed', 'skipped_short', 'skipped_no_audio'"
)


def upgrade() -> None:
    # Пока обе копии текста на месте: отпечаток, сходившийся с копией с масками,
    # заменяется отпечатком по исходному тексту.
    op.execute(
        f"""
        UPDATE speech_call_evaluations AS e
           SET transcript_fingerprint = {_FINGERPRINT.format(column="text_raw")}
         WHERE e.transcript_fingerprint IS NOT NULL
           AND e.transcript_fingerprint = {_FINGERPRINT.format(column="text_masked")}
        """
    )

    # Звонки, застрявшие на обезличивании: задание `mask` не выполнено, а
    # расшифровка есть. Им ставится разбор — с полосой и приоритетом задания,
    # которое он заменяет. Разбор, оставшийся от прежнего прохода звонка,
    # ставится в очередь заново; идущий и ждущий не трогаются.
    op.execute(
        """
        INSERT INTO speech_jobs (
            id, call_id, kind, lane, status, priority, attempts, max_attempts,
            run_after, lease_seconds, payload, created_at
        )
        SELECT gen_random_uuid(), j.call_id, 'analyze', j.lane, 'queued', j.priority, 0, 3,
               now(), 300, '{}'::jsonb, now()
          FROM speech_jobs AS j
         WHERE j.kind = 'mask'
           AND j.status <> 'done'
           AND EXISTS (SELECT 1 FROM speech_transcripts AS t WHERE t.call_id = j.call_id)
        ON CONFLICT (call_id, kind) DO UPDATE
           SET status = 'queued', lane = EXCLUDED.lane, priority = EXCLUDED.priority,
               attempts = 0, run_after = now(), locked_by = NULL, locked_at = NULL,
               started_at = NULL, finished_at = NULL,
               last_error_class = NULL, last_error_at = NULL
         WHERE speech_jobs.status IN ('done', 'failed', 'dead')
        """
    )
    op.execute(
        """
        UPDATE speech_calls AS c
           SET processing_status = 'analyzing', completed_at = NULL, updated_at = now()
         WHERE EXISTS (
               SELECT 1 FROM speech_jobs AS j
                WHERE j.call_id = c.id AND j.kind = 'mask' AND j.status <> 'done')
           AND EXISTS (
               SELECT 1 FROM speech_jobs AS j
                WHERE j.call_id = c.id AND j.kind = 'analyze' AND j.status = 'queued')
        """
    )
    # Статус без задания — на случай строк, которые правили руками: звонок без
    # расшифровки возвращается в очередь расшифровки не миграцией, а
    # переобработкой, здесь он просто перестаёт называться «обезличивается».
    op.execute(
        "UPDATE speech_calls SET processing_status = 'failed', updated_at = now() "
        "WHERE processing_status = 'masking'"
    )
    op.execute("DELETE FROM speech_jobs WHERE kind = 'mask'")
    op.execute("UPDATE speech_settings SET value = value - 'mask' WHERE key = 'max_attempts'")

    op.drop_constraint("ck_speech_jobs_kind", "speech_jobs", type_="check")
    op.create_check_constraint("ck_speech_jobs_kind", "speech_jobs", f"kind IN ({_KINDS_NEW})")
    op.drop_constraint("ck_speech_calls_processing_status", "speech_calls", type_="check")
    op.create_check_constraint(
        "ck_speech_calls_processing_status",
        "speech_calls",
        f"processing_status IN ({_STATUSES_NEW})",
    )

    # Одним оператором: таблица переписывается один раз. Индекс уходит вместе
    # со старой колонкой поиска и строится по новой.
    op.execute(
        """
        ALTER TABLE speech_transcript_segments
            DROP COLUMN search_vector,
            DROP COLUMN text_masked,
            DROP COLUMN pii_spans,
            ADD COLUMN search_vector tsvector
                GENERATED ALWAYS AS (to_tsvector('russian', text_raw)) STORED NOT NULL
        """
    )
    op.create_index(
        "speech_segments_fts_idx",
        "speech_transcript_segments",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.drop_column("speech_transcripts", "masked_at")


def downgrade() -> None:
    op.execute("ALTER TABLE speech_transcripts ADD COLUMN masked_at timestamptz")
    op.execute("UPDATE speech_transcripts SET masked_at = created_at")
    op.execute(
        """
        ALTER TABLE speech_transcript_segments
            DROP COLUMN search_vector,
            ADD COLUMN text_masked text,
            ADD COLUMN pii_spans jsonb NOT NULL DEFAULT '[]'::jsonb
        """
    )
    # Масок нет и взяться им неоткуда: во вторую копию ложится тот же текст.
    op.execute("UPDATE speech_transcript_segments SET text_masked = text_raw")
    op.execute(
        """
        ALTER TABLE speech_transcript_segments
            ALTER COLUMN text_masked SET NOT NULL,
            ALTER COLUMN pii_spans DROP DEFAULT,
            ADD COLUMN search_vector tsvector
                GENERATED ALWAYS AS (to_tsvector('russian', text_masked)) STORED NOT NULL
        """
    )
    op.create_index(
        "speech_segments_fts_idx",
        "speech_transcript_segments",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.drop_constraint("ck_speech_jobs_kind", "speech_jobs", type_="check")
    op.create_check_constraint("ck_speech_jobs_kind", "speech_jobs", f"kind IN ({_KINDS_OLD})")
    op.drop_constraint("ck_speech_calls_processing_status", "speech_calls", type_="check")
    op.create_check_constraint(
        "ck_speech_calls_processing_status",
        "speech_calls",
        f"processing_status IN ({_STATUSES_OLD})",
    )
    op.execute(
        "UPDATE speech_settings SET value = value || '{\"mask\": 5}'::jsonb "
        "WHERE key = 'max_attempts'"
    )
