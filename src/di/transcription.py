"""Сборка сценария распознавания и его HTTP-адаптера."""

import httpx

from src.application.dto.audio import AudioUpload
from src.application.dto.transcription import TranscriptionOutcome, TranscriptionResult
from src.application.transcription.service import TranscribeAudio
from src.infrastructure.transcription.service import HttpTranscriptionBackend
from src.setting.config import Settings
from src.setting.transcription import resolve_transcription_config


def build_transcription(
    settings: Settings, *, http_client: httpx.AsyncClient | None = None
) -> TranscribeAudio:
    """По настройкам собрать сценарий с адаптером, использующим переданный клиент.

    Returns:
        Сценарий; отсутствие настроенного движка обрабатывается при его вызове.
    """
    backend = HttpTranscriptionBackend(
        resolve_transcription_config(settings), http_client=http_client
    )
    return TranscribeAudio(backend)


async def transcribe_audio(
    *,
    settings: Settings,
    audio: AudioUpload,
    language: str | None,
    request_id: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[TranscriptionResult, TranscriptionOutcome]:
    """Собрать и выполнить распознавание записи, вернуть результат и метаданные.

    Args:
        settings: Настройки выбора и транспорта движка.
        audio: Валидированная запись.
        language: Язык записи или None.
        request_id: Идентификатор для журнала.
        http_client: Клиент вызывающего; None означает клиент адаптера на один вызов.

    Raises:
        TranscriptionError: При отказе распознавания с нормализованной причиной.
    """
    return await build_transcription(settings, http_client=http_client).execute(
        audio, language=language, request_id=request_id
    )
