"""Порт короткой транзакции принятия решения для живого ingest."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from src.application.dto.ingest_admission import IngestAdmission, IngestContext
from src.application.dto.ingest_control import IngestClaim


class IngestControlTransaction(Protocol):
    """Проверить захват и сохранить снимок решения, не скачивая записи."""

    async def load(self, claim: IngestClaim) -> IngestContext:
        """Заблокировать звонок/задание, проверить аренду и вернуть снимок либо ApplicationError."""
        ...

    async def remember(self, claim: IngestClaim, admission: IngestAdmission) -> None:
        """Сохранить решение того же живого nonce; потеря аренды откатывает транзакцию."""
        ...


class IngestControlRepository(Protocol):
    """Открыть единицу работы, которая закрывается до передачи технической команды."""

    def transaction(self) -> AbstractAsyncContextManager[IngestControlTransaction]:
        """Коммитить снимок при успехе, откатывать при любой ошибке."""
        ...
