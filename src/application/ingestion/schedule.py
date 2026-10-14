"""Расписание приёма метаданных и истории выполняет CPU-процесс бэкенда."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from src.application.dto.calls import CallSource
from src.application.dto.daily_ingest import DayShare
from src.application.ingestion.daily import catch_up
from src.application.ports.daily_ingest import BackfillRepository, DailyIngestRepository
from src.application.ports.sources import DailyBatchSource
from src.domain.calls.schedule import CollectionSchedule


class IntakeSchedule:
    """Для одного источника регистрировать вчера/сегодня и порцию истории по прежним правилам."""

    def __init__(
        self,
        source: CallSource,
        policy: CollectionSchedule,
        repository: DailyIngestRepository,
        history: BackfillRepository,
        source_factory: Callable[[], DailyBatchSource],
        *,
        history_from: date | None = None,
        history_days: int = 7,
    ) -> None:
        """Принять политику, порты и фабрику метаданных; аудио и LLM не создаются."""
        self._source = source
        self._policy = policy
        self._repository = repository
        self._history = history
        self._source_factory = source_factory
        self._history_from = history_from
        self._history_days = history_days
        self._last_started: datetime | None = None

    async def tick(self, *, now: datetime | None = None) -> None:
        """При наступлении срока принять метаданные; сбой оставляет прогон доступным для повтора.

        Источник вызывается до открытия транзакции регистрации. После перезапуска
        выполняется один догон; уникальные задания сохраняют прежние отсрочки.
        """
        instant = now or datetime.now(UTC)
        if self._policy.next_run(self._source.value, instant, self._last_started) > instant:
            return
        share = (
            DayShare(
                share=self._policy.immediate_share,
                night_from=self._policy.night_from,
                night_until=self._policy.night_until,
                timezone=self._policy.timezone,
            )
            if self._source is CallSource.NCC
            else None
        )
        await catch_up(
            source=self._source_factory(),
            repository=self._repository,
            history=self._history,
            call_source=self._source.value,
            today=instant.astimezone(ZoneInfo(self._policy.timezone)).date(),
            history_from=self._history_from,
            history_days=self._history_days,
            day_share=share,
            priority=20 if self._source is CallSource.TELPHIN else None,
            now=instant,
        )
        self._last_started = instant
