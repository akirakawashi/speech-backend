"""Согласованные данные для проверок хранения анализа без сети и секретов."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from src.application.dto.analysis_storage import (
    AnalysisBindingKind,
    AnalysisJobEvidence,
    AnalysisReferencePayload,
    LegacyAnalysisProvenance,
    LlmAnalysisProvenance,
    ReferenceCallReason,
    ReferenceClassifierNode,
    StoredCallAnalysis,
)
from src.application.dto.calls import Sentiment

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
CALL_ID = UUID(int=101)
TRANSCRIPT_ID = UUID(int=102)
ANALYSIS_ID = UUID(int=103)
REFERENCE_ID = UUID(int=104)
NODE_ID = UUID(int=105)


def references() -> AnalysisReferencePayload:
    """Вернуть активный корень и две причины, реально переданные модели."""
    return AnalysisReferencePayload(
        scenario="analysis",
        classifier=(
            ReferenceClassifierNode(
                id=NODE_ID, parent_id=None, level=1, label="Новая тема", key="новая тема"
            ),
        ),
        reasons=(
            ReferenceCallReason(code="K05", label="Нет связи", hint="Связь отсутствует"),
            ReferenceCallReason(code="K99", label="Другое"),
        ),
        reason_reference_state="sent",
    )


def provenance(
    transcript_id: UUID = TRANSCRIPT_ID, *, scenario: str = "analysis", model: str = "model-a"
) -> LlmAnalysisProvenance:
    """Сформировать происхождение запроса; reason выполняется отдельно от очереди."""
    return LlmAnalysisProvenance.model_validate(
        {
            "execution_id": UUID(int=106),
            "scenario": scenario,
            "profile": "production",
            "requested_model": model,
            "prompt_version": "1.0",
            "prompt_digest": "1" * 64,
            "rules_version": "analysis-rules/1",
            "request_fingerprint": "2" * 64,
            "temperature": 0,
            "max_tokens": 8192,
            "transcript_id": transcript_id,
            "transcript_created_at": NOW - timedelta(minutes=1),
            "fingerprint_schema": f"{scenario}-snapshot/1",
            "source_fingerprint": "3" * 64,
            "committed_transcript_fingerprint": "4" * 64,
            "started_at": NOW,
            "finished_at": NOW + timedelta(seconds=1),
            "model_call_index": 1,
            "job": AnalysisJobEvidence(job_id=UUID(int=107), claim_token=UUID(int=108), attempt=1)
            if scenario == "analysis"
            else None,
        }
    )


def legacy() -> LegacyAnalysisProvenance:
    """Отметить неизвестность старой модели и исходного входа без подстановки."""
    return LegacyAnalysisProvenance(
        imported_at=NOW,
        unknown_fields=("requested_model", "prompt_version", "source_fingerprint", "finished_at"),
    )


def analysis() -> StoredCallAnalysis:
    """Вернуть полный актуальный разбор с независимым происхождением его причины."""
    return StoredCallAnalysis(
        id=ANALYSIS_ID,
        call_id=CALL_ID,
        transcript_id=TRANSCRIPT_ID,
        revision=1,
        binding_kind=AnalysisBindingKind.VERIFIED,
        ai_node_id=NODE_ID,
        ai_path=("Новая тема",),
        sentiment=Sentiment.NEGATIVE,
        summary="Нет связи.",
        analysis_reference_id=REFERENCE_ID,
        analysis_provenance=provenance(),
        call_reason="Нет домашнего интернета.",
        call_reason_code="K05",
        reason_reference_id=REFERENCE_ID,
        reason_provenance=provenance(scenario="reason", model="model-b"),
        created_at=NOW,
        updated_at=NOW,
    )
