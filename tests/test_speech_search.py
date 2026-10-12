"""Поиск по разговорам.

Полнотекстовый поиск — свойство Postgres, а не Python: русская морфология,
`websearch_to_tsquery`, фразовый оператор и `ts_headline` на подставном объекте
не проверяются вовсе, потому что подставной объект вернёт то, что мы от него
ждём.

Поэтому пропуск повешен на **фикстуру корпуса**, а не на модуль: разбор режима
и арифметика доли базы не требуют и выполняются везде. Модульный `skipif`
уносил бы их вместе с остальным — ровно та ошибка, которая уже была найдена в
`test_speech_queue_protocol.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.infrastructure.persistence.search import (
    HIGHLIGHT_START,
    HIGHLIGHT_STOP,
    PAGE_SIZE,
    SearchFilters,
    SearchPage,
    parse_mode,
    search_calls,
)
from tests.speech_db_support import speech_test_database_url

_TEST_DATABASE_URL = speech_test_database_url()

DAY = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)

#: Разговоры с известным содержимым: по ним считается вручную то, что должен
#: посчитать запрос.
CORPUS: list[tuple[str, list[str]]] = [
    ("phrase-1", ["не работает интернет уже второй день", "проверьте оборудование"]),
    ("phrase-2", ["интернет не работает совсем", "перезагрузите роутер"]),
    ("words-1", ["интернет отличный, а вот телевизор не работает", "понял вас"]),
    ("twice-1", ["не работает интернет", "и снова не работает интернет"]),
    ("morph-1", ["у меня не работали интернеты всю неделю", "разберёмся"]),
    ("miss-1", ["хочу сменить тариф на более дешёвый", "подберём"]),
    # Кавычки-ёлочки в самой расшифровке. Пока `ts_headline` помечал найденное
    # ими же, интерфейсу было нечем отличить свою метку от названия компании.
    ("quoted-1", ["подключите мне «Кошелёк», интернет не нужен", "оформляю"]),
]

#: Сколько «шумовых» звонков досыпается сверх корпуса, чтобы счётчик периода
#: считался не по шести строкам. Постановка требовала тысячу.
NOISE = 1000


@pytest.fixture()
async def corpus() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Корпус в живой базе. Без базы тест пропускается, но не молча."""
    if not _TEST_DATABASE_URL:
        pytest.skip("SPEECH_POSTGRES_TEST_URL is required for the full-text search proof")

    engine = create_async_engine(_TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _clean() -> None:
        async with sessions() as session, session.begin():
            await session.execute(text("DELETE FROM speech_transcript_segments"))
            await session.execute(text("DELETE FROM speech_transcripts"))
            await session.execute(text("DELETE FROM speech_jobs"))
            await session.execute(text("DELETE FROM speech_calls"))

    await _clean()
    async with sessions() as session, session.begin():
        await _seed(session)
    yield sessions
    await _clean()
    await engine.dispose()


async def _seed(session: AsyncSession) -> None:
    for index, (name, replies) in enumerate(CORPUS):
        call_id = uuid.uuid4()
        started = DAY - timedelta(minutes=index)
        await session.execute(
            text(
                "INSERT INTO speech_calls (id, session_id, direction, started_at, ended_at, "
                "duration_s, audio_status, processing_status, analysis_status, source_snapshot, "
                "first_seen_at, created_at, updated_at) "
                "VALUES (:id, :sid, 'inbound', :s, :s, 120, 'stored', 'done', 'done', '{}', "
                ":s, :s, :s)"
            ),
            {"id": call_id, "sid": f"node_1_domain_1_nauss_1_1_{name}", "s": started},
        )
        transcript_id = (
            await session.execute(
                text(
                    "INSERT INTO speech_transcripts (id, call_id, engine, model_name, language, "
                    "role_method, segments_count, created_at) "
                    "VALUES (gen_random_uuid(), :c, 'test', 'large-v3', 'ru', 'stereo_energy', "
                    ":n, now()) RETURNING id"
                ),
                {"c": call_id, "n": len(replies)},
            )
        ).scalar_one()
        for seq, reply in enumerate(replies):
            await session.execute(
                text(
                    "INSERT INTO speech_transcript_segments (transcript_id, call_id, seq, "
                    "speaker, start_ms, end_ms, text_raw) "
                    "VALUES (:t, :c, :seq, 'client', :start, :end, :raw)"
                ),
                {
                    "t": transcript_id,
                    "c": call_id,
                    "seq": seq,
                    "start": seq * 5000,
                    "end": seq * 5000 + 4000,
                    "raw": reply,
                },
            )

    # Шум: звонки без транскриптов. Они не находятся, но считаются в периоде —
    # то есть проверяют, что доля от периода берётся из звонков, а не из
    # найденного.
    await session.execute(
        text(
            "INSERT INTO speech_calls (id, session_id, direction, started_at, ended_at, "
            "duration_s, audio_status, processing_status, analysis_status, source_snapshot, "
            "first_seen_at, created_at, updated_at) "
            "SELECT gen_random_uuid(), 'noise_' || g, 'inbound', :s, :s, 60, 'stored', "
            "'done', 'done', '{}', :s, :s, :s FROM generate_series(1, :n) AS g"
        ),
        {"s": DAY - timedelta(hours=1), "n": NOISE},
    )


# --------------------------------------------------------------------------- #
# Разбор режима — без базы
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("query", "mode"),
    [
        ('"не работает интернет"', "phrase"),
        ("не работает интернет", "any_order"),
        ('интернет "не работает"', "phrase"),
        ("", "browse"),
        ("   ", "browse"),
    ],
    ids=lambda v: repr(v)[:28],
)
def test_the_mode_shown_to_the_user_follows_the_quotes(query: str, mode: str) -> None:
    """Подпись «точная фраза» / «слова в любом порядке» (SRC-01).

    Режим определяется наличием кавычек, а сам разбор делает
    `websearch_to_tsquery`. Разбирать кавычки своим кодом значило бы завести
    второй синтаксис рядом с настоящим и однажды с ним разойтись.
    """
    assert parse_mode(query) == mode


