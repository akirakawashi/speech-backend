"""Допуск к оценке готовой расшифровки скриптом продаж."""

from datetime import datetime

from src.domain.calls.contracts import ProcessingStatus
from src.domain.quality.script import CURRENT_SCRIPT


class EvaluationInputUnavailable(ValueError):
    """У звонка нет входа нужного источника для оценки продаж."""


class EvaluationInputNotReady(ValueError):
    """Предшествующий этап или время версии входа требуют отсрочки."""


def require_evaluation_input(
    *,
    source: str,
    status: ProcessingStatus,
    created_at: datetime,
    read_at: datetime,
    other_processing: bool,
) -> None:
    """Разрешить только готовый Телфин после времени транскрипта; иначе нормализованный отказ."""
    if source != CURRENT_SCRIPT.source.value:
        raise EvaluationInputUnavailable("evaluation_source_is_unavailable")
    if status is not ProcessingStatus.DONE or read_at < created_at or other_processing:
        raise EvaluationInputNotReady("evaluation_input_is_not_ready")
