"""Правила допустимости результата распознавания относительно выданного задания."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.domain.calls.contracts import (
    AudioStatus,
    JobKind,
    JobLane,
    JobStatus,
    ProcessingStatus,
    RoleMethod,
    Speaker,
)


class TranscriptionClaim(BaseModel):
    """Идентификаторы звонка, задания и конкретного захвата воркером."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    call_id: UUID
    job_id: UUID
    claim_token: UUID
    worker_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(strict=True, ge=1, le=32767)


class TranscriptionSource(BaseModel):
    """Снимок сохранённого аудио, по которому воркер выполнил распознавание."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    object_key: str = Field(min_length=1, max_length=1024)
    channels: int | None = Field(default=None, strict=True, ge=1)


class ReceivingCall(BaseModel):
    """Текущее состояние звонка под блокировкой приёма."""

    model_config = ConfigDict(frozen=True)
    id: UUID
    audio_status: AudioStatus
    audio_object_key: str | None
    audio_channels: int | None
    processing_status: ProcessingStatus


class TranscriptionLease(BaseModel):
    """Снимок задания и срока его аренды; данные прочитаны из очереди, а не из запроса."""

    model_config = ConfigDict(frozen=True)
    id: UUID
    call_id: UUID
    kind: JobKind
    status: JobStatus
    lane: JobLane
    priority: int
    attempts: int
    locked_by: str | None
    claim_token: UUID | None
    lease_until: AwareDatetime | None


class StaleTranscriptionResult(ValueError):
    """Результат не соответствует действующему заданию, аренде или сохранённому аудио."""


def validate_asr_attribution(role_method: RoleMethod, speakers: Sequence[Speaker]) -> None:
    """Проверить метод и роли распознавания; текстовую атрибуцию применяет только бэкенд.

    Принимает объявленный метод и роли реплик. LLM-метод либо известная роль
    без измерения вызывает ValueError; допустимый набор возвращает None.
    """
    if role_method is RoleMethod.LLM_ATTRIBUTION:
        raise ValueError("LLM attribution is not a transcription result")
    if role_method is RoleMethod.NONE and any(
        speaker is not Speaker.UNKNOWN for speaker in speakers
    ):
        raise ValueError("unmeasured roles must be unknown")


def validate_transcription_claim(
    claim: TranscriptionClaim,
    source: TranscriptionSource,
    call: ReceivingCall,
    job: TranscriptionLease,
    *,
    now: datetime,
) -> None:
    """Проверить захват и исходное аудио либо отказать без содержимого запроса.

    Принимает заявленные идентификаторы и снимки из базы, включая её время.
    Просроченный, заменённый или неподходящий захват вызывает
    StaleTranscriptionResult. Проверка не продлевает аренду и не изменяет данные.
    """
    if (
        call.id != claim.call_id
        or job.id != claim.job_id
        or job.call_id != claim.call_id
        or job.kind is not JobKind.TRANSCRIBE
        or job.status is not JobStatus.RUNNING
        or job.locked_by != claim.worker_id
        or job.attempts != claim.attempt
        or job.claim_token != claim.claim_token
        or job.lease_until is None
        or job.lease_until <= now
    ):
        raise StaleTranscriptionResult("transcription_claim_is_stale")
    if (
        call.audio_status is not AudioStatus.STORED
        or call.audio_object_key != source.object_key
        or call.audio_channels != source.channels
        or call.processing_status not in (ProcessingStatus.QUEUED, ProcessingStatus.TRANSCRIBING)
    ):
        raise StaleTranscriptionResult("transcription_input_has_changed")