def test_the_share_of_the_period_is_zero_on_an_empty_period() -> None:
    """«0%» — осмысленный ответ; деление на ноль — нет."""
    assert (
        SearchPage(mode="any_order", total_matches=0, total_calls=0, period_calls=0).share_of_period
        == 0.0
    )
    assert SearchPage(
        mode="any_order", total_matches=9, total_calls=3, period_calls=12
    ).share_of_period == pytest.approx(0.25)


# --------------------------------------------------------------------------- #
# Поиск на живой базе
# --------------------------------------------------------------------------- #


async def test_words_without_quotes_match_in_any_order(corpus) -> None:
    async with corpus() as session:
        page = await search_calls(session, query="интернет не работает")

    assert page.mode == "any_order"
    found = {item.session_id.split("_")[-1] for item in page.items}
    # `words-1` содержит оба слова, но не подряд — без кавычек это попадание.
    assert {"phrase-1", "phrase-2", "words-1", "twice-1"} <= found
    assert "miss-1" not in found


async def test_quotes_demand_the_words_in_order(corpus) -> None:
    """Фразовый режим — то, ради чего в строке поиска вообще есть кавычки."""
    async with corpus() as session:
        page = await search_calls(session, query='"не работает интернет"')

    assert page.mode == "phrase"
    found = {item.session_id.split("_")[-1] for item in page.items}
    assert "phrase-1" in found and "twice-1" in found
    assert "words-1" not in found, "здесь слова стоят не подряд"
    assert "phrase-2" not in found, "здесь между ними другое слово"


async def test_russian_morphology_is_applied(corpus) -> None:
    """«интернет» обязан находить «интернеты», иначе поиск бесполезен."""
    async with corpus() as session:
        page = await search_calls(session, query="интернет")

    assert "morph-1" in {item.session_id.split("_")[-1] for item in page.items}


async def test_counters_match_a_manual_count(corpus) -> None:
    """SRC-03: «Найдено N совпадений в M разговорах» и доля M от периода.

    Считается тем же проходом, что и страница: отдельными запросами счётчик мог
    бы не сойтись со списком под ним.
    """
    async with corpus() as session:
        page = await search_calls(session, query="интернет")

    by_call = {item.session_id.split("_")[-1]: item.match_count for item in page.items}
    assert by_call["twice-1"] == 2, "две реплики с попаданием в одном разговоре"
    assert by_call["phrase-1"] == 1
    assert page.total_matches == sum(by_call.values())
    assert page.total_calls == len(by_call)
    assert page.period_calls == len(CORPUS) + NOISE
    assert page.share_of_period == pytest.approx(page.total_calls / page.period_calls)


