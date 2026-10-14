"""Миграция и ограничения хранения на отдельной схеме настоящей PostgreSQL."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from src.application.dto.calls import AnalysisStatus, JobKind, JobLane, JobStatus
from src.application.dto.pipeline import AnalyzeWork
from src.infrastructure.persistence.models.analysis import (
    SpeechAnalysisReference,
    SpeechCallAnalysis,
)
from src.infrastructure.persistence.models.speech import (
    SpeechCall,
    SpeechClassifierNode,
    SpeechJob,
    SpeechTranscript,
)
from src.infrastructure.persistence.session import SPEECH_REQUIRED_SCHEMA_REVISION
from src.infrastructure.persistence.stages.analyze import store_result
from tests.analysis_storage_support import (
    CALL_ID,
    NODE_ID,
    NOW,
    REFERENCE_ID,
    TRANSCRIPT_ID,
    analysis,
    references,
)
from tests.speech_db_support import speech_test_database_url

PROJECT = Path(__file__).resolve().parents[1]
PREVIOUS = "sp_0016_evaluation_jobs"
HEAD = "sp_0017_analysis_storage"
OTHER_CALL_ID = uuid.UUID(int=201)
OTHER_TRANSCRIPT_ID = uuid.UUID(int=202)
NEW_TABLES = {"speech_call_analyses", "speech_analysis_references"}
DATABASE_URL = speech_test_database_url()


def _config() -> Config:
    """Вернуть дерево проекта без чтения .env и настройки глобального логирования."""
    config = Config()
    config.set_main_option("script_location", str(PROJECT / "alembic"))
    return config


def _migrate(connection: Connection, revision: str, downgrade: bool = False) -> None:
    """Применить настоящее дерево на соединении вызывающего без своего commit."""
    config = _config()
    config.attributes["connection"] = connection
    action = command.downgrade if downgrade else command.upgrade
    action(config, revision)


@dataclass
class Database:
    """Только изолированная схема тестовой БД; public не входит в search_path."""

    engine: AsyncEngine
    sessions: async_sessionmaker

    async def migrate(self, revision: str = "head", *, downgrade: bool = False) -> None:
        """Применить цепочку в отдельной транзакции и дождаться commit."""
        async with self.engine.begin() as connection:
            await connection.run_sync(_migrate, revision, downgrade)

    async def old_rows(self) -> dict[str, list[Any]]:
        """Снять данные всех прежних таблиц, чтобы проверить отсутствие переписывания."""
        async with self.engine.connect() as connection:
            names = await connection.run_sync(lambda value: inspect(value).get_table_names())
            result = {}
            for name in sorted(set(names) - NEW_TABLES - {"alembic_version"}):
                result[name] = list(
                    (
                        await connection.execute(
                            text(
                                f'SELECT to_jsonb(row) FROM "{name}" AS row '
                                "ORDER BY to_jsonb(row)::text"
                            )
                        )
                    ).scalars()
                )
            return result


async def _seed(database: Database) -> None:
    """Сохранить прежний анализ, тексты и задание до добавления новой схемы."""
    async with database.sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO speech_call_reasons "
                "(code, label, hint, sort_order, is_active, created_at, updated_at) "
                "VALUES ('K05', 'Нет связи', '', 0, TRUE, :now, :now)"
            ),
            {"now": NOW},
        )
        session.add(
            SpeechClassifierNode(
                id=NODE_ID,
                parent_id=None,
                level=1,
                label="Новая тема",
                key="новая тема",
                source="file",
            )
        )
        await session.flush()
        for call_id in (CALL_ID, OTHER_CALL_ID):
            session.add(
                SpeechCall(
                    id=call_id,
                    session_id=str(call_id),
                    started_at=NOW,
                    ended_at=NOW,
                    duration_s=0,
                    processing_status="done",
                    analysis_status="done",
                    sentiment="neutral",
                    summary="Старое резюме.",
                    ai_node_id=NODE_ID,
                    call_reason="Прежняя причина.",
                    call_reason_code="K05",
                    segment="individual",
                    segment_source="llm",
                    product="broadband",
                    product_source="project",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        await session.flush()
        for call_id, transcript_id in (
            (CALL_ID, TRANSCRIPT_ID),
            (OTHER_CALL_ID, OTHER_TRANSCRIPT_ID),
        ):
            session.add(
                SpeechTranscript(
                    id=transcript_id,
                    call_id=call_id,
                    engine="whisper",
                    model_name="existing-model",
                    role_method="none",
                    created_at=NOW,
                )
            )
        session.add(
            SpeechJob(
                id=uuid.UUID(int=107),
                call_id=CALL_ID,
                kind=JobKind.ANALYZE,
                lane=JobLane.DAILY,
                status=JobStatus.DONE,
                attempts=1,
                max_attempts=3,
                finished_at=NOW,
                payload={"existing_receipt": {"version": "1.0"}},
            )
        )


@pytest.fixture
async def previous_db() -> AsyncIterator[Database]:
    """Создать свою схему на sp_0016, посеять старые данные и удалить её после теста."""
    if not DATABASE_URL:
        pytest.skip("SPEECH_POSTGRES_TEST_URL is required for analysis migration proofs")
    name = "speech_analysis_test_" + uuid.uuid4().hex
    owner = create_async_engine(DATABASE_URL, hide_parameters=True)
    engine = create_async_engine(
        DATABASE_URL, hide_parameters=True, connect_args={"server_settings": {"search_path": name}}
    )
    created = False
    try:
        async with owner.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{name}"'))
            created = True
        database = Database(engine, async_sessionmaker(engine, expire_on_commit=False))
        await database.migrate(PREVIOUS)
        await _seed(database)
        yield database
    finally:
        await engine.dispose()
        if created:
            async with owner.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        await owner.dispose()


@pytest.fixture
async def db(previous_db: Database) -> Database:
    """Поднять подготовленную предыдущую схему до новой вершины."""
    await previous_db.migrate()
    return previous_db


async def _reference(db: Database) -> None:
    """Записать один проверенный снимок, на который ссылается тестовый результат."""
    payload = references()
    async with db.sessions.begin() as session:
        session.add(
            SpeechAnalysisReference(
                id=REFERENCE_ID,
                schema_version=1,
                fingerprint=payload.fingerprint(),
                payload=payload.model_dump(mode="json"),
                created_at=NOW,
            )
        )


def _row(**changes: Any) -> SpeechCallAnalysis:
    """Преобразовать согласованный DTO в ORM, оставив SQL UUID и JSONB своими типами."""
    value = analysis()
    data = value.model_dump()
    encoded = value.model_dump(mode="json")
    for field in ("analysis_provenance", "reason_provenance"):
        data[field] = encoded[field]
    return SpeechCallAnalysis(**{**data, **changes})


async def _insert(db: Database, **changes: Any) -> None:
    """Зафиксировать тестовую запись либо дать ограничению БД откатить всю транзакцию."""
    async with db.sessions.begin() as session:
        session.add(_row(**changes))


def test_migration_has_one_head_and_follows_the_previous_revision() -> None:
    """Новая ревизия продолжает единственную цепочку, а readiness требует её вершину."""
    scripts = ScriptDirectory.from_config(_config())
    assert scripts.get_heads() == [HEAD]
    revision = scripts.get_revision(HEAD)
    assert revision is not None and revision.down_revision == PREVIOUS
    assert SPEECH_REQUIRED_SCHEMA_REVISION == HEAD


async def test_upgrade_downgrade_and_reupgrade_preserve_all_old_rows(previous_db) -> None:
    """Реальная цепочка добавляет пустые таблицы, а обратный шаг не меняет прежние данные."""
    before = await previous_db.old_rows()
    await previous_db.migrate()
    assert await previous_db.old_rows() == before
    async with previous_db.engine.connect() as connection:
        for name in NEW_TABLES:
            assert await connection.scalar(text(f'SELECT count(*) FROM "{name}"')) == 0
    await _reference(previous_db)
    await _insert(previous_db)
    await previous_db.migrate(PREVIOUS, downgrade=True)
    assert await previous_db.old_rows() == before
    async with previous_db.engine.connect() as connection:
        tables = await connection.run_sync(lambda value: inspect(value).get_table_names())
        assert not NEW_TABLES.intersection(tables)
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == PREVIOUS
    await previous_db.migrate()
    assert await previous_db.old_rows() == before


async def test_failed_outer_transaction_rolls_back_the_whole_migration(previous_db) -> None:
    """Прерывание транзакции не оставляет полусозданных таблиц или сменённой ревизии."""
    async with previous_db.engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(_migrate, "head")
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        await transaction.rollback()
    async with previous_db.engine.connect() as connection:
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == PREVIOUS
        assert not NEW_TABLES.intersection(
            await connection.run_sync(lambda value: inspect(value).get_table_names())
        )


async def test_migrated_schema_matches_the_new_orm(db) -> None:
    """Типы, FK, уникальности, индексы и defaults миграции соответствуют ORM."""
    async with db.engine.connect() as connection:

        def compare(value: Connection) -> list[Any]:
            context = MigrationContext.configure(
                value,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": lambda obj, name, kind, reflected, other: (
                        kind != "table" or name in NEW_TABLES
                    ),
                },
            )
            return list(compare_metadata(context, SpeechCallAnalysis.metadata))

        assert await connection.run_sync(compare) == []


async def test_orm_roundtrip_and_only_one_current_result(db) -> None:
    """JSONB остаётся типизированным, SQL NULL не превращается в JSON null, дубль запрещён."""
    await _reference(db)
    await _insert(db)
    async with db.sessions() as session:
        stored = (await session.scalars(select(SpeechCallAnalysis))).one()
        data = {
            column.name: getattr(stored, column.name)
            for column in SpeechCallAnalysis.__table__.columns
        }
        from src.application.dto.analysis_storage import StoredCallAnalysis

        assert StoredCallAnalysis.model_validate(data) == analysis()
        assert stored.ai_suggestion is None
    with pytest.raises(IntegrityError, match="uq_speech_call_analysis_call"):
        await _insert(db, id=uuid.uuid4())


async def test_foreign_transcript_cannot_be_attached_to_the_call(db) -> None:
    """Составной FK отвергает существующую расшифровку другого звонка."""
    await _reference(db)
    with pytest.raises(IntegrityError, match="fk_speech_call_analysis_transcript"):
        await _insert(db, transcript_id=OTHER_TRANSCRIPT_ID)


@pytest.mark.parametrize("parent", ["speech_calls", "speech_transcripts"])
async def test_removing_call_or_transcript_removes_only_its_current_result(db, parent) -> None:
    """Старый результат не переживает свой текст; второй звонок и снимок остаются."""
    await _reference(db)
    await _insert(db)
    async with db.engine.begin() as connection:
        identifier = CALL_ID if parent == "speech_calls" else TRANSCRIPT_ID
        await connection.execute(text(f'DELETE FROM "{parent}" WHERE id = :id'), {"id": identifier})
        assert await connection.scalar(text("SELECT count(*) FROM speech_call_analyses")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM speech_analysis_references")) == 1
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM speech_calls WHERE id = :id"), {"id": OTHER_CALL_ID}
            )
            == 1
        )


async def test_finished_job_can_be_pruned_without_erasing_analysis(db) -> None:
    """Происхождение не связывает срок жизни результата со сроком очереди."""
    await _reference(db)
    await _insert(db)
    async with db.engine.begin() as connection:
        await connection.execute(text("DELETE FROM speech_jobs"))
        assert await connection.scalar(text("SELECT count(*) FROM speech_call_analyses")) == 1


async def test_used_reference_cannot_be_removed_or_duplicated(db) -> None:
    """FK сохраняет документ происхождения, уникальность защищает переиспользуемый снимок."""
    await _reference(db)
    await _insert(db)
    with pytest.raises(IntegrityError):
        async with db.engine.begin() as connection:
            await connection.execute(text("DELETE FROM speech_analysis_references"))
    with pytest.raises(IntegrityError, match="uq_speech_analysis_reference_hash"):
        async with db.sessions.begin() as session:
            payload = references()
            session.add(
                SpeechAnalysisReference(
                    id=uuid.uuid4(),
                    schema_version=1,
                    fingerprint=payload.fingerprint(),
                    payload=payload.model_dump(mode="json"),
                )
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 0},
        {"binding_kind": "unknown"},
        {"sentiment": "unknown"},
        {"llm_segment": "unknown"},
        {"llm_product": "unknown"},
        {"call_reason": "x" * 201},
        {"ai_path": {}},
        {"ai_suggestion": {}},
        {"analysis_provenance": []},
        {"analysis_provenance": {}},
        {"analysis_provenance": {"origin": "invented"}},
        {"analysis_provenance": {"origin": "legacy_import"}},
        {"analysis_reference_id": None},
        {"reason_reference_id": None},
        {"reason_provenance": None},
        {"analysis_provenance": None, "reason_provenance": None},
        {"updated_at": NOW.replace(year=2025)},
    ],
)
async def test_database_rejects_invalid_shape_even_without_pydantic(db, changes) -> None:
    """Обход DTO не позволяет записать неподтверждённый или повреждённый набор полей."""
    await _reference(db)
    with pytest.raises(IntegrityError):
        await _insert(db, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 0},
        {"fingerprint": "F" * 64},
        {"fingerprint": "0" * 63},
        {"payload": []},
        {"payload": None},
    ],
)
async def test_database_rejects_invalid_reference_shape(db, changes) -> None:
    """БД самостоятельно проверяет версию, хеш и объектный тип документа."""
    payload = references()
    values = {
        "id": REFERENCE_ID,
        "schema_version": 1,
        "fingerprint": payload.fingerprint(),
        "payload": payload.model_dump(mode="json"),
        **changes,
    }
    with pytest.raises(IntegrityError):
        async with db.sessions.begin() as session:
            session.add(SpeechAnalysisReference(**values))


async def test_old_analysis_writer_still_uses_existing_columns(db) -> None:
    """Добавочная схема не переключает действующий SQL на пустые новые таблицы."""
    async with db.sessions.begin() as session:
        await store_result(
            session,
            CALL_ID,
            AnalyzeWork(
                node_id=NODE_ID,
                path=("Новая тема",),
                sentiment="positive",
                summary="Новый прежний путь.",
            ),
        )
    async with db.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT summary FROM speech_calls WHERE id = :id"), {"id": CALL_ID}
            )
            == "Новый прежний путь."
        )
        assert (
            await connection.scalar(
                text("SELECT analysis_status FROM speech_calls WHERE id = :id"), {"id": CALL_ID}
            )
            == AnalysisStatus.DONE
        )
        assert await connection.scalar(text("SELECT count(*) FROM speech_call_analyses")) == 0
