"""Неизменный вход CPU-оценки, момент чтения и бюджет попыток очереди."""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.application.dto.calls import ProcessingStatus, RoleMethod
from src.application.dto.quality import TranscriptLine
from src.application.dto.quality_batch import LineSnapshot


class EvaluationInput(BaseModel):
    """Текст, роли и идентичность версии; сравнение DTO обнаруживает замену входа."""

    model_config = ConfigDict(frozen=True)
    call_source: str
    processing_status: ProcessingStatus
    transcript_id: UUID
    transcript_created_at: AwareDatetime
    role_method: RoleMethod
    lines: tuple[TranscriptLine, ...] = Field(repr=False)


class EvaluationSnapshot(BaseModel):
    """Согласованный вход прочитан короткой транзакцией; HTTP выполняется после её закрытия."""

    model_config = ConfigDict(frozen=True)
    data: EvaluationInput = Field(repr=False)
    read_at: AwareDatetime
    max_attempts: int = Field(ge=1, le=32767)
    other_processing: bool = False

    @property
    def lines(self) -> LineSnapshot:
        """Вернуть прежний DTO сценария оценки с фиксированным моментом чтения."""
        return LineSnapshot(lines=self.data.lines, read_at=self.read_at)
