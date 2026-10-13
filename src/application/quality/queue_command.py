"""Ручная постановка оценки через локальный сценарий бэкенда."""

from src.application.dto.maintenance import QualityQueueReport, QualityQueueRequest
from src.application.dto.quality_batch import RunEvaluation
from src.application.ports.job_processing import EvaluationSchedulingApplication


class QueuedEvaluationService:
    """Выбрать охват без LLM; оценку выполняет надёжный CPU-процесс."""

    def __init__(self, application: EvaluationSchedulingApplication) -> None:
        """Внедрить порт локальной постановки; доступ к своему HTTP API не нужен."""
        self._application = application

    async def run(self, query: RunEvaluation) -> QualityQueueReport:
        """Проверить охват, поставить задания при apply и вернуть количество."""
        return await self._application.enqueue(QualityQueueRequest(selection=query))
