"""Сценарий распознавания загруженной записи."""

from src.application.dto.audio import AudioUpload
from src.application.dto.transcription import (
    TranscriptionOutcome,
    TranscriptionRequest,
    TranscriptionResult,
)
from src.application.ports.transcription import TranscriptionBackend


class TranscribeAudio:
    """Передать валидированную запись внедрённому движку распознавания."""

    def __init__(self, backend: TranscriptionBackend) -> None:
        """Принять реализацию порта; сетевые ресурсы создаёт сборка приложения."""
        self._backend = backend

    async def execute(
        self,
        audio: AudioUpload,
        *,
        language: str | None,
        request_id: str | None = None,
    ) -> tuple[TranscriptionResult, TranscriptionOutcome]:
        """Распознать запись и вернуть текст с метаданными одной операции.

        Args:
            audio: Валидированное содержимое записи и её формат.
            language: Язык записи или None для определения движком.
            request_id: Идентификатор запроса для журнала операции.

        Returns:
            Результат распознавания и нормализованные метаданные вызова.

        Raises:
            TranscriptionError: Если движок не настроен либо распознавание отказало.

        Side Effects:
            Вызывает внедрённый порт распознавания.
        """
        request = TranscriptionRequest(
            filename=audio.filename,
            content=audio.content,
            mime_type=audio.mime_type,
            audio_format=audio.audio_format,
            size_bytes=audio.size_bytes,
            language=language,
        )
        return await self._backend.transcribe(request, request_id=request_id)
