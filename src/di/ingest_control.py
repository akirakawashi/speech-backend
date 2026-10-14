"""Сборка служебного управления получением аудио."""

from typing import Annotated

from fastapi import Depends, Header

from src.application.ingestion.admission import IngestControlService
from src.application.worker_access import authorize_worker
from src.infrastructure.persistence.ingest_control import PostgresIngestControlRepository
from src.infrastructure.persistence.session import get_speech_ms_sessionmaker
from src.setting.config import Settings, get_settings


def ingest_control_application(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> IngestControlService:
    """Проверить ключ до создания пула и внедрить порт коротких транзакций."""
    authorize_worker(settings.speech_worker_api_token, authorization)
    return IngestControlService(
        PostgresIngestControlRepository(get_speech_ms_sessionmaker(settings)),
        worker_token=settings.speech_worker_api_token,
    )
