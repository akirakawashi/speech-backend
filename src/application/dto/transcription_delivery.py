"""Служебный контракт 1.0 для приёма распознавания и подтверждения после commit."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.application.dto.pipeline import Reply, TranscribeWork
from src.domain.calls.transcription_delivery import (
    ReceivingCall as ReceivingCall,
    TranscriptionClaim as TranscriptionClaim,
    TranscriptionLease as TranscriptionLease,
    TranscriptionSource as TranscriptionSource,
    validate_asr_attribution,
)


class ReceivedReply(Reply):
    """Реплика доставки с ограничениями индексов и таймкодов базы."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    seq: int = Field(strict=True, ge=0, le=2147483647)
    start_ms: int = Field(strict=True, ge=0, le=2147483647)
    end_ms: int = Field(strict=True, ge=0, le=2147483647)

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        """Не принимать интервал, у которого конец раньше начала."""
        if self.end_ms < self.start_ms:
            raise ValueError("reply interval is reversed")
        return self


class ReceivedTranscription(TranscribeWork):
    """Распознавание для записи; поля анализа и роли, полученные LLM, не принимаются."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    engine: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    model_digest: str | None = Field(default=None, max_length=128)
    language: str = Field(min_length=1, max_length=16)
    duration_s: float | None = Field(ge=0, lt=10000000, allow_inf_nan=False)
    replies: tuple[ReceivedReply, ...] = Field(max_length=50000, repr=False)

    @model_validator(mode="after")
    def valid_asr_result(self) -> Self:
        """Проверить последовательность реплик и допустимый метод технической атрибуции."""
        validate_asr_attribution(self.role_method, tuple(reply.speaker for reply in self.replies))
        sequences = [reply.seq for reply in self.replies]
        if sequences != sorted(set(sequences)):
            raise ValueError("reply sequences must be unique and increasing")
        if sum(len(reply.text) for reply in self.replies) > 5000000:
            raise ValueError("transcription text is too large")
        return self


class TranscriptionDeliveryRequest(BaseModel):
    """Результат конкретного захвата и исходное аудио; ключ повтора — job_id/claim_token."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    contract_version: Literal["1.0"] = "1.0"
    claim: TranscriptionClaim
    source: TranscriptionSource
    result: ReceivedTranscription = Field(repr=False)


class TranscriptionReceipt(BaseModel):
    """Стабильное подтверждение сохранения и постановки анализа без ожидания LLM."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    contract_version: Literal["1.0"] = "1.0"
    call_id: UUID
    job_id: UUID
    claim_token: UUID
    attempt: int
    transcript_id: UUID
    analysis_job_id: UUID
    accepted_at: AwareDatetime


class StoredTranscriptionReceipt(BaseModel):
    """Квитанция и хеш канонического запроса; полного текста в метаданных очереди нет."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt: TranscriptionReceipt
