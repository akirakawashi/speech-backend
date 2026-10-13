"""Служебный HTTP-приём распознавания без вызова моделей и ожидания анализа."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from src.application.dto.transcription_delivery import (
    TranscriptionDeliveryRequest,
    TranscriptionReceipt,
)
from src.application.transcription_delivery import TranscriptionDeliveryService

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


async def transcription_delivery_dep(request: Request) -> TranscriptionDeliveryService:
    """Получить сценарий из DI; отсутствие привязки означает ошибку сборки."""
    raise RuntimeError("transcription delivery dependency is not installed")


@router.post("/transcription-results", response_model=TranscriptionReceipt)
async def receive_transcription_result(
    command: TranscriptionDeliveryRequest,
    service: Annotated[TranscriptionDeliveryService, Depends(transcription_delivery_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> TranscriptionReceipt:
    """Передать DTO сценарию и вернуть подтверждение сохранения после commit."""
    return await service.receive_authorized(command, authorization)
