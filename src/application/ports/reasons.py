"""Операции хранения, необходимые заполнению причин."""

from typing import Protocol
from uuid import UUID

from src.application.dto.analysis_writes import ReasonSnapshot, SaveReason
from src.application.dto.reasons import ReasonCandidates, ReasonSelection


class ReasonRepository(Protocol):
    """Короткие чтения и атомарная запись причины одного звонка."""

    async def candidates(self, selection: ReasonSelection) -> ReasonCandidates:
        """Вернуть снимок выбранных звонков и включённых причин."""
        ...

    async def load(self, call_id: UUID) -> ReasonSnapshot | None:
        """Прочитать согласованный снимок входа/версии; None означает отсутствие текста."""
        ...

    async def save(self, command: SaveReason) -> bool:
        """Записать обе проекции только при прежнем входе/версии; иначе вернуть False."""
        ...
