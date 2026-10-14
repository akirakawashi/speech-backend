"""Единая запись актуального анализа и неизменяемых снимков в транзакции вызывающего."""

from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.analysis_storage import (
    AnalysisReferencePayload,
    AnalysisReferenceSnapshot,
    StoredCallAnalysis,
)
from src.infrastructure.persistence.models.analysis import (
    SpeechAnalysisReference,
    SpeechCallAnalysis,
)


class AnalysisResultStore:
    """Читать и писать DTO под уже взятой блокировкой звонка без собственного commit."""

    def __init__(self, session: AsyncSession) -> None:
        """Принять сессию текущей транзакции полного анализа либо причины."""
        self._session = session

    async def current(self, call_id: UUID) -> StoredCallAnalysis | None:
        """Вернуть проверенную текущую запись звонка либо None; данные не изменять."""
        row = await self._session.scalar(
            select(SpeechCallAnalysis).where(SpeechCallAnalysis.call_id == call_id)
        )
        return StoredCallAnalysis.model_validate(row, from_attributes=True) if row else None

    async def reference(self, payload: AnalysisReferencePayload) -> UUID:
        """Сохранить снимок либо переиспользовать существующий по версии и SHA-256.

        Args:
            payload: Проверенный снимок реально использованных справочников.

        Returns:
            UUID неизменяемого снимка. Конкурентная вставка не переписывает payload.

        Raises:
            ValidationError: Если ранее сохранённый документ не соответствует хешу.
        """
        fingerprint = payload.fingerprint()
        await self._session.execute(
            insert(SpeechAnalysisReference)
            .values(
                id=uuid4(),
                schema_version=1,
                fingerprint=fingerprint,
                payload=payload.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=["schema_version", "fingerprint"])
        )
        row = (
            await self._session.scalars(
                select(SpeechAnalysisReference).where(
                    SpeechAnalysisReference.schema_version == 1,
                    SpeechAnalysisReference.fingerprint == fingerprint,
                )
            )
        ).one()
        snapshot = AnalysisReferenceSnapshot.model_validate(row, from_attributes=True)
        return snapshot.id

    async def put(self, result: StoredCallAnalysis) -> None:
        """Атомарно заменить строку результата проверенным DTO без записи проекции звонка.

        Args:
            result: Полный или частичный результат, сформированный под lock звонка.

        Side Effects:
            Вставляет либо обновляет одну строку; commit выполняет владелец сессии.
        """
        values = result.model_dump()
        for name in ("analysis_provenance", "reason_provenance"):
            provenance = getattr(result, name)
            values[name] = provenance.model_dump(mode="json") if provenance is not None else None
        statement = insert(SpeechCallAnalysis).values(**values)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["call_id"],
                set_={
                    name: getattr(statement.excluded, name)
                    for name in values
                    if name not in {"id", "call_id", "created_at"}
                },
            )
        )


async def remove_analysis(session: AsyncSession, call_id: UUID) -> None:
    """Снять текущий результат в транзакции replay/ASR; справочники не удалять."""
    await session.execute(
        text("DELETE FROM speech_call_analyses WHERE call_id = :call_id"), {"call_id": call_id}
    )
