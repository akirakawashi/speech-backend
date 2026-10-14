"""Нормализованный отказ обработки задания без текста разговора и ответа провайдера."""

from src.domain.calls.contracts import JobErrorClass


class StageError(Exception):
    """Передать очереди класс отказа; технические детали не предназначены для журнала."""

    def __init__(self, error_class: JobErrorClass, message: str) -> None:
        """Принять класс и диагностическое сообщение; сохранить класс для политики повторов.

        Args:
            error_class: Устойчивый класс отказа задания.
            message: Сообщение вызывающего; не включается в безопасный журнал автоматически.
        """
        super().__init__(message)
        self.error_class = error_class
