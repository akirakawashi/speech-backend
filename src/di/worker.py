"""Сборка ресурсов и запуск технического исполнителя конвейера."""

from __future__ import annotations

import argparse
import asyncio

from src.application.dto.calls import JobKind
from src.di.pipeline import build_stage_deps
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.infrastructure.queue.worker import (
    Worker,
    _install_signal_handlers,
    parse_lanes,
    parse_stages,
)
from src.infrastructure.runtime.logs import configure_logging


def run_worker(args: argparse.Namespace) -> int:
    """Собрать ресурсы выбранных этапов и запустить рабочий цикл."""
    configure_logging()
    lanes = parse_lanes(args.lanes)
    stages = parse_stages(args.stages)

    from src.di.analysis import build_analysis_client
    from src.di.pipeline import build_registry
    from src.di.sources import build_worker_source
    from src.infrastructure.clients.whisper import build_whisper_client
    from src.infrastructure.persistence.session import (
        get_speech_ms_engine,
        get_speech_ms_sessionmaker,
    )
    from src.infrastructure.storage.recordings import build_storage
    from src.setting.config import get_settings

    settings = get_settings()
    sessionmaker = get_speech_ms_sessionmaker(settings)
    # Движок передаётся обязательно: без него очередь отказывается выдавать
    # слот расшифровки — advisory-блокировку уровня сессии нельзя держать на
    # соединении, которое пул волен вернуть. Тесты собирали очередь с движком,
    # а боевая точка входа — без, и расхождение всплыло бы только на первом
    # запуске этапа расшифровки.
    queue = PostgresJobQueue(sessionmaker, engine=get_speech_ms_engine(settings))
    # Зависимости собираются **при старте**, а не по требованию внутри этапа:
    # ненастроенное хранилище или пустой адрес Whisper обязаны остановить
    # процесс сразу и один раз, а не выясняться на первом задании — то есть
    # ночью и на всём суточном пакете.
    # Каждая зависимость собирается под свой этап. Воркер, взявший только
    # анализ, не обязан знать ни каталога выгрузки NCC, ни адреса Whisper — и
    # раньше отказывался стартовать без них, то есть отказывался делать ровно
    # то, что умеет.
    #
    # Строгость не потеряна: зависимость **взятого** этапа обязана собраться при
    # старте. Ненастроенное хранилище останавливает процесс сразу и один раз, а
    # не выясняется на первом задании — то есть ночью и на всём суточном пакете.
    deps = build_stage_deps(
        settings=settings,
        sessionmaker=sessionmaker,
        # Записи звонков всех источников: NCC и Телфина (фича 14).
        source=build_worker_source(settings) if JobKind.INGEST in stages else None,
        # Хранилище нужно приёму (кладёт запись) и расшифровке (читает её).
        storage=(
            build_storage(settings) if {JobKind.INGEST, JobKind.TRANSCRIBE} & set(stages) else None
        ),
        transcription=build_whisper_client(settings) if JobKind.TRANSCRIBE in stages else None,
        analysis=build_analysis_client(settings) if JobKind.ANALYZE in stages else None,
    )
    worker = Worker(queue, build_registry(stages), deps, lanes=lanes)

    async def _run() -> None:
        """Запустить рабочий цикл и корректно обработать остановку процесса."""
        _install_signal_handlers(worker)
        if args.once:
            await worker.run_once()
            return
        await worker.run_forever()

    print(
        f"speech worker {worker.worker_id} "
        f"lanes={','.join(lane.value for lane in lanes)} "
        f"stages={','.join(kind.value for kind in stages)}"
    )
    asyncio.run(_run())
    return 0
