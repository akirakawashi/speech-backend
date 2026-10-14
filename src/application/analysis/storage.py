"""Сборка актуального анализа из проверенных DTO без базы и текущих Settings."""

from datetime import datetime
from uuid import UUID, uuid4

from src.application.dto.analysis_storage import (
    AnalysisBindingKind,
    AnalysisJobEvidence,
    LegacyAnalysisProvenance,
    LlmAnalysisProvenance,
    StoredCallAnalysis,
)
from src.application.dto.analysis_writes import SaveAnalysis, SaveReason, TranscriptState
from src.domain.calls.contracts import RoleMethod


def analysis_after_roles(command: SaveAnalysis) -> TranscriptState:
    """Получить состояние текста после собственных ролей, повторив порядок SQL-обновлений.

    Args:
        command: Сверенный вход и проверенный результат анализа.

    Returns:
        Состояние без справочников; последняя роль одного seq побеждает.
        Роль для отсутствующего seq не создаёт новую реплику.
    """
    roles = dict(command.result.work.roles)
    data = command.source.data
    return TranscriptState(
        transcript_id=command.source.transcript_id,
        transcript_created_at=command.source.transcript_created_at,
        role_method=RoleMethod.LLM_ATTRIBUTION.value if roles else data.call.role_method,
        segments=tuple(
            segment.model_copy(update={"speaker": roles[segment.seq].value})
            if segment.seq in roles
            else segment
            for segment in data.segments
        ),
    )


def full_analysis(
    command: SaveAnalysis, current: StoredCallAnalysis | None, reference_id: UUID, now: datetime
) -> StoredCallAnalysis:
    """Заменить весь модельный набор и зафиксировать факты успешного запроса.

    Args:
        command: Сверенный снимок, захват и пригодное вычисление.
        current: Прежняя запись под блокировкой звонка либо None.
        reference_id: UUID фактически использованного снимка справочников.
        now: Время БД внутри транзакции сохранения.

    Returns:
        Один текущий результат. Причина отсутствует, если её не дал этот анализ.
    """
    provenance = LlmAnalysisProvenance.model_validate(
        {
            **command.result.execution.model_dump(),
            "rules_version": command.result.rules_version,
            "transcript_id": command.source.transcript_id,
            "transcript_created_at": command.source.transcript_created_at,
            "fingerprint_schema": "analysis-snapshot/1",
            "source_fingerprint": command.source.fingerprint,
            "committed_transcript_fingerprint": analysis_after_roles(command).fingerprint(),
            "model_call_index": command.result.model_call_index,
            "job": AnalysisJobEvidence(
                job_id=command.claim.job_id,
                claim_token=command.claim.claim_token,
                attempt=command.claim.attempt,
            ),
        }
    )
    work = command.result.work
    has_reason = work.call_reason is not None or work.call_reason_code is not None
    return StoredCallAnalysis(
        id=current.id if current else uuid4(),
        call_id=command.claim.call_id,
        transcript_id=command.source.transcript_id,
        revision=current.revision + 1 if current else 1,
        binding_kind=AnalysisBindingKind.VERIFIED,
        ai_node_id=work.node_id,
        ai_path=work.path or None,
        ai_suggestion=work.suggestion or None,
        ai_suggestion_node_id=work.suggestion_node_id,
        sentiment=work.sentiment,
        summary=work.summary,
        llm_segment=work.segment,
        llm_product=work.product,
        call_reason=work.call_reason,
        call_reason_code=work.call_reason_code,
        analysis_reference_id=reference_id,
        analysis_provenance=provenance,
        reason_reference_id=reference_id if has_reason else None,
        reason_provenance=provenance if has_reason else None,
        created_at=current.created_at if current else now,
        updated_at=now,
    )


def with_reason(
    command: SaveReason, current: StoredCallAnalysis | None, reference_id: UUID, now: datetime
) -> StoredCallAnalysis:
    """Дополнить только причину, сохранив независимое происхождение полного анализа.

    Args:
        command: Проверенная причина и неизменный снимок её входа.
        current: Ожидаемый актуальный результат под блокировкой либо None.
        reference_id: UUID отправленного справочника причин.
        now: Время БД для новой ревизии и честной отметки переноса старых полей.

    Returns:
        Полный либо частичный результат; состояние процесса звонка не меняется.
    """
    source = command.source
    if current is None:
        legacy = source.legacy
        current = StoredCallAnalysis(
            id=uuid4(),
            call_id=source.call_id,
            transcript_id=source.transcript.transcript_id,
            revision=1,
            binding_kind=AnalysisBindingKind.LEGACY_OBSERVED
            if legacy.has_full_component
            else AnalysisBindingKind.VERIFIED,
            **legacy.model_dump(),
            analysis_provenance=LegacyAnalysisProvenance(
                imported_at=now,
                unknown_fields=(
                    "requested_model",
                    "prompt_version",
                    "rules_version",
                    "execution_time",
                    "source_fingerprint",
                    "reference_snapshot",
                    "original_ai_path",
                ),
            )
            if legacy.has_full_component
            else None,
            call_reason=command.result.answer.text,
            call_reason_code=command.result.answer.code,
            reason_reference_id=reference_id,
            reason_provenance=_reason_provenance(command),
            created_at=now,
            updated_at=now,
        )
        return current
    return StoredCallAnalysis.model_validate(
        {
            **current.model_dump(),
            "revision": current.revision + 1,
            "call_reason": command.result.answer.text,
            "call_reason_code": command.result.answer.code,
            "reason_reference_id": reference_id,
            "reason_provenance": _reason_provenance(command),
            "updated_at": now,
        }
    )


def _reason_provenance(command: SaveReason) -> LlmAnalysisProvenance:
    """Вернуть факты отдельного запроса с отпечатком входа reason-snapshot/1."""
    source = command.source
    return LlmAnalysisProvenance.model_validate(
        {
            **command.result.execution.model_dump(),
            "rules_version": command.result.rules_version,
            "transcript_id": source.transcript.transcript_id,
            "transcript_created_at": source.transcript.transcript_created_at,
            "fingerprint_schema": "reason-snapshot/1",
            "source_fingerprint": source.fingerprint,
            "committed_transcript_fingerprint": source.transcript.fingerprint(),
            "model_call_index": command.result.model_call_index,
        }
    )
