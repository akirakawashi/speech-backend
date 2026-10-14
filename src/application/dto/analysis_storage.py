"""Проверенные данные актуального анализа и происхождения его компонентов."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.application.dto.model_execution import Sha256 as Sha256
from src.application.dto.pipeline_input import ClassifierRecord
from src.domain.calls.classifier import CLASSIFIER_DEPTH, LABEL_LIMIT, label_key
from src.domain.calls.contracts import (
    AnalysisBindingKind as AnalysisBindingKind,
    Product,
    Segment,
    Sentiment,
)
from src.domain.calls.reasons import REASONS_PROMPT_LIMIT, CallReason, prompt_reasons

NonEmptyText = Annotated[str, Field(min_length=1)]
ClassifierPath = Annotated[
    tuple[Annotated[str, Field(max_length=LABEL_LIMIT)], ...], Field(max_length=CLASSIFIER_DEPTH)
]
SuggestionPath = Annotated[
    tuple[Annotated[str, Field(max_length=120)], ...], Field(max_length=CLASSIFIER_DEPTH)
]


class ReferenceClassifierNode(ClassifierRecord):
    """Узел неизменяемого снимка; пустая подпись сохраняет место уровня."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    level: int = Field(ge=1, le=CLASSIFIER_DEPTH, strict=True)
    label: str = Field(max_length=LABEL_LIMIT)
    key: str = Field(max_length=LABEL_LIMIT)

    @model_validator(mode="after")
    def validate_position(self) -> Self:
        """Проверить корень и нормализованный ключ; вернуть узел либо ValueError."""
        if (self.parent_id is None) != (self.level == 1):
            raise ValueError("classifier root and level disagree")
        if self.key != label_key(self.label):
            raise ValueError("classifier key does not match its label")
        return self


class ReferenceCallReason(CallReason):
    """Только явно переданная причина с устойчивым кодом и непустой подписью."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str = Field(pattern=r"^K[0-9]{2}$")
    label: str = Field(min_length=1, max_length=LABEL_LIMIT)


class AnalysisReferencePayload(BaseModel):
    """Справочники конкретного запроса без текста разговора и секретов."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    scenario: Literal["analysis", "reason"]
    classifier: tuple[ReferenceClassifierNode, ...] = ()
    reasons: tuple[ReferenceCallReason, ...] = ()
    reason_reference_state: Literal["sent", "empty", "omitted_too_large"]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Проверить дерево, коды и факт передачи причин; вернуть согласованный снимок.

        Raises:
            ValueError: Если ссылки/ключи повторяются, дерево разорвано или
                содержимое причин не соответствует заявленному состоянию.
        """
        if self.scenario == "reason" and self.classifier:
            raise ValueError("reason request cannot contain a classifier")
        if (self.reason_reference_state == "sent") != bool(self.reasons):
            raise ValueError("reason reference state disagrees with its contents")
        if len({reason.code for reason in self.reasons}) != len(self.reasons):
            raise ValueError("reason codes must be unique")
        if len(prompt_reasons(self.reasons)) > REASONS_PROMPT_LIMIT:
            raise ValueError("sent reason reference exceeds the prompt limit")
        nodes = {node.id: node for node in self.classifier}
        if len(nodes) != len(self.classifier):
            raise ValueError("classifier node identifiers must be unique")
        slots = {(node.parent_id, node.key) for node in self.classifier}
        if len(slots) != len(self.classifier):
            raise ValueError("classifier sibling keys must be unique")
        for node in self.classifier:
            if node.parent_id is not None:
                parent = nodes.get(node.parent_id)
                if parent is None or parent.level + 1 != node.level:
                    raise ValueError("classifier parent is missing or has an invalid level")
        return self

    def fingerprint(self) -> str:
        """Вернуть SHA-256 канонического DTO; порядок строк чтения не влияет на хеш."""
        value = self.model_dump(mode="json")
        value["classifier"] = sorted(value["classifier"], key=lambda row: row["id"])
        value["reasons"] = sorted(value["reasons"], key=lambda row: row["code"])
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AnalysisReferenceSnapshot(BaseModel):
    """Сохранённый снимок со сверенным хешем и версией его канонизации."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    schema_version: Literal[1] = 1
    fingerprint: Sha256
    payload: AnalysisReferencePayload
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_fingerprint(self) -> Self:
        """Отклонить несоответствие документа его хешу; вернуть проверенный снимок."""
        if self.fingerprint != self.payload.fingerprint():
            raise ValueError("reference fingerprint does not match its payload")
        return self


