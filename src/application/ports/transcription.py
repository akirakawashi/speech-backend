"""Операция распознавания, необходимая сценарию приложения."""

from typing import Protocol

from src.application.dto.transcription import (
    TranscriptionOutcome,
    TranscriptionRequest,
    TranscriptionResult,
)


class TranscriptionBackend(Protocol):
    """Распознать подготовленное аудио без раскрытия транспорта сценарию."""

    async def transcribe(
        self, request: TranscriptionRequest, *, request_id: str | None = None
    ) -> tuple[TranscriptionResult, TranscriptionOutcome]:
        """Принять аудио DTO и вернуть текст с метаданными вызова.

        Raises:
            TranscriptionError: При ненастроенном движке или отказе распознавания.
        """
        ...
