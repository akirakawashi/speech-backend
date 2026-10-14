"""Правила остановки получения записи и перехода к распознаванию."""

from src.domain.calls.contracts import JobKind, ProcessingStatus


def is_short_recording(duration_s: int | None, threshold_s: int) -> bool:
    """По заявленной длительности определить короткий звонок; ноль означает неизвестную длину."""
    return 0 < (duration_s or 0) < threshold_s


def recording_is_unavailable(attempt: int, max_attempts: int) -> bool:
    """По номеру попытки решить, исчерпано ли ожидание отсутствующей у источника записи."""
    return attempt >= max_attempts


def ingest_transition(outcome: str) -> tuple[ProcessingStatus, JobKind | None]:
    """По подтверждённому исходу вернуть статус и следующий этап; неизвестный исход отклонить."""
    if outcome == "skipped_short":
        return ProcessingStatus.SKIPPED_SHORT, None
    if outcome == "no_audio":
        return ProcessingStatus.SKIPPED_NO_AUDIO, None
    if outcome == "stored":
        return ProcessingStatus.TRANSCRIBING, JobKind.TRANSCRIBE
    raise ValueError("unknown ingestion outcome")
