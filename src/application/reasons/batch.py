"""Заполнение причин с ограничением параллельности и проверкой ответов модели."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import datetime

from pydantic import JsonValue

from src.application.analysis.execution import (
    MAX_OUTPUT_ATTEMPTS,
)
from src.application.analysis.output import describe_raw_problem, normalise_outputs
from src.application.dto.analysis_writes import ReasonComputation, SaveReason
from src.application.dto.pipeline_input import AnalysisSegment
from src.application.dto.reasons import FillReport, ReasonAnswer, ReasonSelection
from src.application.errors.external import AnalysisError
from src.application.errors.reasons import ReasonsNotConfigured
from src.application.ports.analysis import AnalysisBackend
from src.application.ports.reasons import ReasonRepository
from src.domain.calls.reasons import REASONS_PROMPT_LIMIT, interpret_reason, prompt_reasons

logger = logging.getLogger(__name__)
PROVIDER_RETRIES = 3
PROVIDER_PAUSE_SECONDS = 15
REASON_RULES_VERSION = "reason-interpretation/1"


async def ask_reason(
    client: AnalysisBackend,
    call_id: uuid.UUID,
    reference: str,
    codes: frozenset[str],
    segments: Sequence[AnalysisSegment],
) -> ReasonAnswer | None:
    """Вернуть причину для существующих потребителей без записи; None при отказе LLM."""
    result = await compute_reason(client, call_id, reference, codes, segments)
    return result.answer if result is not None else None


async def compute_reason(
    client: AnalysisBackend,
    call_id: uuid.UUID,
    reference: str,
    codes: frozenset[str],
    segments: Sequence[AnalysisSegment],
) -> ReasonComputation | None:
    """Вычислить причину и сохранить факты вызова, не удерживая транзакцию.

    Args:
        client: Порт сценария reason.
        call_id: UUID звонка.
        reference: Фактически передаваемый текст справочника.
        codes: Допустимые коды этого справочника.
        segments: Согласованные реплики исходной расшифровки.

    Returns:
        Последняя проверенная причина с фактами запроса, включая ответ без кода;
        None означает отказ провайдера после прежнего бюджета повторов.
    """
    inputs: dict[str, JsonValue] = {
        "call_id": str(call_id),
        "reasons": reference,
        "segments": [
            {"seq": int(s.seq), "speaker": str(s.speaker), "text": s.text_raw} for s in segments
        ],
    }
    result: ReasonComputation | None = None
    call_index = 0
    for _attempt in range(MAX_OUTPUT_ATTEMPTS):
        outputs: JsonValue = None
        for retry in range(PROVIDER_RETRIES):
            try:
                call_index += 1
                response = await client.run(inputs)
                outputs = response.outputs
                break
            except AnalysisError as exc:
                if not exc.retryable or retry == PROVIDER_RETRIES - 1:
                    logger.warning("call=%s: reason workflow failed: %s", call_id, exc.reason)
                    return None
                await asyncio.sleep(PROVIDER_PAUSE_SECONDS)
        problem = describe_raw_problem(outputs)
        answer = interpret_reason(normalise_outputs(outputs), codes)
        result = ReasonComputation(
            answer=answer,
            execution=response.execution,
            model_call_index=call_index,
            rules_version=REASON_RULES_VERSION,
        )
        if answer.code:
            return result
        logger.info("call=%s: no reason code in the answer (%s)", call_id, problem or "no code")
    return result


async def fill_reasons(
    repository: ReasonRepository,
    client: AnalysisBackend | None,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    transcribed_since: datetime | None = None,
    apply: bool,
    limit: int | None = None,
    concurrency: int = 4,
    progress_every: int = 100,
) -> FillReport:
    """Причины звонков периода — или звонков, расшифрованных после `transcribed_since`."""
    if transcribed_since is None and (date_from is None or date_to is None):
        raise ValueError("give a period or transcribed_since")
    started = time.monotonic()
    candidates = await repository.candidates(
        ReasonSelection(
            date_from=date_from, date_to=date_to, transcribed_since=transcribed_since, limit=limit
        )
    )
    call_ids, reasons = candidates.call_ids, candidates.reasons
    report = FillReport(candidates=len(call_ids), applied=apply)
    if not apply or not call_ids:
        return report
    if client is None:
        raise ReasonsNotConfigured("no reason client")
    reference = prompt_reasons(reasons)
    if not reasons or len(reference) > REASONS_PROMPT_LIMIT:
        raise ReasonsNotConfigured(
            "the reason reference is empty or longer than the workflow accepts: "
            "python -m src.presentation.cli.seed_call_reasons --apply"
        )
    gate = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def fill_one(call_id: uuid.UUID) -> None:
        """Вычислить причину по свежему снимку и учесть только подтверждённую запись."""
        snapshot = await repository.load(call_id)
        if snapshot is None or snapshot.has_reason or not snapshot.transcript.segments:
            report.skipped += 1
            return
        actual_reference = prompt_reasons(snapshot.reasons)
        if not snapshot.reasons or len(actual_reference) > REASONS_PROMPT_LIMIT:
            report.skipped += 1
            return
        result = await compute_reason(
            client,
            call_id,
            actual_reference,
            frozenset(reason.code for reason in snapshot.reasons),
            snapshot.transcript.segments,
        )
        if result is None:
            report.failed += 1
        elif result.answer.code is None:
            report.unanswered += 1
        else:
            saved = await repository.save(SaveReason(source=snapshot, result=result))
            if saved:
                report.filled += 1
            else:
                report.skipped += 1

    async def one(call_id: uuid.UUID) -> None:
        """Ограничить параллельность и показать прогресс, включая пропущенные звонки."""
        nonlocal done
        async with gate:
            await fill_one(call_id)
            done += 1
            if progress_every and done % progress_every == 0:
                # `print`, а не `logging`: в образах логирование не сконфигурировано.
                print(
                    f"  {done}/{len(call_ids)}: записано {report.filled}, "
                    f"без ответа {report.unanswered}, сбой {report.failed}, "
                    f"пропущено {report.skipped}, "
                    f"{time.monotonic() - started:.0f} с",
                    flush=True,
                )

    await asyncio.gather(*(one(call_id) for call_id in call_ids))
    report.seconds = time.monotonic() - started
    return report
