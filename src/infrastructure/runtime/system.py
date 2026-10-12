"""Проверка PostgreSQL, схемы, метрик и идентичности собранного артефакта."""

from sqlalchemy import text

from src.application.dto.calls import SPEECH_API_CONTRACT_VERSION
from src.application.dto.system import MetricsText, Readiness, VersionInfo
from src.infrastructure.observability import metrics as speech_metrics
from src.infrastructure.persistence.session import (
    SPEECH_REQUIRED_SCHEMA_REVISION,
    get_speech_ms_sessionmaker,
    speech_schema_is_ready,
)
from src.infrastructure.runtime.build_info import build_version_info
from src.setting.config import Settings


class PostgresSystemProbe:
    """Открывать короткую сессию только для готовности или сбора счётчиков."""

    def __init__(self, settings: Settings) -> None:
        """Принять явную конфигурацию подключения, не создавать его при импорте."""
        self._settings = settings

    async def readiness(self) -> Readiness:
        """Проверить соединение и схему; отказ базы вернуть закрытым результатом."""
        try:
            async with get_speech_ms_sessionmaker(self._settings)() as session:
                await session.execute(text("SELECT 1"))
                ready = await speech_schema_is_ready(session)
        except Exception:
            return Readiness(ready=False, detail="database unavailable")
        if not ready:
            return Readiness(
                ready=False,
                detail=(
                    "schema=migration_required: expected revision "
                    + SPEECH_REQUIRED_SCHEMA_REVISION
                    + (
                        ". Run `alembic -c alembic.ini upgrade head` against "
                        "SPEECH_MS_DATABASE_URL."
                    )
                ),
            )
        return Readiness(ready=True)

    def version(self) -> VersionInfo:
        """Вернуть артефакт и текущую версию контракта API."""
        return build_version_info(
            "speech-ms",
            {"speech.api": SPEECH_API_CONTRACT_VERSION},
            commit=self._settings.aicistent_git_commit,
            build_time=self._settings.aicistent_build_time,
        )

    async def metrics(self) -> MetricsText:
        """Собрать счётчики в отдельной короткой сессии и вернуть текст Prometheus."""
        async with get_speech_ms_sessionmaker(self._settings)() as session:
            samples = await speech_metrics.collect(session)
        return MetricsText(content=speech_metrics.render(samples))
