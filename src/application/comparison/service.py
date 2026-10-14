"""Сравнить модель-кандидат с сохранённым результатом и необязательным контролем."""

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence

from src.application.analysis.execution import AnalysisExecutionService
from src.application.dto.calls import JobErrorClass
from src.application.dto.comparison import Outcome, ReasonOutcome, ReasonRow, Row, Sampled
from src.application.dto.pipeline_input import AnalysisInput, AnalysisSegment
from src.application.errors.stages import StageError
from src.application.ports.analysis import AnalysisBackend
from src.application.ports.comparison import ComparisonRepository
from src.application.reasons.batch import ask_reason
from src.domain.calls.reasons import REASONS_PROMPT_LIMIT, prompt_reasons


async def analyse(
    application: AnalysisExecutionService,
    backend: AnalysisBackend,
    call_id: uuid.UUID,
    data: AnalysisInput | None,
) -> Outcome:
    """Вычислить анализ снимка без очереди и записи; отказ вернуть в Outcome."""
    started = time.monotonic()
    try:
        if data is None:
            raise StageError(JobErrorClass.NOT_FOUND, f"call {call_id} is gone")
        work = await application.execute(call_id, data, backend)
    except StageError as exc:
        return Outcome(
            work=None, error=f"{exc.error_class.value}: {exc}", seconds=time.monotonic() - started
        )
    return Outcome(work=work, error=None, seconds=time.monotonic() - started)


async def compare(
    calls: Sequence[Sampled],
    *,
    repository: ComparisonRepository,
    candidate: AnalysisBackend,
    control: AnalysisBackend | None,
    concurrency: int,
    progress_every: int = 10,
    echo: Callable[[str], None] = print,
) -> list[Row]:
    """Сравнить модели на одном DTO входа каждого звонка без записи и аудиоресурсов."""
    application = AnalysisExecutionService()
    gate = asyncio.Semaphore(max(1, concurrency))
    started = time.monotonic()
    done = 0

    async def one(call: Sampled) -> Row:
        """Обработать один звонок в пределах разрешённой параллельности и учесть результат."""
        nonlocal done
        async with gate:
            data = await repository.analysis_input(call.call_id)
            if control is None:
                row = Row(
                    call=call,
                    candidate=await analyse(application, candidate, call.call_id, data),
                    control=None,
                )
            else:
                fresh, again = await asyncio.gather(
                    analyse(application, candidate, call.call_id, data),
                    analyse(application, control, call.call_id, data),
                )
                row = Row(call=call, candidate=fresh, control=again)
        done += 1
        if progress_every and done % progress_every == 0:
            # `print`, а не `logging`: в образах логирование не сконфигурировано.
            echo(f"  {done}/{len(calls)}, {time.monotonic() - started:.0f} с")
        return row

    return list(await asyncio.gather(*(one(call) for call in calls)))


async def _reason_reference(
    repository: ComparisonRepository,
) -> tuple[str, frozenset[str]]:
    """Прочитать и проверить доступный модели справочник причин."""
    reasons = await repository.reasons()
    reference = prompt_reasons(reasons)
    if not reasons or len(reference) > REASONS_PROMPT_LIMIT:
        raise SystemExit("the reason reference is empty or longer than the workflow accepts")
    return reference, frozenset(reason.code for reason in reasons)


async def _ask_timed(
    client: AnalysisBackend,
    call_id: uuid.UUID,
    reference: str,
    codes: frozenset[str],
    segments: Sequence[AnalysisSegment],
) -> ReasonOutcome:
    """Вызвать модель причины и вернуть ответ вместе с длительностью вызова."""
    started = time.monotonic()
    answer = await ask_reason(client, call_id, reference, codes, segments)
    seconds = time.monotonic() - started
    if answer is None:
        return ReasonOutcome(code=None, failed=True, seconds=seconds)
    return ReasonOutcome(code=answer.code, failed=False, seconds=seconds)


async def compare_reasons(
    repository: ComparisonRepository,
    calls: Sequence[tuple[uuid.UUID, str]],
    *,
    candidate: AnalysisBackend,
    control: AnalysisBackend | None,
    concurrency: int,
    progress_every: int = 10,
    echo: Callable[[str], None] = print,
) -> list[ReasonRow]:
    """Сравнить коды причин кандидата и контроля на переданной выборке звонков."""
    reference, codes = await _reason_reference(repository)
    gate = asyncio.Semaphore(max(1, concurrency))
    started = time.monotonic()
    done = 0

    async def one(call_id: uuid.UUID, stored: str) -> ReasonRow:
        """Обработать один звонок в пределах разрешённой параллельности и учесть результат."""
        nonlocal done
        async with gate:
            segments = await repository.segments(call_id)
            if control is None:
                fresh = await _ask_timed(candidate, call_id, reference, codes, segments)
                row = ReasonRow(call_id=call_id, stored=stored, candidate=fresh, control=None)
            else:
                fresh, again = await asyncio.gather(
                    _ask_timed(candidate, call_id, reference, codes, segments),
                    _ask_timed(control, call_id, reference, codes, segments),
                )
                row = ReasonRow(call_id=call_id, stored=stored, candidate=fresh, control=again)
        done += 1
        if progress_every and done % progress_every == 0:
            echo(f"  {done}/{len(calls)}, {time.monotonic() - started:.0f} с")
        return row

    return list(await asyncio.gather(*(one(call_id, stored) for call_id, stored in calls)))
