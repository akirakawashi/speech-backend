"""Семантические контракты снимка, происхождения и частичного результата."""

from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.application.dto.analysis_storage import (
    AnalysisReferencePayload,
    AnalysisReferenceSnapshot,
    LegacyAnalysisProvenance,
    LlmAnalysisProvenance,
    StoredCallAnalysis,
)
from tests.analysis_storage_support import (
    NODE_ID,
    NOW,
    REFERENCE_ID,
    analysis,
    legacy,
    provenance,
    references,
)


def test_full_analysis_roundtrip_keeps_independent_component_models() -> None:
    """JSON-передача не смешивает полную классификацию и дополнение другой моделью."""
    value = analysis()
    restored = StoredCallAnalysis.model_validate_json(value.model_dump_json())
    assert restored == value
    assert isinstance(restored.analysis_provenance, LlmAnalysisProvenance)
    assert isinstance(restored.reason_provenance, LlmAnalysisProvenance)
    assert restored.analysis_provenance.requested_model == "model-a"
    assert restored.reason_provenance.requested_model == "model-b"


def test_reason_only_record_does_not_manufacture_a_full_analysis() -> None:
    """Самостоятельная причина допустима без тональности, резюме и полного происхождения."""
    data = analysis().model_dump()
    data.update(
        ai_node_id=None,
        ai_path=None,
        sentiment=None,
        summary=None,
        analysis_reference_id=None,
        analysis_provenance=None,
    )
    result = StoredCallAnalysis.model_validate(data)
    assert result.analysis_provenance is None and result.sentiment is None
    assert result.call_reason_code == "K05"


@pytest.mark.parametrize("missing", ["call_reason", "call_reason_code"])
def test_reason_text_and_code_are_independently_optional(missing) -> None:
    """Текст причины без кода и код без текста сохраняют нынешний контракт."""
    data = analysis().model_dump()
    data[missing] = None
    assert StoredCallAnalysis.model_validate(data).reason_provenance is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 0},
        {"revision": True},
        {"binding_kind": "invented"},
        {"transcript_id": UUID(int=999)},
        {"analysis_reference_id": None},
        {"reason_reference_id": None},
        {"sentiment": None},
        {"summary": "x" * 301},
        {"ai_path": ()},
        {"ai_node_id": None},
        {"ai_suggestion_node_id": NODE_ID},
        {"call_reason": "x" * 201},
        {"call_reason_code": "UNKNOWN"},
        {"call_reason": None, "call_reason_code": None},
        {"reason_provenance": None},
        {"updated_at": NOW - timedelta(seconds=1)},
        {"raw_text": "must not enter provenance"},
    ],
)
def test_invalid_publication_is_rejected_before_persistence(changes) -> None:
    """Неполный, чужой или несогласованный результат не проходит контракт записи."""
    data = analysis().model_dump()
    data.update(changes)
    with pytest.raises(ValidationError):
        StoredCallAnalysis.model_validate(data)


def test_reason_claiming_full_analysis_must_share_its_provenance() -> None:
    """Чужая модель не может выдать свою причину за компонент полного анализа."""
    data = analysis().model_dump()
    data["reason_provenance"] = provenance(model="another-model").model_dump()
    with pytest.raises(ValidationError, match="same provenance"):
        StoredCallAnalysis.model_validate(data)
    data["reason_provenance"] = provenance().model_dump()
    assert StoredCallAnalysis.model_validate(data).call_reason_code == "K05"


def test_legacy_import_retains_unknowns_without_inventing_request_metadata() -> None:
    """Прежнее резюме не обрезается, а модель и настоящий вход остаются неизвестны."""
    data = analysis().model_dump()
    data.update(
        binding_kind="legacy_observed",
        analysis_reference_id=None,
        reason_reference_id=None,
        analysis_provenance=legacy().model_dump(),
        reason_provenance=legacy().model_dump(),
        summary="x" * 350,
    )
    result = StoredCallAnalysis.model_validate_json(
        StoredCallAnalysis.model_validate(data).model_dump_json()
    )
    assert isinstance(result.analysis_provenance, LegacyAnalysisProvenance)
    assert len(result.summary or "") == 350
    assert not hasattr(result.analysis_provenance, "requested_model")
    for changes in (
        {"binding_kind": "verified"},
        {"analysis_reference_id": REFERENCE_ID},
    ):
        with pytest.raises(ValidationError):
            StoredCallAnalysis.model_validate({**data, **changes})
    with pytest.raises(ValidationError):
        LegacyAnalysisProvenance.model_validate(
            {**legacy().model_dump(), "requested_model": "today"}
        )


