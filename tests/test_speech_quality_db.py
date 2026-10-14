"""Оценка звонков по скрипту на настоящем Postgres: команда и обе ручки.

Проверяется то, чего не видно без базы: какие звонки команда берёт, что
повторная оценка заменяет строку, а не копит её, что звонок вне скрипта в
сводку не входит, и что сводка разворачивает `jsonb` этапов и возражений в
счёт, который сходится с записанным.

Нужен `SPEECH_POSTGRES_TEST_URL` с применённой схемой.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.application.dto.analysis import AnalyzeWork
from src.application.quality.batch import evaluate_calls
from src.di.http import create_app
from src.di.requests import session_dep
from src.domain.calls.contracts import SPEECH_ROLE_READER, JobKind, Speaker
from src.domain.quality.script import CURRENT_SCRIPT
from src.infrastructure.clients.quality import QualityModelError
from src.infrastructure.persistence.evaluation import (
    PostgresEvaluationRepository,
    drop_stale,
    find_call,
    find_candidates,
)
from src.infrastructure.persistence.reprocessing import reprocess
from src.infrastructure.persistence.stages.analyze import store_result
from src.presentation.http.auth import Principal, current_principal
from src.setting.config import Settings, get_settings
from tests.speech_db_support import speech_test_database_url

_TEST_DATABASE_URL = speech_test_database_url()

pytestmark = pytest.mark.skipif(
    not _TEST_DATABASE_URL,
    reason="SPEECH_POSTGRES_TEST_URL is required",
)

START = datetime(2026, 9, 14, tzinfo=UTC)
END = START + timedelta(days=1)
SCRIPT = CURRENT_SCRIPT

Sessions = async_sessionmaker[AsyncSession]


def _answer(*, failed: Sequence[str] = (), applies: bool = True, outcome: str = "request") -> str:
    if not applies:
        return json.dumps({"script_applies": False, "summary": "Входящее обращение."})
    return json.dumps(
        {
            "script_applies": True,
            "stages": [
                {
                    "key": stage.key,
                    "applicable": stage.key != "busy",
                    "passed": stage.key not in failed,
                    "evidence": [0],
                    "comment": "",
                }
                for stage in SCRIPT.stages
            ],
            "objections": [
                {"type": "cheaper", "seq": 1, "handled": not failed, "evidence": [0]},
            ],
            "outcome": outcome,
            "summary": "Итог.",
        },
        ensure_ascii=False,
    )


#: Модель не смогла назвать ни одной роли в разговоре из двух реплик.
_UNKNOWN_ROLES = '{"roles": [{"seq": 0, "speaker": "unknown"}, {"seq": 1, "speaker": "unknown"}]}'


class _FakeModel:
    model = "fake-model"

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    async def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        self.prompts.append(messages[-1]["content"])
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]


async def _clean(sessions: Sessions) -> None:
    async with sessions() as session, session.begin():
        for table in (
            "speech_call_evaluations",
            "speech_transcript_segments",
            "speech_transcripts",
            "speech_calls",
            "speech_operators",
        ):
            await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608


@pytest.fixture()
async def sessions() -> AsyncIterator[Sessions]:
    assert _TEST_DATABASE_URL is not None
    engine = create_async_engine(_TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _clean(factory)
    yield factory
    await _clean(factory)
    await engine.dispose()


async def _operator(sessions: Sessions, login: str) -> uuid.UUID:
    operator_id = uuid.uuid4()
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_operators (id, login, full_name, removed, created_at, "
                "updated_at) VALUES (:id, :login, :name, false, now(), now())"
            ),
            {"id": operator_id, "login": login, "name": f"Сотрудник {login}"},
        )
    return operator_id


async def _call(
    sessions: Sessions,
    name: str,
    *,
    started: datetime = START + timedelta(hours=10),
    transcribed: bool = True,
    operator_id: uuid.UUID | None = None,
    source: str = SCRIPT.source.value,
) -> uuid.UUID:
    """Звонок, по умолчанию с расшифровкой и из источника скрипта: другие не оцениваются."""
    call_id = uuid.uuid4()
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_calls (id, session_id, source, operator_id, direction, "
                "started_at, ended_at, duration_s, audio_status, processing_status, "
                "analysis_status, segment_source, product_source, source_snapshot, "
                "first_seen_at, created_at, updated_at) "
                "VALUES (:id, :sid, :source, :op, 'outbound', :s, :s, 100, 'stored', 'done', "
                "'done', 'unknown', 'unknown', '{}', :s, :s, :s)"
            ),
            {
                "id": call_id,
                "sid": f"node_1_domain_1_nauss_1_1_{name}",
                "source": source,
                "op": operator_id,
                "s": started,
            },
        )
        if not transcribed:
            return call_id
        transcript_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO speech_transcripts (id, call_id, engine, model_name, language, "
                "role_method, segments_count, created_at) "
                "VALUES (:id, :c, 'test', 'large-v3', 'ru', 'stereo_energy', 2, now())"
            ),
            {"id": transcript_id, "c": call_id},
        )
        for seq, (speaker, raw) in enumerate(
            (
                ("operator", "Меня зовут Анна, компания Волна"),
                ("client", "Мой номер 79781234567"),
            )
        ):
            await session.execute(
                text(
                    "INSERT INTO speech_transcript_segments (transcript_id, call_id, seq, "
                    "speaker, start_ms, end_ms, text_raw) "
                    "VALUES (:t, :c, :seq, :speaker, :start, :end, :raw)"
                ),
                {
                    "t": transcript_id,
                    "c": call_id,
                    "seq": seq,
                    "speaker": speaker,
                    "start": seq * 3000,
                    "end": seq * 3000 + 2500,
                    "raw": raw,
                },
            )
    return call_id


async def _retranscribe(sessions: Sessions, call_id: uuid.UUID) -> None:
    """Перезаписать тестовый текст строго позже прежнего транскрипта и оценки.

    Новый вход должен делать прежнюю оценку устаревшей независимо от того,
    насколько быстро исполняется тест и с какой точностью обновляются часы.
    """
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE speech_transcript_segments SET text_raw = 'Новая реплика' "
                "WHERE call_id = :c AND seq = 0"
            ),
            {"c": call_id},
        )
        await session.execute(
            text(
                "UPDATE speech_transcripts "
                "SET created_at = GREATEST(clock_timestamp(), created_at, "
                "(SELECT max(evaluated_at) FROM speech_call_evaluations WHERE call_id = :c)) "
                "+ interval '1 microsecond' WHERE call_id = :c"
            ),
            {"c": call_id},
        )


async def _rows(sessions: Sessions) -> list[dict[str, Any]]:
    async with sessions() as session:
        result = await session.execute(
            text("SELECT * FROM speech_call_evaluations ORDER BY evaluated_at")
        )
        return [dict(row) for row in result.mappings()]


# --------------------------------------------------------------------------- #
# Команда
# --------------------------------------------------------------------------- #


async def test_candidates_are_transcribed_calls_of_the_period_without_an_evaluation(
    sessions: Sessions,
) -> None:
    fresh = await _call(sessions, "fresh")
    await _call(sessions, "untranscribed", transcribed=False)
    await _call(sessions, "outside", started=START - timedelta(hours=1))
    done = await _call(sessions, "done")
    await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel(_answer()),
        await _refs(sessions, done),
        script=SCRIPT,
    )

    found = await find_candidates(sessions, date_from=START, date_to=END)
    assert [ref.call_id for ref in found] == [fresh]

    forced = await find_candidates(sessions, date_from=START, date_to=END, force=True)
    assert {ref.call_id for ref in forced} == {fresh, done}


async def test_a_call_of_another_source_is_never_a_candidate(sessions: Sessions) -> None:
    """Скрипт продаж — про звонки своего источника: обращение в поддержку модель не видит."""
    sale = await _call(sessions, "sale")
    support = await _call(sessions, "support-line", source="ncc")

    found = await find_candidates(sessions, date_from=START, date_to=END)
    assert [ref.call_id for ref in found] == [sale]
    # `--force` пересчитывает оценённое, а не расширяет круг звонков.
    forced = await find_candidates(sessions, date_from=START, date_to=END, force=True)
    assert [ref.call_id for ref in forced] == [sale]
    # Названный явно звонок правило отбора не обходит.
    with pytest.raises(LookupError, match="is a ncc call"):
        await find_call(sessions, str(support))


async def _refs(sessions: Sessions, call_id: uuid.UUID) -> list[Any]:
    return [await find_call(sessions, str(call_id))]


async def test_the_transcript_text_reaches_the_model_as_it_is(sessions: Sessions) -> None:
    """Обезличивания нет: модель оценивает тот же текст, что записал Whisper."""
    call_id = await _call(sessions, "as_it_is")
    model = _FakeModel(_answer())
    await evaluate_calls(
        PostgresEvaluationRepository(sessions), model, await _refs(sessions, call_id), script=SCRIPT
    )
    prompt = model.prompts[0]
    assert "79781234567" in prompt and "Анна" in prompt


async def test_an_evaluation_is_stored_once_and_replaced_on_a_new_run(
    sessions: Sessions,
) -> None:
    call_id = await _call(sessions, "twice")
    refs = await _refs(sessions, call_id)
    first = await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel(_answer(failed=["passport"])),
        refs,
        script=SCRIPT,
    )
    assert (first.evaluated, first.failed) == (1, 0)
    await evaluate_calls(
        PostgresEvaluationRepository(sessions), _FakeModel(_answer()), refs, script=SCRIPT
    )

    rows = await _rows(sessions)
    assert len(rows) == 1
    row = rows[0]
    assert row["score"] == 100
    assert row["all_passed"] is True
    assert row["passed_count"] == row["stage_count"] == len(SCRIPT.stages)
    assert row["script_version"] == SCRIPT.version
    assert row["model_name"] == "fake-model"
    assert [stage["key"] for stage in row["stages"]] == [stage.key for stage in SCRIPT.stages]


async def test_a_call_outside_the_script_is_stored_without_scores(sessions: Sessions) -> None:
    call_id = await _call(sessions, "support")
    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel(_answer(applies=False)),
        await _refs(sessions, call_id),
    )
    assert (report.evaluated, report.not_applicable) == (0, 1)
    row = (await _rows(sessions))[0]
    assert row["script_applies"] is False
    assert row["score"] is None and row["all_passed"] is None
    # Записанный «не применим» больше не попадает в кандидаты.
    assert await find_candidates(sessions, date_from=START, date_to=END) == []


async def test_an_unusable_answer_leaves_the_call_for_the_next_run(sessions: Sessions) -> None:
    call_id = await _call(sessions, "broken")
    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel("нет", "нет", "нет"),
        await _refs(sessions, call_id),
    )
    assert (report.evaluated, report.failed) == (0, 1)
    row = (await _rows(sessions))[0]
    assert row["failure_reason"] == "invalid_evaluation"
    assert row["score"] is None and row["script_applies"] is None
    assert [
        ref.call_id for ref in await find_candidates(sessions, date_from=START, date_to=END)
    ] == [call_id]


async def test_without_a_period_the_newest_unscored_calls_come_first(sessions: Sessions) -> None:
    """`--all`: расписание берёт порцию — сначала свежее, остаток из истории."""
    old = await _call(sessions, "old", started=START - timedelta(days=40))
    scored = await _call(sessions, "scored", started=START + timedelta(hours=9))
    fresh = await _call(sessions, "fresh", started=START + timedelta(days=3))
    await _call(sessions, "support", started=START + timedelta(days=4), source="ncc")
    await evaluate_calls(
        PostgresEvaluationRepository(sessions), _FakeModel(_answer()), await _refs(sessions, scored)
    )

    everything = await find_candidates(sessions, date_from=None, date_to=None)
    assert [ref.call_id for ref in everything] == [fresh, old]

    portion = await find_candidates(sessions, date_from=None, date_to=None, limit=1)
    assert [ref.call_id for ref in portion] == [fresh]


async def test_a_failed_call_is_skipped_only_when_asked_and_only_while_unchanged(
    sessions: Sessions,
) -> None:
    """`--skip-failed`: расписание не возвращается к неудавшимся, пока звонок тот же."""
    broken = await _call(sessions, "broken")
    waiting = await _call(sessions, "waiting", started=START + timedelta(hours=11))
    await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel("нет", "нет", "нет"),
        await _refs(sessions, broken),
    )

    async def candidates(*, skip_failed: bool) -> list[uuid.UUID]:
        found = await find_candidates(
            sessions, date_from=None, date_to=None, skip_failed=skip_failed
        )
        return [ref.call_id for ref in found]

    # Запуск руками пробует неудавшийся снова, расписание — нет.
    assert await candidates(skip_failed=False) == [waiting, broken]
    assert await candidates(skip_failed=True) == [waiting]

    # Звонок расшифровали заново — прежний отказ к новому тексту уже не относится.
    await _retranscribe(sessions, broken)
    assert await candidates(skip_failed=True) == [waiting, broken]


async def test_a_call_without_a_transcript_cannot_be_picked_by_reference(
    sessions: Sessions,
) -> None:
    call_id = await _call(sessions, "untranscribed", transcribed=False)
    with pytest.raises(LookupError, match="no transcript"):
        await find_call(sessions, str(call_id))
    with pytest.raises(LookupError, match="not in the database"):
        await find_call(sessions, "node_9_domain_9_nauss_9_9_9")


# --------------------------------------------------------------------------- #
# Ручки
# --------------------------------------------------------------------------- #


@contextlib.asynccontextmanager
async def _api_client(
    sessions: Sessions, *, principal: Principal | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """Клиент поверх ASGI в том же цикле событий, что и движок базы.

    Без `principal` действует открытый режим: у запроса все права. С ним —
    права его ролей.
    """
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="local",
        speech_auth_mode="open",
        speech_ms_database_url=_TEST_DATABASE_URL or "",
    )

    async def _override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[session_dep] = _override
    if principal is not None:
        app.dependency_overrides[current_principal] = lambda: principal
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture()
async def api(sessions: Sessions) -> AsyncIterator[httpx.AsyncClient]:
    async with _api_client(sessions) as client:
        yield client


async def test_the_card_shows_labels_of_the_script(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    call_id = await _call(sessions, "card")
    await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel(_answer(failed=["passport"])),
        await _refs(sessions, call_id),
    )

    response = await api.get(f"/api/v1/calls/{call_id}/quality")
    assert response.status_code == 200
    body = response.json()
    assert body["score"] == 90
    assert body["all_passed"] is False
    assert body["outcome_label"] == "Заявка оформлена"
    passport = next(stage for stage in body["stages"] if stage["key"] == "passport")
    assert passport["label"] == "Обязательные условия — паспорт держателя, 18+"
    assert (passport["passed"], passport["mandatory"]) == (False, True)
    busy = next(stage for stage in body["stages"] if stage["key"] == "busy")
    assert (busy["applicable"], busy["passed"]) == (False, True)
    assert body["objections"][0]["label"] == "Мой тариф/провайдер выгоднее"


async def test_a_call_without_an_evaluation_is_a_named_404(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    call_id = await _call(sessions, "none")
    response = await api.get(f"/api/v1/calls/{call_id}/quality")
    assert response.status_code == 404
    assert response.json()["detail"] == "not_evaluated"


async def test_a_call_of_another_source_stays_out_of_the_screen(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    """Звонок поддержки не числится ни оценённым, ни ожидающим, а его карточка не ждёт оценки."""
    anna = await _operator(sessions, "anna")
    sale = await _call(sessions, "sale", operator_id=anna)
    support = await _call(sessions, "support-line", operator_id=anna, source="ncc")
    await evaluate_calls(
        PostgresEvaluationRepository(sessions), _FakeModel(_answer()), await _refs(sessions, sale)
    )
    # Оценка звонка поддержки, записанная до отбора по источнику, остаётся в
    # базе, но показываться перестаёт.
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_call_evaluations (id, call_id, script_version, model_name, "
                "script_applies, stages, objections, evaluated_at, transcript_fingerprint) "
                "SELECT gen_random_uuid(), :other, script_version, model_name, false, "
                "'[]'::jsonb, '[]'::jsonb, evaluated_at, transcript_fingerprint "
                "FROM speech_call_evaluations WHERE call_id = :sale"
            ),
            {"other": support, "sale": sale},
        )

    period = {"from": START.isoformat(), "to": END.isoformat()}
    summary = (await api.get("/api/v1/quality/summary", params=period)).json()
    assert (summary["total"], summary["evaluated"], summary["pending"]) == (1, 1, 0)
    assert [(item["login"], item["calls"]) for item in summary["operators"]] == [("anna", 1)]

    calls = (await api.get("/api/v1/quality/calls", params=period)).json()
    assert [item["call_id"] for item in calls["items"]] == [str(sale)]
    assert calls["total"] == 1

    card = await api.get(f"/api/v1/calls/{support}/quality")
    assert card.status_code == 404
    assert card.json()["detail"] == "not_applicable"


async def test_search_shows_the_score_only_to_those_who_may_see_the_evaluation(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    """Балл в выдаче поиска — у оценённого звонка и только с правом `quality`."""
    scored = await _call(sessions, "scored")
    plain = await _call(sessions, "plain", started=START + timedelta(hours=11))
    await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel(_answer(failed=["passport"])),
        await _refs(sessions, scored),
    )
    params = {
        "q": "Волна",
        "source": SCRIPT.source.value,
        "from": START.isoformat(),
        "to": END.isoformat(),
    }

    found = (await api.get("/api/v1/search", params=params)).json()
    assert {item["call_id"]: item["quality_score"] for item in found["items"]} == {
        str(scored): 90,
        str(plain): None,
    }

    # Читателю поиск открыт, оценка — нет: звонок он находит, балла не видит.
    reader = Principal(
        sub="reader",
        username="reader",
        email=None,
        display_name=None,
        roles=(SPEECH_ROLE_READER,),
        auth_mode="keycloak",
    )
    async with _api_client(sessions, principal=reader) as client:
        seen = (await client.get("/api/v1/search", params=params)).json()
    assert {item["call_id"]: item["quality_score"] for item in seen["items"]} == {
        str(scored): None,
        str(plain): None,
    }


async def test_the_summary_counts_only_calls_the_script_applies_to(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    anna = await _operator(sessions, "anna")
    oleg = await _operator(sessions, "oleg")
    good = await _call(sessions, "good", operator_id=anna)
    weak = await _call(sessions, "weak", operator_id=oleg)
    support = await _call(sessions, "support", operator_id=oleg)
    for call_id, answer in (
        (good, _answer()),
        (weak, _answer(failed=["passport", "needs"], outcome="refusal")),
        (support, _answer(applies=False)),
    ):
        await evaluate_calls(
            PostgresEvaluationRepository(sessions),
            _FakeModel(answer),
            await _refs(sessions, call_id),
        )

    response = await api.get(
        "/api/v1/quality/summary",
        params={"from": START.isoformat(), "to": END.isoformat()},
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["evaluated"], body["calls"], body["fully_correct"]) == (3, 2, 1)
    assert body["fully_correct_share"] == 0.5
    assert body["average_score"] == 90.0

    stages = {stage["key"]: stage for stage in body["stages"]}
    assert [stage["key"] for stage in body["stages"]] == [stage.key for stage in SCRIPT.stages]
    assert (stages["passport"]["passed"], stages["passport"]["share"]) == (1, 0.5)
    assert (stages["greeting"]["passed"], stages["greeting"]["share"]) == (2, 1.0)
    assert (stages["busy"]["applicable"], stages["busy"]["passed"]) == (0, 2)
    # Не понадобившийся этап засчитан, но невыполненным не считается.
    assert (stages["busy"]["failed"], stages["passport"]["failed"]) == (0, 1)
    assert (stages["passport"]["mandatory"], stages["greeting"]["mandatory"]) == (True, False)

    # Баллы 100 и 80: хороший и средний. Нарушение — пропущенный паспорт.
    assert body["buckets"] == {"good": 1, "fair": 1, "poor": 0}
    assert body["violations"] == 1

    cheaper = next(item for item in body["objections"] if item["type"] == "cheaper")
    assert (cheaper["raised"], cheaper["handled"], cheaper["share"]) == (2, 1, 0.5)

    outcomes = {item["key"]: item["calls"] for item in body["outcomes"]}
    assert outcomes["request"] == 1 and outcomes["refusal"] == 1 and outcomes["callback"] == 0

    operators = {item["login"]: item for item in body["operators"]}
    assert operators["anna"]["average_score"] == 100.0
    # Входящее обращение Олега в его балл не входит: один звонок по скрипту.
    assert (operators["oleg"]["calls"], operators["oleg"]["average_score"]) == (1, 80.0)
    assert operators["oleg"]["buckets"] == {"good": 0, "fair": 1, "poor": 0}
    assert (operators["oleg"]["violations"], operators["anna"]["violations"]) == (1, 0)
    # Строка таблицы «сотрудник × этап» — в порядке скрипта и по применимым звонкам.
    oleg_stages = operators["oleg"]["stages"]
    assert [stage["key"] for stage in oleg_stages] == [stage.key for stage in SCRIPT.stages]
    by_key = {stage["key"]: (stage["applicable"], stage["failed"]) for stage in oleg_stages}
    assert (by_key["passport"], by_key["greeting"], by_key["busy"]) == ((1, 1), (1, 0), (0, 0))

    assert [week["week_start"] for week in body["weeks"]] == ["2026-09-14"]
    assert body["weeks"][0]["calls"] == 2
    assert body["weeks"][0]["average_score"] == 90.0
    assert body["weeks"][0]["buckets"] == {"good": 1, "fair": 1, "poor": 0}

    only_oleg = (
        await api.get(
            "/api/v1/quality/summary",
            params={"from": START.isoformat(), "to": END.isoformat(), "operator_id": str(oleg)},
        )
    ).json()
    assert (only_oleg["evaluated"], only_oleg["calls"]) == (2, 1)


async def test_the_calls_list_names_failed_stages_newest_first(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    anna = await _operator(sessions, "anna")
    older = await _call(sessions, "older", started=START + timedelta(hours=9), operator_id=anna)
    newer = await _call(sessions, "newer", started=START + timedelta(hours=11), operator_id=anna)
    support = await _call(sessions, "inbound", operator_id=anna)
    for call_id, answer in (
        (older, _answer()),
        (newer, _answer(failed=["passport"], outcome="refusal")),
        (support, _answer(applies=False)),
    ):
        await evaluate_calls(
            PostgresEvaluationRepository(sessions),
            _FakeModel(answer),
            await _refs(sessions, call_id),
        )

    period = {
        "from": START.isoformat(),
        "to": END.isoformat(),
        "operator_id": str(anna),
        "applies": "true",
    }
    body = (await api.get("/api/v1/quality/calls", params=period)).json()
    # Входящее обращение — не звонок по скрипту, в списке его нет.
    assert body["total"] == 2
    assert [item["call_id"] for item in body["items"]] == [str(newer), str(older)]
    first = body["items"][0]
    assert first["failed_stages"] == ["Обязательные условия — паспорт держателя, 18+"]
    assert (first["score"], first["all_passed"], first["outcome_label"]) == (90, False, "Отказ")
    assert first["operator_login"] == "anna"
    assert body["items"][1]["failed_stages"] == []

    second = {**period, "limit": 1, "offset": 1}
    page = (await api.get("/api/v1/quality/calls", params=second)).json()
    assert page["total"] == 2
    assert [item["call_id"] for item in page["items"]] == [str(older)]


async def test_calls_without_an_operator_are_a_filter_of_their_own(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    anna = await _operator(sessions, "anna")
    mine = await _call(sessions, "mine", operator_id=anna)
    local = await _call(sessions, "local")
    for call_id in (mine, local):
        await evaluate_calls(
            PostgresEvaluationRepository(sessions),
            _FakeModel(_answer()),
            await _refs(sessions, call_id),
        )

    params = {"from": START.isoformat(), "to": END.isoformat(), "unassigned": "true"}
    calls = (await api.get("/api/v1/quality/calls", params=params)).json()
    assert [item["call_id"] for item in calls["items"]] == [str(local)]
    summary = (await api.get("/api/v1/quality/summary", params=params)).json()
    assert summary["calls"] == 1
    everyone = (
        await api.get(
            "/api/v1/quality/summary", params={"from": START.isoformat(), "to": END.isoformat()}
        )
    ).json()
    assert everyone["calls"] == 2


async def test_the_calls_list_is_filtered_and_ordered(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    anna = await _operator(sessions, "anna")
    clean = await _call(sessions, "clean", started=START + timedelta(hours=9), operator_id=anna)
    slip = await _call(sessions, "slip", started=START + timedelta(hours=10), operator_id=anna)
    poor = await _call(sessions, "poor", started=START + timedelta(hours=11), operator_id=anna)
    support = await _call(sessions, "support", started=START + timedelta(hours=12))
    for call_id, answer in (
        (clean, _answer()),
        (slip, _answer(failed=["passport"], outcome="refusal")),
        (
            poor,
            _answer(failed=["needs", "presentation", "tech_check", "closing"], outcome="callback"),
        ),
        (support, _answer(applies=False)),
    ):
        await evaluate_calls(
            PostgresEvaluationRepository(sessions),
            _FakeModel(answer),
            await _refs(sessions, call_id),
        )

    period = {"from": START.isoformat(), "to": END.isoformat()}

    async def ids(**params: str) -> list[str]:
        body = (
            await api.get("/api/v1/quality/calls", params={**period, "applies": "true", **params})
        ).json()
        assert body["total"] == len(body["items"])
        return [item["call_id"] for item in body["items"]]

    assert await ids(sort="worst") == [str(poor), str(slip), str(clean)]
    assert await ids(sort="best") == [str(clean), str(slip), str(poor)]
    # Баллы 100, 90 и 60: два хороших, средних нет, один плохой.
    assert await ids(bucket="good", sort="best") == [str(clean), str(slip)]
    assert await ids(bucket="fair") == []
    assert await ids(bucket="poor") == [str(poor)]
    assert await ids(stage="passport") == [str(slip)]
    # Тот же этап с другой стороны: где его выполнили и где он не понадобился.
    assert await ids(stage="passport", stage_state="done", sort="best") == [str(clean), str(poor)]
    assert await ids(stage="passport", stage_state="skipped") == []
    assert await ids(stage="busy", stage_state="skipped", sort="worst") == [
        str(poor),
        str(slip),
        str(clean),
    ]
    assert await ids(stage="busy", stage_state="done") == []
    assert await ids(outcome="callback") == [str(poor)]
    assert await ids(violations="true") == [str(slip)]

    body = (
        await api.get(
            "/api/v1/quality/calls", params={**period, "applies": "true", "sort": "worst"}
        )
    ).json()
    worst, middle, _best = body["items"]
    assert (worst["score"], worst["bucket"], worst["violation"]) == (60, "poor", False)
    assert (worst["passed_count"], worst["stage_count"]) == (6, 10)
    assert (middle["bucket"], middle["violation"], middle["operator_id"]) == (
        "good",
        True,
        str(anna),
    )
    assert [stage["key"] for stage in worst["stages"]] == [stage.key for stage in SCRIPT.stages]
    strip = {stage["key"]: (stage["applicable"], stage["passed"]) for stage in worst["stages"]}
    assert (strip["needs"], strip["greeting"], strip["busy"]) == (
        (True, False),
        (True, True),
        (False, True),
    )

    # Звонок вне скрипта — отдельным списком, с причиной вместо балла.
    skipped = (await api.get("/api/v1/quality/calls", params={**period, "applies": "false"})).json()
    assert [item["call_id"] for item in skipped["items"]] == [str(support)]
    outside = skipped["items"][0]
    assert (outside["script_applies"], outside["score"], outside["bucket"]) == (False, None, None)
    assert (outside["summary"], outside["stages"]) == ("Входящее обращение.", [])


async def test_an_unknown_filter_value_is_refused_not_answered_with_nothing(
    api: httpx.AsyncClient,
) -> None:
    for params, detail in (
        ({"stage": "pasport"}, "unknown_stage"),
        ({"outcome": "sold"}, "unknown_outcome"),
    ):
        response = await api.get("/api/v1/quality/calls", params=params)
        assert (response.status_code, response.json()["detail"]) == (422, detail)
    assert (await api.get("/api/v1/quality/calls", params={"bucket": "bad"})).status_code == 422
    assert (await api.get("/api/v1/quality/calls", params={"sort": "random"})).status_code == 422
    wrong_state = {"stage": "passport", "stage_state": "maybe"}
    assert (await api.get("/api/v1/quality/calls", params=wrong_state)).status_code == 422


async def test_failed_and_pending_calls_remain_visible_without_lowering_scores(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    anna = await _operator(sessions, "anna")
    good = await _call(sessions, "good", operator_id=anna)
    support = await _call(sessions, "support", operator_id=anna)
    invalid = await _call(sessions, "invalid", operator_id=anna)
    ambiguous = await _call(sessions, "ambiguous", operator_id=anna)
    pending = await _call(sessions, "pending", operator_id=anna)
    untranscribed = await _call(sessions, "untranscribed", transcribed=False)
    await _call(sessions, "outside_period", started=START - timedelta(days=1))
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_transcript_segments SET speaker='unknown' WHERE call_id=:c"),
            {"c": ambiguous},
        )
    for call_id, answer in (
        (good, _answer()),
        (support, _answer(applies=False)),
        (invalid, "not JSON"),
        (ambiguous, _UNKNOWN_ROLES),
    ):
        await evaluate_calls(
            PostgresEvaluationRepository(sessions),
            _FakeModel(answer),
            await _refs(sessions, call_id),
        )

    period = {"from": START.isoformat(), "to": END.isoformat()}
    summary = (await api.get("/api/v1/quality/summary", params=period)).json()
    assert (summary["total"], summary["evaluated"], summary["calls"]) == (6, 2, 1)
    assert (summary["failed"], summary["pending"], summary["average_score"]) == (2, 2, 100)
    assert summary["buckets"] == {"good": 1, "fair": 0, "poor": 0}
    body = (await api.get("/api/v1/quality/calls", params=period)).json()
    assert body["total"] == 6
    items = {item["call_id"]: item for item in body["items"]}
    assert set(items) == {
        str(c) for c in (good, support, invalid, ambiguous, pending, untranscribed)
    }
    for call_id in (invalid, ambiguous, pending, untranscribed):
        item = items[str(call_id)]
        assert item["score"] is None and item["script_applies"] is None
        assert item["all_passed"] is None and item["stages"] == []
        assert not item["violation"]
    assert items[str(invalid)]["failure_reason"] == "invalid_evaluation"
    assert items[str(ambiguous)]["failure_reason"] == "roles_undetermined"
    assert items[str(pending)]["evaluation_status"] == "pending"
    assert items[str(support)]["evaluation_status"] == "evaluated"
    assert items[str(support)]["script_applies"] is False
    for status, expected in (
        ("evaluated", {str(good), str(support)}),
        ("failed", {str(invalid), str(ambiguous)}),
        ("pending", {str(pending), str(untranscribed)}),
    ):
        subset = (
            await api.get("/api/v1/quality/calls", params={**period, "evaluation_status": status})
        ).json()
        assert subset["total"] == 2
        assert {item["call_id"] for item in subset["items"]} == expected
    page = (
        await api.get("/api/v1/quality/calls", params={**period, "limit": 2, "offset": 4})
    ).json()
    assert page["total"] == 6 and len(page["items"]) == 2
    scope = {**period, "operator_id": str(anna)}
    scoped = (await api.get("/api/v1/quality/summary", params=scope)).json()
    assert (scoped["total"], scoped["failed"], scoped["pending"]) == (5, 2, 1)
    assert (await api.get("/api/v1/quality/calls", params=scope)).json()["total"] == 5


async def test_calls_without_transcripts_are_visible_and_stale_failures_become_pending(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    no_transcript = await _call(sessions, "no_transcript")
    failed = await _call(sessions, "failed")
    await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel("not JSON"),
        await _refs(sessions, failed),
    )
    async with sessions() as session, session.begin():
        await session.execute(
            text("DELETE FROM speech_transcripts WHERE call_id=:c"), {"c": no_transcript}
        )
    await _retranscribe(sessions, failed)
    period = {"from": START.isoformat(), "to": END.isoformat()}
    summary = (await api.get("/api/v1/quality/summary", params=period)).json()
    assert (summary["total"], summary["failed"], summary["pending"]) == (2, 0, 2)
    listing = (await api.get("/api/v1/quality/calls", params=period)).json()
    assert listing["total"] == 2
    for item in listing["items"]:
        assert item["evaluation_status"] == "pending"
        assert item["failure_reason"] is None and item["score"] is None


async def test_an_evaluation_older_than_the_transcript_is_stale(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    """Переобработка заменяет реплики: прежняя оценка ссылалась бы на чужой разговор."""
    call_id = await _call(sessions, "reprocessed")
    await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel(_answer()),
        await _refs(sessions, call_id),
    )
    assert (await api.get(f"/api/v1/calls/{call_id}/quality")).status_code == 200
    assert await find_candidates(sessions, date_from=START, date_to=END) == []

    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE speech_transcripts SET created_at = now() + interval '1 hour' "
                "WHERE call_id = :call_id"
            ),
            {"call_id": call_id},
        )

    card = await api.get(f"/api/v1/calls/{call_id}/quality")
    assert (card.status_code, card.json()["detail"]) == (404, "not_evaluated")
    period = {"from": START.isoformat(), "to": END.isoformat()}
    assert (await api.get("/api/v1/quality/summary", params=period)).json()["evaluated"] == 0
    listing = (await api.get("/api/v1/quality/calls", params=period)).json()
    assert listing["total"] == 1
    assert listing["items"][0]["evaluation_status"] == "pending"
    assert listing["items"][0]["score"] is None
    # И команда оценки берёт звонок заново.
    found = await find_candidates(sessions, date_from=START, date_to=END)
    assert [ref.call_id for ref in found] == [call_id]


class _RefusingModel:
    """Отказывает на первом звонке причиной `reason`, на остальных отвечает."""

    model = "fake-model"

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self.calls = 0

    async def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        self.calls += 1
        if self.calls == 1:
            raise QualityModelError(self._reason, retryable=False)
        return _answer()


async def test_a_call_the_model_cannot_take_does_not_stop_the_others(
    sessions: Sessions,
) -> None:
    first = await _call(sessions, "too_long", started=START + timedelta(hours=11))
    second = await _call(sessions, "normal", started=START + timedelta(hours=9))
    calls = await find_candidates(sessions, date_from=START, date_to=END)
    assert [ref.call_id for ref in calls] == [first, second]

    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions), _RefusingModel("http_400"), calls
    )
    assert (report.evaluated, report.failed) == (1, 1)
    rows = await _rows(sessions)
    assert [row["call_id"] for row in rows if row["failure_reason"] is None] == [second]
    assert next(row for row in rows if row["call_id"] == first)["failure_reason"] == "model_error"


async def test_mono_roles_and_evaluation_are_saved_together(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    call_id = await _call(sessions, "mono")
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_transcripts SET role_method = 'none' WHERE call_id = :c"),
            {"c": call_id},
        )
        await session.execute(
            text("UPDATE speech_transcript_segments SET speaker = 'unknown' WHERE call_id = :c"),
            {"c": call_id},
        )
    model = _FakeModel(
        '{"roles": [{"seq": 0, "speaker": "operator"}, {"seq": 1, "speaker": "client"}]}',
        _answer(),
    )
    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions), model, await _refs(sessions, call_id)
    )
    assert (report.evaluated, report.failed) == (1, 0)
    assert len(model.prompts) == 3
    assert "[0] 00:00 Оператор:" in model.prompts[1]
    async with sessions() as session:
        method = await session.scalar(
            text("SELECT role_method FROM speech_transcripts WHERE call_id = :c"), {"c": call_id}
        )
        roles = (
            (
                await session.execute(
                    text(
                        "SELECT speaker FROM speech_transcript_segments "
                        "WHERE call_id = :c ORDER BY seq"
                    ),
                    {"c": call_id},
                )
            )
            .scalars()
            .all()
        )
    assert method == "llm_attribution"
    assert roles == ["operator", "client"]
    assert (await api.get(f"/api/v1/calls/{call_id}/quality")).json()["score"] == 100
    assert await find_candidates(sessions, date_from=START, date_to=END) == []


async def _make_mono(sessions: Sessions, call_id: uuid.UUID) -> None:
    """Одноканальная запись: расшифровка без единой роли."""
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_transcripts SET role_method = 'none' WHERE call_id = :c"),
            {"c": call_id},
        )
        await session.execute(
            text("UPDATE speech_transcript_segments SET speaker = 'unknown' WHERE call_id = :c"),
            {"c": call_id},
        )


async def _roles_in_base(sessions: Sessions, call_id: uuid.UUID) -> tuple[str, list[str]]:
    async with sessions() as session:
        method = await session.scalar(
            text("SELECT role_method FROM speech_transcripts WHERE call_id = :c"), {"c": call_id}
        )
        speakers = (
            await session.execute(
                text(
                    "SELECT speaker FROM speech_transcript_segments WHERE call_id = :c ORDER BY seq"
                ),
                {"c": call_id},
            )
        ).scalars()
        return str(method), [str(speaker) for speaker in speakers]


_MONO_ROLES = '{"roles": [{"seq": 0, "speaker": "operator"}, {"seq": 1, "speaker": "client"}]}'


async def test_mono_roles_stay_when_the_scoring_fails(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    """Роли названы, оценка не прошла проверку: роли остаются, оценки нет."""
    call_id = await _call(sessions, "mono_unscored")
    await _make_mono(sessions, call_id)

    model = _FakeModel(_MONO_ROLES, "not JSON")
    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions), model, await _refs(sessions, call_id)
    )
    assert (report.evaluated, report.failed) == (0, 1)
    assert await _roles_in_base(sessions, call_id) == ("llm_attribution", ["operator", "client"])
    row = (await _rows(sessions))[0]
    assert (row["failure_reason"], row["score"]) == ("invalid_evaluation", None)
    response = await api.get(f"/api/v1/calls/{call_id}/quality")
    assert (response.status_code, response.json()["detail"]) == (404, "quality_invalid_evaluation")

    # Следующий прогон о ролях модель уже не спрашивает: они в расшифровке.
    model = _FakeModel(_answer())
    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions), model, await _refs(sessions, call_id)
    )
    assert (report.evaluated, report.failed) == (1, 0)
    assert len(model.prompts) == 2
    assert "[0] 00:00 Оператор:" in model.prompts[0]
    assert (await api.get(f"/api/v1/calls/{call_id}/quality")).json()["score"] == 100


async def test_mono_roles_are_not_written_into_a_transcript_that_changed(
    sessions: Sessions,
) -> None:
    """Звонок переобработали, пока модель называла роли: в новые реплики они не идут."""
    call_id = await _call(sessions, "mono_changed")
    await _make_mono(sessions, call_id)

    class _ChangingModel(_FakeModel):
        async def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE speech_transcript_segments SET text_raw = 'Новая реплика' "
                        "WHERE call_id = :c AND seq = 0"
                    ),
                    {"c": call_id},
                )
            return _MONO_ROLES

    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions), _ChangingModel(), await _refs(sessions, call_id)
    )
    assert (report.evaluated, report.failed) == (0, 1)
    assert await _roles_in_base(sessions, call_id) == ("none", ["unknown", "unknown"])
    assert await _rows(sessions) == []


async def _expect_no_evaluation(
    sessions: Sessions, api: httpx.AsyncClient, call_id: uuid.UUID
) -> None:
    """Оценки нет ни в карточке, ни в сводке, ни в списке — и команда берёт звонок заново."""
    response = await api.get(f"/api/v1/calls/{call_id}/quality")
    assert (response.status_code, response.json()["detail"]) == (404, "not_evaluated")
    period = {"from": START.isoformat(), "to": END.isoformat()}
    assert (await api.get("/api/v1/quality/summary", params=period)).json()["evaluated"] == 0
    listing = (await api.get("/api/v1/quality/calls", params=period)).json()
    assert listing["total"] == 1
    assert listing["items"][0]["evaluation_status"] == "pending"
    assert listing["items"][0]["score"] is None
    assert [
        ref.call_id for ref in await find_candidates(sessions, date_from=START, date_to=END)
    ] == [call_id]


async def test_roles_written_by_the_analysis_drop_the_evaluation(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    """Разбор расставил роли уже после оценки: она стояла на прежних и снимается сразу.

    Расшифровка от смены ролей не переписывается, и по времени оценка осталась бы
    действующей. Реплики при чтении не сверяются, поэтому снимает её тот, кто
    меняет роли.
    """
    call_id = await _call(sessions, "analysed_later")
    await _make_mono(sessions, call_id)
    model = _FakeModel(_MONO_ROLES, _answer())
    await evaluate_calls(
        PostgresEvaluationRepository(sessions), model, await _refs(sessions, call_id)
    )

    # Разбор без ролей оценку не трогает: менять ему нечего.
    work = AnalyzeWork(node_id=None, path=(), sentiment=None, summary=None)
    async with sessions() as session, session.begin():
        await store_result(session, call_id, work)
    assert (await api.get(f"/api/v1/calls/{call_id}/quality")).json()["score"] == 100

    # Разбор читал звонок ещё без ролей, а записал свои уже после оценки.
    work = AnalyzeWork(
        node_id=None,
        path=(),
        sentiment=None,
        summary=None,
        roles=((0, Speaker.CLIENT), (1, Speaker.OPERATOR)),
    )
    async with sessions() as session, session.begin():
        await store_result(session, call_id, work)

    assert await _roles_in_base(sessions, call_id) == ("llm_attribution", ["client", "operator"])
    assert await _rows(sessions) == []
    await _expect_no_evaluation(sessions, api, call_id)


async def test_a_reanalysis_drops_the_evaluation_only_with_the_roles_the_model_named(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    """Повторный разбор стирает роли, названные моделью, — и оценку, стоявшую на них.

    Роли, измеренные по каналам, он не трогает, и оценка по ним остаётся: иначе
    повторный разбор истории снимал бы оценки со всех стерео-записей.
    """
    mono = await _call(sessions, "mono", started=START + timedelta(hours=11))
    stereo = await _call(sessions, "stereo", started=START + timedelta(hours=9))
    await _make_mono(sessions, mono)
    await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel(_MONO_ROLES, _answer()),
        await _refs(sessions, mono),
    )
    await evaluate_calls(
        PostgresEvaluationRepository(sessions), _FakeModel(_answer()), await _refs(sessions, stereo)
    )

    for call_id in (mono, stereo):
        async with sessions() as session, session.begin():
            await reprocess(session, call_id, JobKind.ANALYZE)

    assert await _roles_in_base(sessions, mono) == ("none", ["unknown", "unknown"])
    assert await _roles_in_base(sessions, stereo) == ("stereo_energy", ["operator", "client"])
    assert [row["call_id"] for row in await _rows(sessions)] == [stereo]
    card = await api.get(f"/api/v1/calls/{mono}/quality")
    assert (card.status_code, card.json()["detail"]) == (404, "not_evaluated")
    assert (await api.get(f"/api/v1/calls/{stereo}/quality")).json()["score"] == 100
    found = await find_candidates(sessions, date_from=START, date_to=END)
    assert [ref.call_id for ref in found] == [mono]


async def test_ambiguous_roles_after_a_reanalysis_leave_a_named_failure_not_the_old_score(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    call_id = await _call(sessions, "ambiguous")
    await _make_mono(sessions, call_id)
    await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel(_MONO_ROLES, _answer()),
        await _refs(sessions, call_id),
    )
    async with sessions() as session, session.begin():
        await reprocess(session, call_id, JobKind.ANALYZE)

    model = _FakeModel(_UNKNOWN_ROLES)
    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions), model, await _refs(sessions, call_id)
    )
    assert (report.evaluated, report.failed) == (0, 1)
    row = (await _rows(sessions))[0]
    assert row["failure_reason"] == "roles_undetermined"
    assert row["score"] is None and row["script_applies"] is None
    assert row["stages"] == []
    response = await api.get(f"/api/v1/calls/{call_id}/quality")
    assert (response.status_code, response.json()["detail"]) == (404, "quality_roles_undetermined")
    period = {"from": START.isoformat(), "to": END.isoformat()}
    assert (await api.get("/api/v1/quality/summary", params=period)).json()["evaluated"] == 0
    assert [
        ref.call_id for ref in await find_candidates(sessions, date_from=START, date_to=END)
    ] == [call_id]

    # Следующий запуск повторяет отказ и заменяет его успешной оценкой.
    model = _FakeModel(_MONO_ROLES, _answer())
    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions), model, await _refs(sessions, call_id)
    )
    assert (report.evaluated, report.failed) == (1, 0)
    row = (await _rows(sessions))[0]
    assert row["failure_reason"] is None and row["score"] == 100
    assert (await api.get(f"/api/v1/calls/{call_id}/quality")).json()["score"] == 100
    assert await find_candidates(sessions, date_from=START, date_to=END) == []


async def test_roles_named_by_the_command_drop_the_evaluation_made_on_the_old_ones(
    sessions: Sessions,
) -> None:
    """Команда сама назвала роли заново: баллы, поставленные по прежним, не остаются.

    Осечка модели действующую оценку не затирает
    (`test_a_failed_rerun_keeps_the_evaluation_that_still_stands`), но здесь
    оценка перестала действовать раньше осечки — в момент, когда записаны роли.
    """
    call_id = await _call(sessions, "renamed")
    await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel(_answer()),
        await _refs(sessions, call_id),
    )
    # Роли стёрты мимо конвейера: оценку это не снимает, а команда видит моно-запись.
    await _make_mono(sessions, call_id)

    model = _FakeModel(_MONO_ROLES, "not JSON")
    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions), model, await _refs(sessions, call_id)
    )
    assert (report.evaluated, report.failed) == (0, 1)
    assert await _roles_in_base(sessions, call_id) == ("llm_attribution", ["operator", "client"])
    rows = await _rows(sessions)
    assert [(row["failure_reason"], row["score"]) for row in rows] == [("invalid_evaluation", None)]


async def test_the_check_on_demand_drops_evaluations_the_transcript_moved_away_from(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    """`--drop-stale`: реплики правили мимо конвейера — оценка по прежним находится и удаляется."""
    kept = await _call(sessions, "kept", started=START + timedelta(hours=9))
    edited = await _call(sessions, "edited", started=START + timedelta(hours=10))
    legacy = await _call(sessions, "legacy", started=START + timedelta(hours=11))
    for call_id in (kept, edited, legacy):
        await evaluate_calls(
            PostgresEvaluationRepository(sessions),
            _FakeModel(_answer()),
            await _refs(sessions, call_id),
        )
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_transcript_segments SET speaker = 'unknown' WHERE call_id = :c"),
            {"c": edited},
        )
        # Оценка, поставленная до появления отпечатка.
        await session.execute(
            text(
                "UPDATE speech_call_evaluations SET transcript_fingerprint = NULL "
                "WHERE call_id = :c"
            ),
            {"c": legacy},
        )

    # Экран реплики не сверяет: до сверки такая оценка показывается.
    assert (await api.get(f"/api/v1/calls/{edited}/quality")).json()["score"] == 100
    assert await find_candidates(sessions, date_from=START, date_to=END) == []

    assert await drop_stale(sessions, apply=False) == 2
    assert len(await _rows(sessions)) == 3
    assert await drop_stale(sessions, apply=True) == 2
    assert [row["call_id"] for row in await _rows(sessions)] == [kept]
    found = await find_candidates(sessions, date_from=START, date_to=END)
    assert [ref.call_id for ref in found] == [legacy, edited]
    assert await drop_stale(sessions, apply=True) == 0


async def test_a_reply_without_a_role_is_kept_and_the_call_is_scored(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    """Стерео-запись с одной неясной репликой: оценка есть, роль не выдумана."""
    call_id = await _call(sessions, "overlap")
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE speech_transcript_segments SET speaker = 'unknown' "
                "WHERE call_id = :c AND seq = 1"
            ),
            {"c": call_id},
        )
    model = _FakeModel('{"roles": [{"seq": 1, "speaker": "unknown"}]}', _answer())
    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions), model, await _refs(sessions, call_id)
    )
    assert (report.evaluated, report.failed) == (1, 0)
    async with sessions() as session:
        speaker = await session.scalar(
            text("SELECT speaker FROM speech_transcript_segments WHERE call_id = :c AND seq = 1"),
            {"c": call_id},
        )
        method = await session.scalar(
            text("SELECT role_method FROM speech_transcripts WHERE call_id = :c"), {"c": call_id}
        )
    assert (speaker, method) == ("unknown", "stereo_energy")
    assert (await api.get(f"/api/v1/calls/{call_id}/quality")).json()["score"] == 100
    assert await find_candidates(sessions, date_from=START, date_to=END) == []


async def test_a_failed_rerun_keeps_the_evaluation_that_still_stands(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    """`--force` и `--call` пересчитывают оценённое: осечка модели не стирает баллы."""
    call_id = await _call(sessions, "rerun")
    await evaluate_calls(
        PostgresEvaluationRepository(sessions),
        _FakeModel(_answer()),
        await _refs(sessions, call_id),
    )

    for model in (_FakeModel("not JSON"), _RefusingModel("http_500")):
        report = await evaluate_calls(
            PostgresEvaluationRepository(sessions), model, await _refs(sessions, call_id)
        )
        assert (report.evaluated, report.failed) == (0, 1)
        row = (await _rows(sessions))[0]
        assert (row["failure_reason"], row["score"], row["model_name"]) == (None, 100, "fake-model")
    assert (await api.get(f"/api/v1/calls/{call_id}/quality")).json()["score"] == 100
    assert await find_candidates(sessions, date_from=START, date_to=END) == []


async def test_a_call_that_will_never_have_a_transcript_is_not_pending(
    sessions: Sessions, api: httpx.AsyncClient
) -> None:
    short = await _call(sessions, "short")
    waiting = await _call(sessions, "waiting", transcribed=False)
    async with sessions() as session, session.begin():
        await session.execute(text("DELETE FROM speech_transcripts WHERE call_id=:c"), {"c": short})
        await session.execute(
            text("UPDATE speech_calls SET processing_status='skipped_short' WHERE id=:c"),
            {"c": short},
        )
        await session.execute(
            text("UPDATE speech_calls SET processing_status='transcribing' WHERE id=:c"),
            {"c": waiting},
        )
    period = {"from": START.isoformat(), "to": END.isoformat()}
    summary = (await api.get("/api/v1/quality/summary", params=period)).json()
    assert (summary["total"], summary["pending"], summary["unavailable"]) == (2, 1, 1)
    listing = (await api.get("/api/v1/quality/calls", params=period)).json()
    statuses = {item["call_id"]: item["evaluation_status"] for item in listing["items"]}
    assert statuses == {str(short): "unavailable", str(waiting): "pending"}
    only = (
        await api.get(
            "/api/v1/quality/calls", params={**period, "evaluation_status": "unavailable"}
        )
    ).json()
    assert [item["call_id"] for item in only["items"]] == [str(short)]


async def test_transcript_changed_while_the_model_runs_is_not_overwritten(
    sessions: Sessions,
) -> None:
    call_id = await _call(sessions, "concurrent")

    class _ChangingModel(_FakeModel):
        async def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE speech_transcript_segments SET text_raw = 'Новая реплика' "
                        "WHERE call_id = :c AND seq = 0"
                    ),
                    {"c": call_id},
                )
            return _answer()

    report = await evaluate_calls(
        PostgresEvaluationRepository(sessions), _ChangingModel(), await _refs(sessions, call_id)
    )
    assert (report.evaluated, report.failed) == (0, 1)
    assert await _rows(sessions) == []


async def test_a_wrong_key_stops_the_batch_at_once(sessions: Sessions) -> None:
    await _call(sessions, "one", started=START + timedelta(hours=11))
    await _call(sessions, "two", started=START + timedelta(hours=9))
    calls = await find_candidates(sessions, date_from=START, date_to=END)
    model = _RefusingModel("http_401")
    with pytest.raises(QualityModelError):
        await evaluate_calls(PostgresEvaluationRepository(sessions), model, calls)
    assert model.calls == 1
    assert await _rows(sessions) == []
