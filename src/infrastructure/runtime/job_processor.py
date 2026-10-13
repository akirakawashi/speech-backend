"""Общий последовательный CPU-цикл заданий с heartbeat, восстановлением и остановкой."""

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid
from collections.abc import Mapping, Sequence

from src.application.dto.calls import JobKind, JobLane
from src.application.dto.job_processing import job_processing_claim
from src.application.ports.job_processing import (
    JobProcessingApplication,
    JobProcessingRecovery,
    JobProcessingSchedule,
)
from src.application.ports.queue import ClaimedJob, JobQueue

logger = logging.getLogger(__name__)


class JobProcessor:
    """Исполнять зарегистрированные модельные задания по одному; аудио не обрабатывать."""

    def __init__(
        self,
        queue: JobQueue,
        applications: Mapping[JobKind, JobProcessingApplication],
        *,
        lanes: Sequence[JobLane],
        recoveries: Mapping[JobKind, JobProcessingRecovery] | None = None,
        schedule: JobProcessingSchedule | None = None,
        worker_id: str | None = None,
        idle_seconds: float = 2,
        heartbeat_seconds: float = 30,
        reaper_seconds: float = 60,
    ) -> None:
        """Внедрить порты и интервалы; зависимости моделей или базы здесь не создаются."""
        if (
            not applications
            or not lanes
            or min(idle_seconds, heartbeat_seconds, reaper_seconds) <= 0
        ):
            raise ValueError("CPU processor requires applications, lanes and positive intervals")
        self._queue = queue
        self._applications = dict(applications)
        self._kinds = tuple(self._applications)
        self._recoveries = dict(recoveries or {})
        self._schedule = schedule
        self._background_scheduling = False
        self._lanes = tuple(lanes)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._idle_seconds = idle_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._reaper_seconds = reaper_seconds
        self._stopping = asyncio.Event()
        self._recovered = False

    def request_stop(self) -> None:
        """Прекратить новые захваты после текущего задания; сигнал не отменяет анализ."""
        self._stopping.set()

    async def run_once(self) -> bool:
        """Восстановить свои задания и обработать одно; False при пустой очереди."""
        if not self._recovered:
            await self._reap()
            self._recovered = True
        if self._stopping.is_set():
            return False
        if self._schedule is not None and not self._background_scheduling:
            await self._schedule.tick()
        if self._stopping.is_set():
            return False
        job = await self._queue.claim(self._lanes, self.worker_id, kinds=self._kinds)
        if job is None:
            return False
        await self._process(job)
        return True

    async def run_forever(self) -> None:
        """Последовательно выполнять анализ до SIGTERM/SIGINT, изолируя временные ошибки базы."""
        reaper = asyncio.create_task(self._reap_loop())
        scheduler = None
        if self._schedule is not None:
            self._background_scheduling = True
            scheduler = asyncio.create_task(self._schedule_loop())
        try:
            while not self._stopping.is_set():
                processed = False
                try:
                    processed = await self.run_once()
                except Exception as exc:
                    logger.error("CPU processing queue unavailable: type=%s", type(exc).__name__)
                if not processed:
                    await self._wait(self._idle_seconds)
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            if scheduler is not None:
                scheduler.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await scheduler
            self._background_scheduling = False

    async def _schedule_loop(self) -> None:
        """Вызывать единственный планировщик независимо от длительности LLM."""
        assert self._schedule is not None
        while not self._stopping.is_set():
            try:
                await self._schedule.tick()
            except Exception as exc:
                logger.error("CPU scheduling unavailable: type=%s", type(exc).__name__)
            await self._wait(self._idle_seconds)

    async def _process(self, job: ClaimedJob) -> None:
        """Удерживать nonce; потеря heartbeat прекращает устаревшую application-задачу."""
        claim = job_processing_claim(job, self.worker_id)
        work = asyncio.create_task(self._applications[job.kind].process(claim))
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            await asyncio.wait((work, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if not work.done():
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
                logger.warning("CPU processing lease lost: job=%s call=%s", job.id, job.call_id)
                return
            outcome = await work
            logger.info(
                "CPU processing outcome: job=%s call=%s status=%s class=%s",
                job.id,
                job.call_id,
                outcome.status,
                outcome.error_class.value if outcome.error_class else "-",
            )
        except asyncio.CancelledError:
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work
            raise
        except Exception as exc:
            logger.error(
                "CPU processing processing interrupted: job=%s call=%s type=%s",
                job.id,
                job.call_id,
                type(exc).__name__,
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "CPU processing heartbeat ended: job=%s type=%s", job.id, type(exc).__name__
                )

    async def _heartbeat(self, job: ClaimedJob) -> None:
        """Продлевать только живой nonce; три последовательных ошибки прекращают удержание."""
        interval = min(self._heartbeat_seconds, max(0.01, job.lease_seconds / 3))
        failures = 0
        while True:
            await asyncio.sleep(interval)
            try:
                alive = await self._queue.heartbeat(
                    job.id, self.worker_id, claim_token=job.claim_token
                )
            except Exception as exc:
                failures += 1
                logger.warning(
                    "CPU processing heartbeat failed: job=%s count=%s type=%s",
                    job.id,
                    failures,
                    type(exc).__name__,
                )
                if failures >= 3:
                    return
                continue
            failures = 0
            if not alive:
                return

    async def _reap(self) -> None:
        """Вернуть только свои виды/полосы; применить внедрённые проекции dead после аварии."""
        expired = await self._queue.requeue_expired(kinds=self._kinds, lanes=self._lanes)
        for job in expired:
            recovery = self._recoveries.get(job.kind)
            if recovery is not None:
                await recovery.mark_expired(job)
        for recovery in self._recoveries.values():
            await recovery.repair_expired_failures(self._lanes)

    async def _reap_loop(self) -> None:
        """Повторять восстановление до остановки; ошибки не прекращают обработку очереди."""
        while not self._stopping.is_set():
            await self._wait(self._reaper_seconds)
            if self._stopping.is_set():
                return
            try:
                await self._reap()
            except Exception as exc:
                logger.error("CPU processing reaper failed: type=%s", type(exc).__name__)

    async def _wait(self, seconds: float) -> None:
        """Подождать интервал или немедленно проснуться при штатной остановке."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)


def install_signal_handlers(processor: JobProcessor) -> None:
    """Связать SIGTERM/SIGINT с завершением текущей работы без отмены её задачи."""
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, processor.request_stop)
