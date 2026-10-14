"""Решения допуска записи и переходов ingest принадлежат бэкенду."""

from src.application.dto.ingest_admission import IngestAdmission
from src.application.dto.ingest_control import (
    FetchRecording,
    IngestClaim,
    IngestDecision,
    IngestPlan,
    IngestResolution,
    ResolveIngest,
)
from src.application.dto.pipeline import IngestWork, StageResult
from src.application.dto.pipeline_input import CallForIngest
from src.application.dto.sources import CallRef
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.ingest_control import IngestControlRepository
from src.application.recordings import object_key
from src.application.worker_access import authorize_worker
from src.domain.calls.admission import (
    ingest_transition,
    is_short_recording,
    recording_is_unavailable,
)
from src.domain.calls.contracts import AudioStatus


class IngestControlService:
    """Выдать узкую команду и истолковать факты; аудио, Whisper и LLM здесь не вызываются."""

    def __init__(self, repository: IngestControlRepository, *, worker_token: str) -> None:
        """Принять порт PostgreSQL и служебный ключ; не открывать ресурсы."""
        self._repository = repository
        self._worker_token = worker_token

    async def prepare(self, claim: IngestClaim, authorization: str | None) -> IngestPlan:
        """Проверить доступ/аренду, зафиксировать правила попытки и вернуть команду получения."""
        authorize_worker(self._worker_token, authorization)
        async with self._repository.transaction() as transaction:
            context = await transaction.load(claim)
            admission = context.admission
            if admission is None:
                admission = IngestAdmission(
                    claim_token=claim.claim_token,
                    skip_short=is_short_recording(
                        context.call.duration_s, context.short_call_threshold_s
                    ),
                    object_key=object_key(context.call.session_id, context.call.started_at),
                    segment=context.call.project_segment,
                    product=context.call.project_product,
                )
                await transaction.remember(claim, admission)
            if admission.skip_short:
                return IngestPlan(
                    claim=claim, completion=_decision(admission, IngestWork.SKIPPED_SHORT)
                )
            return IngestPlan(
                claim=claim,
                fetch=FetchRecording(
                    ref=_recording_ref(context.call),
                    object_key=admission.object_key,
                    check_stored=context.call.audio_status == AudioStatus.STORED.value
                    and context.call.audio_object_key == admission.object_key,
                ),
            )

    async def resolve(self, command: ResolveIngest, authorization: str | None) -> IngestResolution:
        """По фактам записи вернуть повтор либо итог с переходом; чужой nonce/ключ отклонить."""
        authorize_worker(self._worker_token, authorization)
        claim, observation = command.claim, command.observation
        async with self._repository.transaction() as transaction:
            context = await transaction.load(claim)
            admission = context.admission
            if admission is None or admission.skip_short:
                raise ApplicationError(ErrorKind.CONFLICT, "ingest_command_is_not_prepared")
            if observation.outcome == "missing":
                if not recording_is_unavailable(claim.attempt, context.max_attempts):
                    return IngestResolution(claim=claim, retry=True)
                completion = _decision(admission, IngestWork.NO_AUDIO)
            else:
                if observation.object_key != admission.object_key:
                    raise ApplicationError(ErrorKind.CONFLICT, "ingest_object_changed")
                completion = _decision(
                    admission,
                    IngestWork.STORED,
                    object_key=observation.object_key,
                    audio_bytes=observation.audio_bytes,
                    channels=observation.channels,
                    duration_s=observation.duration_s,
                )
            return IngestResolution(claim=claim, completion=completion)


def _decision(
    admission: IngestAdmission,
    outcome: str,
    *,
    object_key: str | None = None,
    audio_bytes: int | None = None,
    channels: int | None = None,
    duration_s: int | None = None,
) -> IngestDecision:
    """Применить доменный переход и снимок проекта; вернуть готовое изменение звонка."""
    status, next_kind = ingest_transition(outcome)
    return IngestDecision(
        work=IngestWork(
            outcome=outcome,
            object_key=object_key,
            audio_bytes=audio_bytes,
            channels=channels,
            duration_s=duration_s,
            segment=admission.segment,
            product=admission.product,
        ),
        stage=StageResult(processing_status=status, next_kind=next_kind),
    )


def _recording_ref(call: CallForIngest) -> CallRef:
    """Восстановить технический локатор из звонка; отсутствие даёт безопасный NOT_FOUND."""
    locator = call.source_snapshot.get("audio_locator")
    if not locator:
        raise ApplicationError(ErrorKind.NOT_FOUND, "ingest_recording_locator_is_missing")
    return CallRef(
        session_id=call.session_id,
        audio_locator=str(locator),
        day=call.started_at.date(),
        started_at=call.started_at,
        duration_s=call.duration_s or None,
        source=call.source,
        audio_url=call.source_audio_url,
    )