def test_reference_hash_ignores_select_order_but_tracks_meaningful_changes() -> None:
    """Перестановка строк не создаёт редакцию, изменение доступности или подписи создаёт."""
    original = references()
    data = original.model_dump()
    data["reasons"] = tuple(reversed(data["reasons"]))
    assert AnalysisReferencePayload.model_validate(data).fingerprint() == original.fingerprint()
    for changes in (
        {"is_active": False},
        {"sort_order": 1},
        {"label": "Другая тема", "key": "другая тема"},
    ):
        modified = original.model_dump()
        modified["classifier"] = [{**modified["classifier"][0], **changes}]
        assert (
            AnalysisReferencePayload.model_validate(modified).fingerprint()
            != original.fingerprint()
        )


def test_reference_snapshot_detects_stale_or_forged_fingerprint() -> None:
    """Снимок нельзя сохранить с хешем прежнего содержимого или другой версией DTO."""
    payload = references()
    snapshot = AnalysisReferenceSnapshot(
        id=REFERENCE_ID, payload=payload, fingerprint=payload.fingerprint(), created_at=NOW
    )
    assert AnalysisReferenceSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    for changes in ({"fingerprint": "0" * 64}, {"schema_version": 2}):
        with pytest.raises(ValidationError):
            AnalysisReferenceSnapshot.model_validate({**snapshot.model_dump(), **changes})


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_id",
        "duplicate_key",
        "missing_parent",
        "wrong_level",
        "wrong_key",
        "duplicate_code",
        "wrong_state",
        "reason_with_tree",
        "too_large",
        "extra",
    ],
)
def test_reference_cannot_lie_about_its_tree_or_sent_codes(change) -> None:
    """Снимок проверяет структуру и фактическую передачу, сохраняя динамические названия тем."""
    data = references().model_dump()
    if change == "duplicate_id":
        data["classifier"] = [data["classifier"][0], data["classifier"][0]]
    elif change == "duplicate_key":
        data["classifier"] = [data["classifier"][0], {**data["classifier"][0], "id": UUID(int=555)}]
    elif change == "missing_parent":
        data["classifier"] = [{**data["classifier"][0], "parent_id": UUID(int=555), "level": 2}]
    elif change == "wrong_level":
        data["classifier"][0]["level"] = 2
    elif change == "wrong_key":
        data["classifier"][0]["key"] = "different"
    elif change == "duplicate_code":
        data["reasons"] = [data["reasons"][0], data["reasons"][0]]
    elif change == "wrong_state":
        data["reason_reference_state"] = "empty"
    elif change == "reason_with_tree":
        data["scenario"] = "reason"
    elif change == "too_large":
        data["reasons"][0]["hint"] = "x" * 16001
    else:
        data["classifier"][0]["secret"] = "not allowed"
    with pytest.raises(ValidationError):
        AnalysisReferencePayload.model_validate(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"finished_at": NOW - timedelta(seconds=1)},
        {"fingerprint_schema": "reason-snapshot/1"},
        {"request_fingerprint": "F" * 64},
        {"source_fingerprint": "0" * 63},
        {"prompt_digest": "0" * 65},
        {"started_at": NOW.replace(tzinfo=None)},
        {"model_call_index": 0},
        {"max_tokens": True},
        {"job": {"job_id": UUID(int=1), "claim_token": UUID(int=2), "attempt": 0}},
        {"api_key": "not allowed"},
    ],
)
def test_execution_evidence_rejects_inconsistent_or_secret_fields(changes) -> None:
    """Происхождение не принимает несогласованные времена, версии, попытки и секреты."""
    with pytest.raises(ValidationError):
        LlmAnalysisProvenance.model_validate({**provenance().model_dump(), **changes})
