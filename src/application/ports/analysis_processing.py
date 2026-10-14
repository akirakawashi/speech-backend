"""Порты согласованного чтения и атомарного завершения анализа без типов SQLAlchemy."""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from src.application.dto.analysis_processing import AnalysisClaim, AnalysisLease, AnalysisSnapshot
from src.application.dto.analysis_writes import SaveAnalysis
from src.application.dto.calls import JobErrorClass, JobLane, JobStatus
from src.application.ports.queue import ExpiredJob


class AnalysisProcessingTransaction(Protocol):
    """Короткая транзакция результата или отказа; сетевых операций внутри нет."""

    async def lock(self, job_id: UUID, call_id: UUID) -> AnalysisLease | None:
        """Заблокировать звонок и задания; вернуть текущую аренду или None."""
        ...

    async def database_time(self) -> datetime:
        """Вернуть фактическое время базы после получения блокировок."""
        ...

    async def snapshot(self, call_id: UUID) -> AnalysisSnapshot | None:
        """Удерживать источники снимка до commit; конфликт даёт AnalysisProcessingBusy."""
        ...

    async def save(self, command: SaveAnalysis) -> None:
        """Записать анализ, происхождение, прежнюю проекцию и роли одной транзакцией.

        Args:
            command: Проверенный захват, исходный снимок и пригодное вычисление.

        Side Effects:
            Сохраняет новые таблицы и прежние поля вместе; снимает зависимые оценки.
        """
        ...

    async def complete(self, claim: AnalysisClaim) -> bool:
        """Завершить ту же живую аренду; False требует rollback всех результатов."""
        ...

    async def fail(
        self, claim: AnalysisClaim, error_class: JobErrorClass, *, terminal: bool
    ) -> JobStatus | None:
        """Записать отказ захвата и назначить повтор; None означает потерю права изменения."""
        ...

    async def mark_failed(self, call_id: UUID) -> None:
        """Сделать анализ неудавшимся, сохранив расшифрованный звонок доступным."""
        ...


class AnalysisProcessingRepository(Protocol):
    """Загрузить вход до LLM и открыть заменяемую атомарную транзакцию после него."""

    async def load(self, claim: AnalysisClaim) -> AnalysisSnapshot | None:
        """Проверить захват, получить снимок и закрыть чтение до внешнего запроса."""
        ...

    def transaction(self) -> AbstractAsyncContextManager[AnalysisProcessingTransaction]:
        """Открыть транзакцию; ошибка выхода не считается подтверждением обработки."""
        ...

    async def defer(self, claim: AnalysisClaim) -> bool:
        """Отложить только этот живой захват без расхода попытки при смене входа/блокировке."""
        ...

    async def unprojected_failures(self, lanes: Sequence[JobLane]) -> Sequence[ExpiredJob]:
        """Найти dead после истечения lease, чей отказ ещё не отражён в звонке."""
        ...