async def test_a_quote_marks_only_what_was_found(corpus) -> None:
    """Расшифровка русской речи содержит «ёлочки» сама по себе.

    Пока `ts_headline` помечал найденное ими же, «Кошелёк» в тексте был для
    интерфейса неотличим от совпадения, и подсветка вставала на название
    компании. Управляющие символы в расшифровке появиться не могут.
    """
    async with corpus() as session:
        page = await search_calls(session, query="интернет")

    quoted = next(i for i in page.items if i.session_id.endswith("quoted-1"))
    text = quoted.quotes[0].text

    assert "«Кошелёк»" in text, "чужие кавычки остаются на месте"
    marked = text.split(HIGHLIGHT_START)[1].split(HIGHLIGHT_STOP)[0]
    assert marked.lower().startswith("интернет"), f"подсвечено не то: {marked!r}"
    assert text.count(HIGHLIGHT_START) == text.count(HIGHLIGHT_STOP) == 1


async def test_a_card_carries_quotes_with_timecodes(corpus) -> None:
    """Цитаты с таймкодом — по ним пользователь перематывает запись (SRC-04)."""
    async with corpus() as session:
        page = await search_calls(session, query="интернет")

    twice = next(i for i in page.items if i.session_id.endswith("twice-1"))
    assert len(twice.quotes) == 2
    assert all(
        HIGHLIGHT_START in quote.text and HIGHLIGHT_STOP in quote.text for quote in twice.quotes
    )
    assert [quote.start_ms for quote in twice.quotes] == [0, 5000]
    assert twice.hit_seqs == (0, 1), "все попадания, а не только показанные две цитаты"


async def test_only_two_quotes_are_returned_however_many_matched(corpus) -> None:
    async with corpus() as session:
        page = await search_calls(session, query="интернет")

    assert all(len(item.quotes) <= 2 for item in page.items)


async def test_a_period_filter_narrows_both_the_page_and_the_counters(corpus) -> None:
    async with corpus() as session:
        page = await search_calls(
            session,
            query="интернет",
            filters=SearchFilters(
                date_from=DAY - timedelta(minutes=2),
                date_to=DAY + timedelta(minutes=1),
            ),
        )

    assert page.period_calls == 3, "шум лежит часом раньше и в период не попал"
    assert page.total_calls <= 3


async def test_an_empty_query_browses_instead_of_finding_nothing(corpus) -> None:
    """Пустой запрос — не «ничего не найдено», а «показать всё за период».

    Иначе фильтры без слова были бы бесполезны: пустой `tsquery` не совпадает ни
    с чем, и сузивший период пользователь увидел бы пустой экран.
    """
    async with corpus() as session:
        page = await search_calls(session, query="   ")

    assert page.mode == "browse"
    assert page.period_calls == len(CORPUS) + NOISE
    assert page.total_calls == page.period_calls
    assert page.total_matches == 0
    assert len(page.items) == PAGE_SIZE
    assert all(item.quotes == () for item in page.items)


async def test_a_query_that_matches_nothing_still_reports_the_period(corpus) -> None:
    """Состояния 01 и 02 прототипа различаются именно этим числом.

    «Ничего не найдено за период со ста звонками» и «в периоде нет звонков» —
    разные экраны, и без счётчика периода они неразличимы.
    """
    async with corpus() as session:
        page = await search_calls(session, query="телепортация")

    assert page.items == ()
    assert page.total_calls == 0
    assert page.total_matches == 0
    assert page.period_calls == len(CORPUS) + NOISE


async def test_results_are_ordered_by_date_newest_first(corpus) -> None:
    async with corpus() as session:
        page = await search_calls(session, query="интернет")

    dates = [item.started_at for item in page.items]
    assert dates == sorted(dates, reverse=True)


async def test_paging_does_not_lose_or_repeat_a_call(corpus) -> None:
    """Сортировка добита `id`: по одному только времени порядок между
    одинаковыми отметками не определён, и страница 2 могла бы повторить строку
    со страницы 1."""
    async with corpus() as session:
        first = await search_calls(session, query="", limit=10, offset=0)
        second = await search_calls(session, query="", limit=10, offset=10)

    ids = [i.call_id for i in first.items] + [i.call_id for i in second.items]
    assert len(ids) == len(set(ids)) == 20