class AnalysisJobEvidence(BaseModel):
    """Идентичность захвата; не FK, поскольку завершённое задание удаляется по сроку."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    job_id: UUID
    claim_token: UUID
    attempt: int = Field(ge=1, strict=True)


class LlmAnalysisProvenance(BaseModel):
    """Происхождение одного компонента из фактически отправленного запроса.

    Метаданные запроса возвращает LLM-порт, захват и отпечаток входа добавляет
    application. Исходного текста, сырых ответов и адресов с секретами здесь нет.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    origin: Literal["llm"] = "llm"
    execution_id: UUID
    scenario: Literal["analysis", "reason"]
    profile: Literal["production", "candidate"]
    requested_model: NonEmptyText
    served_model: NonEmptyText | None = None
    model_digest: NonEmptyText | None = None
    prompt_version: NonEmptyText
    prompt_digest: Sha256
    rules_version: NonEmptyText
    request_fingerprint: Sha256
    temperature: float = Field(ge=0, le=2)
    max_tokens: int = Field(ge=1, le=32768, strict=True)
    transcript_id: UUID
    transcript_created_at: AwareDatetime
    fingerprint_schema: Literal["analysis-snapshot/1", "reason-snapshot/1"]
    source_fingerprint: Sha256
    committed_transcript_fingerprint: Sha256 | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    model_call_index: int = Field(ge=1, strict=True)
    job: AnalysisJobEvidence | None = None

    @model_validator(mode="after")
    def validate_execution(self) -> Self:
        """Сверить сценарий, схему отпечатка и время; вернуть согласованные сведения."""
        expected = f"{self.scenario}-snapshot/1"
        if self.fingerprint_schema != expected:
            raise ValueError("source fingerprint schema disagrees with the scenario")
        if self.finished_at < self.started_at:
            raise ValueError("model response precedes the request")
        return self


class LegacyAnalysisProvenance(BaseModel):
    """Честный перенос прежних полей без выдуманной модели или даты вычисления."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    origin: Literal["legacy_import"] = "legacy_import"
    source: Literal["speech_calls"] = "speech_calls"
    imported_at: AwareDatetime
    unknown_fields: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unknown_fields(self) -> Self:
        """Отклонить повторённые имена неизвестных сведений; вернуть метаданные импорта."""
        if len(set(self.unknown_fields)) != len(self.unknown_fields):
            raise ValueError("unknown fields must be unique")
        return self


AnalysisProvenance = Annotated[
    LlmAnalysisProvenance | LegacyAnalysisProvenance, Field(discriminator="origin")
]


class StoredCallAnalysis(BaseModel):
    """Один актуальный набор результатов, включая независимое дополнение причины."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    call_id: UUID
    transcript_id: UUID
    revision: int = Field(ge=1, strict=True)
    binding_kind: AnalysisBindingKind
    ai_node_id: UUID | None = None
    ai_path: ClassifierPath | None = None
    ai_suggestion: SuggestionPath | None = None
    ai_suggestion_node_id: UUID | None = None
    sentiment: Sentiment | None = None
    summary: str | None = None
    llm_segment: Segment | None = None
    llm_product: Product | None = None
    call_reason: str | None = Field(default=None, max_length=200)
    call_reason_code: str | None = Field(default=None, pattern=r"^K[0-9]{2}$")
    analysis_reference_id: UUID | None = None
    reason_reference_id: UUID | None = None
    analysis_provenance: AnalysisProvenance | None = None
    reason_provenance: AnalysisProvenance | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_components(self) -> Self:
        """Сверить компоненты, происхождение и вход без обращения к БД.

        Returns:
            Проверенный актуальный результат, в том числе только с причиной.

        Raises:
            ValueError: Если новый результат не имеет доказанного входа,
                перепутаны сценарии или частичное дополнение выдаётся за анализ.
        """
        full, reason = self.analysis_provenance, self.reason_provenance
        if full is None and reason is None:
            raise ValueError("analysis must contain at least one component")
        if self.updated_at < self.created_at:
            raise ValueError("analysis update precedes its creation")
        if self.binding_kind is AnalysisBindingKind.VERIFIED and any(
            isinstance(value, LegacyAnalysisProvenance) for value in (full, reason)
        ):
            raise ValueError("legacy input cannot have a verified binding")
        if full is None and any(
            value is not None
            for value in (
                self.ai_node_id,
                self.ai_path,
                self.ai_suggestion,
                self.ai_suggestion_node_id,
                self.sentiment,
                self.summary,
                self.llm_segment,
                self.llm_product,
            )
        ):
            raise ValueError("full analysis fields require their own provenance")
        has_reason = self.call_reason is not None or self.call_reason_code is not None
        if has_reason != (reason is not None):
            raise ValueError("reason fields require matching provenance")
        for provenance, reference in (
            (full, self.analysis_reference_id),
            (reason, self.reason_reference_id),
        ):
            if isinstance(provenance, LlmAnalysisProvenance):
                if provenance.transcript_id != self.transcript_id or reference is None:
                    raise ValueError("computed component requires its transcript and references")
            elif reference is not None:
                raise ValueError("reference requires a computed component")
        if isinstance(full, LlmAnalysisProvenance):
            if full.scenario != "analysis" or self.sentiment is None:
                raise ValueError("full analysis requires the analysis scenario and sentiment")
            if self.summary is not None and len(self.summary) > 300:
                raise ValueError("computed summary exceeds its limit")
            if self.ai_node_id is None and self.ai_path:
                raise ValueError("resolved path requires a classifier node")
            if self.ai_node_id is not None and not self.ai_path:
                raise ValueError("classifier node requires a resolved path")
        if self.ai_suggestion_node_id is not None and not self.ai_suggestion:
            raise ValueError("suggestion node requires its path")
        if isinstance(reason, LlmAnalysisProvenance) and reason.scenario == "analysis":
            if full != reason or self.reason_reference_id != self.analysis_reference_id:
                raise ValueError("reason from full analysis requires the same provenance")
        return self
