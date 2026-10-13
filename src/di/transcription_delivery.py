"""Сборка приёма распознавания: служебный ключ и транзакционный порт PostgreSQL."""

from typing import Annotated

from fastapi import Depends, Header

from src.application.transcription_delivery import TranscriptionDeliveryService
from src.application.worker_access import authorize_worker
from src.infrastructure.persistence.session import get_speech_ms_sessionmaker
from src.infrastructure.persistence.transcription_delivery import (
    PostgresTranscriptionReceptionRepository,
)
from src.setting.config import Settings, get_settings


async def transcription_delivery_application(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> TranscriptionDeliveryService:
    """Проверить ключ до сборки базы и внедрить порт без открытия соединения."""
    authorize_worker(settings.speech_worker_api_token, authorization)
    return TranscriptionDeliveryService(
        PostgresTranscriptionReceptionRepository(get_speech_ms_sessionmaker(settings)),
        worker_token=settings.speech_worker_api_token,
    )
