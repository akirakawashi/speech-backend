"""Согласованный вход и команды атомарной записи анализа и частичной причины."""

import hashlib
import json
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.application.dto.analysis_computation import AnalysisComputation
from src.application.dto.analysis_processing import AnalysisClaim, AnalysisSnapshot
from src.application.dto.model_execution import ModelExecutionMetadata
from src.application.dto.pipeline_input import AnalysisSegment
from src.application.dto.reasons import CallReason, ReasonAnswer
from src.domain.calls.contracts import Product, Segment, Sentiment


class TranscriptState(BaseModel):
    """Идентичность и состояние реплик для отпечатка transcript-state/1."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    transcript_id: UUID
    transcript_created_at: AwareDatetime
    role_method: str | None
    segments: tuple[AnalysisSegment, ...] = Field(repr=False)

    def fingerprint(self) -> str:
        """Хешировать идентичность, метод ролей и упорядоченные реплики без справочников."""
        value = self.model_dump(mode="json")
        value["segments"] = sorted(value["segments"], key=lambda row: row["seq"])
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AnalysisVersion(BaseModel):
    """Ожидаемая строка результата; UUID защищает от удаления и повторного создания."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    revision: int = Field(ge=1, strict=True)


class LegacyAnalysisFields(BaseModel):
    """Наблюдаемая прежняя проекция; проектные измерения сюда не попадают."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    ai_node_id: UUID | None = None
    ai_suggestion: tuple[str, ...] | None = None
    ai_suggestion_node_id: UUID | None = None
    sentiment: Sentiment | None = None
    summary: str | None = Field(default=None, repr=False)
    llm_segment: Segment | None = None
    llm_product: Product | None = None

    @property
    def has_full_component(self) -> bool:
        """Вернуть наличие хотя бы одного прежнего поля полного анализа."""
        return any(value is not None for value in self.model_dump().values())


class ReasonSnapshot(BaseModel):
    """Вход причины, прежняя проекция и версия результата одной короткой транзакции."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    call_id: UUID
    transcript: TranscriptState = Field(repr=False)
    reasons: tuple[CallReason, ...]
    expected: AnalysisVersion | None
    legacy: LegacyAnalysisFields = Field(repr=False)
    has_reason: bool

    @property
    def fingerprint(self) -> str:
        """Хешировать вход reason-snapshot/1; версия результата проверяется отдельно."""
        value = {
            "transcript": self.transcript.model_dump(mode="json"),
            "reasons": sorted(
                (reason.model_dump(mode="json") for reason in self.reasons),
                key=lambda row: row["code"],
            ),
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ReasonComputation(BaseModel):
    """Проверенная причина и факты последнего ответа, включая ответ без кода."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    answer: ReasonAnswer = Field(repr=False)
    execution: ModelExecutionMetadata
    model_call_index: int = Field(ge=1, strict=True)
    rules_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scenario(self) -> Self:
        """Принять только сценарий причины; иной сценарий вызывает ValueError."""
        if self.execution.scenario != "reason":
            raise ValueError("reason computation requires the reason scenario")
        return self


class SaveAnalysis(BaseModel):
    """Сохранить вычисление только для сверенного снимка и живого захвата."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    claim: AnalysisClaim
    source: AnalysisSnapshot = Field(repr=False)
    result: AnalysisComputation = Field(repr=False)

    @model_validator(mode="after")
    def validate_call(self) -> Self:
        """Сверить UUID задания и снимка; несогласованность вызывает ValueError."""
        if self.claim.call_id != self.source.data.call.id:
            raise ValueError("analysis claim and source belong to different calls")
        return self


class SaveReason(BaseModel):
    """Дополнить причину только при неизменном входе и ожидаемой версии результата."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source: ReasonSnapshot = Field(repr=False)
    result: ReasonComputation = Field(repr=False)

    @model_validator(mode="after")
    def validate_code(self) -> Self:
        """Запретить запись пустого кода или кода вне отправленного справочника."""
        if self.result.answer.code not in {reason.code for reason in self.source.reasons}:
            raise ValueError("reason code is absent from the input reference")
        return self
