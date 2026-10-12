"""Ожидаемый отказ проверки загруженной записи без HTTP-зависимостей."""

from enum import StrEnum


class AudioProblem(StrEnum):
    """Причины отклонения файла до отправки в распознавание."""

    EMPTY = "empty_audio_file"
    TOO_LARGE = "audio_file_too_large"
    UNSUPPORTED = "unsupported_audio_type"


class AudioValidationError(ValueError):
    """Файл не отвечает ограничениям загрузки."""

    def __init__(self, reason: AudioProblem, message: str) -> None:
        """Принять устойчивую причину и описание отказа для адаптера входа."""
        self.reason = reason
        super().__init__(message)
