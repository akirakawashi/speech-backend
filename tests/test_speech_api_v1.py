"""Пользовательские эндпойнты `/api/v1`.

Проверяются две разные вещи, и смешивать их нельзя.

**Права** — без базы: кто что может, решает `Principal`, и это чистая
арифметика ролей. Она обязана выполняться всегда, потому что ошибка здесь
открывает записи разговоров.

**Ответы ручек** — на живой базе: их смысл в данных, и подставная сессия
проверила бы только то, что мы правильно списали собственные же заглушки.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.application.authorization import open_mode_principal, roles_from_groups
from src.application.calls.period import is_all_time, resolve_period
from src.di.http import create_app
from src.di.requests import session_dep
from src.domain.calls.contracts import (
    SPEECH_API_CONTRACT_VERSION,
    SPEECH_ROLE_ADMIN,
    SPEECH_ROLE_EDITOR,
    SPEECH_ROLE_READER,
)
from src.infrastructure.auth.identity import role_aliases
from src.infrastructure.persistence.classifier import label_key
from src.infrastructure.persistence.search import HIGHLIGHT_START, HIGHLIGHT_STOP
from src.presentation.http.auth import (
    PERM_ADMIN,
    PERM_DOWNLOAD_AUDIO,
    PERM_EXPORT,
    PERM_SEARCH,
    Principal,
)
from src.setting.config import Settings, get_settings
from tests.speech_db_support import speech_test_database_url

_TEST_DATABASE_URL = speech_test_database_url()
#: Данные сеются **рядом с текущим временем**, а не на фиксированную дату.
#: Период по умолчанию — последние 30 дней, и корпус в январе просто не попал бы
#: в него: первый прогон этих тестов так и выглядел — «найдено 0 из 0».
DAY = datetime.now(UTC) - timedelta(hours=1)

#: Ветки классификатора, которыми размечен корпус: имя — (родитель, уровень,
#: название, место, включён).
TREE: dict[str, tuple[str | None, int, str, int, bool]] = {
    "fiz": (None, 1, "Физ.лицо", 1, True),
    "shpd": ("fiz", 2, "ШПД", 1, True),
    "consult": ("shpd", 3, "Консультация", 1, True),
    "broken": ("consult", 4, "Не работает", 1, True),
    "wifi": ("broken", 5, "Wi-Fi", 1, True),
    "mobile": ("fiz", 2, "Мобильная связь", 2, True),
    "mobile_consult": ("mobile", 3, "Консультация", 1, True),
    "tariffs": ("mobile_consult", 4, "Тарифы", 1, True),
    "dash": ("tariffs", 5, "-", 100_000, False),
}
NODES = {name: uuid.uuid4() for name in TREE}
#: Узел классификации ИИ у каждого звонка корпуса.
CALL_AI = {"found": "wifi", "other": "broken", "pending": "tariffs"}


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "local",
        "speech_auth_mode": "open",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _principal(*roles: str) -> Principal:
    return Principal(
        sub="u1",
        username="u1",
        email=None,
        display_name=None,
        roles=tuple(roles),
        auth_mode="keycloak",
    )


# --------------------------------------------------------------------------- #
# Права — без базы
# --------------------------------------------------------------------------- #


def test_a_reader_can_search_and_nothing_else() -> None:
    """Читатель не скачивает аудио и не выгружает списки.

    Каждое из двух — способ вынести персональные данные наружу, и разрешено оно
    роли, которая за это отвечает.
    """
    reader = _principal(SPEECH_ROLE_READER)

    assert reader.can(PERM_SEARCH)
    assert not reader.can(PERM_DOWNLOAD_AUDIO)
    assert not reader.can(PERM_EXPORT)
    assert not reader.can(PERM_ADMIN)


def test_an_editor_can_download_and_export_but_not_administer() -> None:
    editor = _principal(SPEECH_ROLE_READER, SPEECH_ROLE_EDITOR)

    assert editor.can(PERM_DOWNLOAD_AUDIO) and editor.can(PERM_EXPORT)
    assert not editor.can(PERM_ADMIN)


def test_the_senior_role_carries_the_junior_ones() -> None:
    """Иначе человеку выдавали бы три группы, и однажды выдали бы две."""
    granted = roles_from_groups(["whisper-icg-admins"], role_aliases(_settings()))

    assert granted == (SPEECH_ROLE_READER, SPEECH_ROLE_EDITOR, SPEECH_ROLE_ADMIN)
    assert _principal(*granted).can(PERM_SEARCH)


def test_a_stranger_gets_no_roles_at_all() -> None:
    assert roles_from_groups(["some-other-group"], role_aliases(_settings())) == ()


def test_renaming_one_group_does_not_disable_the_other_two() -> None:
    """Переопределение дополняет умолчание, а не замещает его целиком."""
    aliases = role_aliases(_settings(speech_role_aliases_json={"speech_admin": ["ops-admins"]}))

    assert roles_from_groups(["ops-admins"], aliases)[-1] == SPEECH_ROLE_ADMIN
    assert roles_from_groups(["whisper-icg-readers"], aliases) == (SPEECH_ROLE_READER,)


def test_open_mode_is_an_administrator_and_says_so() -> None:
    """Режим виден в `/me`: интерфейс обязан показать, что вход отключён."""
    principal = open_mode_principal()

    assert principal.auth_mode == "open"
    assert principal.can(PERM_ADMIN)


# --------------------------------------------------------------------------- #
# Ручки — на живой базе
# --------------------------------------------------------------------------- #


@pytest.fixture()
async def api() -> AsyncIterator[httpx.AsyncClient]:
    """Клиент поверх ASGI в **том же** цикле событий, что и движок базы.

    `TestClient` синхронный и заводит свой цикл, а асинхронный движок
    привязывается к тому, в котором создан. Смешав их, получаем «attached to a
    different loop» — на первом же запросе и без всякой связи с проверяемым
    кодом.
    """
    if not _TEST_DATABASE_URL:
        pytest.skip("SPEECH_POSTGRES_TEST_URL is required for the endpoint proof")

    settings = _settings(speech_ms_database_url=_TEST_DATABASE_URL)
    engine = create_async_engine(_TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[session_dep] = _override

    await _seed(sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as client:
        yield client
    await _clean(sessions)
    app.dependency_overrides.clear()
    await engine.dispose()


async def _clean(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await session.execute(text("DELETE FROM speech_transcript_segments"))
        await session.execute(text("DELETE FROM speech_transcripts"))
        await session.execute(text("DELETE FROM speech_jobs"))
        await session.execute(text("DELETE FROM speech_calls"))
        await session.execute(text("DELETE FROM speech_users"))
        # Сначала листья: родитель под `RESTRICT` раньше детей не удаляется.
        for level in range(5, 0, -1):
            await session.execute(
                text("DELETE FROM speech_classifier_nodes WHERE level = :level"), {"level": level}
            )


async def _seed(sessions: async_sessionmaker[AsyncSession]) -> None:
    await _clean(sessions)
    async with sessions() as session, session.begin():
        for node, (parent, level, label, order, active) in TREE.items():
            await session.execute(
                text(
                    "INSERT INTO speech_classifier_nodes (id, parent_id, level, label, key, "
                    "source, sort_order, is_active, first_seen_at, created_at, updated_at) "
                    "VALUES (:id, :parent, :level, :label, :key, 'file', :order, :active, "
                    "now(), now(), now())"
                ),
                {
                    "id": NODES[node],
                    "parent": NODES[parent] if parent else None,
                    "level": level,
                    "label": label,
                    "key": label_key(label),
                    "order": order,
                    "active": active,
                },
            )
        # Третий звонок ещё в расшифровке: текста у него нет.
        for index, (name, replies) in enumerate(
            [
                ("found", ["не работает интернет", "проверим оборудование"]),
                ("other", ["хочу сменить тариф"]),
                ("pending", []),
            ]
        ):
            call_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO speech_calls (id, session_id, direction, started_at, ended_at, "
                    "duration_s, audio_status, processing_status, analysis_status, "
                    "sentiment, source_snapshot, first_seen_at, created_at, updated_at, "
                    "ai_node_id, operator_node_id, ai_suggestion) "
                    "VALUES (:id, :sid, 'inbound', :s, :s, 100, 'stored', :ps, 'done', "
                    "'negative', '{}', :s, :s, :s, :ai, :operator, CAST(:suggestion AS jsonb))"
                ),
                {
                    "id": call_id,
                    "sid": f"node_1_domain_1_nauss_1_1_{name}",
                    "s": DAY - timedelta(minutes=index),
                    "ps": "done" if replies else "transcribing",
                    "ai": NODES[CALL_AI[name]],
                    # Оператор классифицировал только первый звонок, и глубже уровня 3 не пошёл.
                    "operator": NODES["consult"] if name == "found" else None,
                    "suggestion": (
                        json.dumps(["Физ.лицо", "ШПД", "Новая тема"]) if name == "found" else None
                    ),
                },
            )
            if not replies:
                continue
            transcript_id = await session.scalar(
                text(
                    "INSERT INTO speech_transcripts (id, call_id, engine, model_name, language, "
                    "role_method, segments_count, created_at) "
                    "VALUES (gen_random_uuid(), :c, 'test', 'large-v3', 'ru', 'stereo_energy', "
                    ":n, now()) RETURNING id"
                ),
                {"c": call_id, "n": len(replies)},
            )
            for seq, reply in enumerate(replies):
                await session.execute(
                    text(
                        "INSERT INTO speech_transcript_segments (transcript_id, call_id, seq, "
                        "speaker, start_ms, end_ms, text_raw) "
                        "VALUES (:t, :c, :seq, 'client', :st, :en, :raw)"
                    ),
                    {
                        "t": transcript_id,
                        "c": call_id,
                        "seq": seq,
                        "st": seq * 1000,
                        "en": seq * 1000 + 900,
                        # Номер в тексте: расшифровка хранится и отдаётся как есть.
                        "raw": f"{reply} 89991234567",
                    },
                )


async def test_me_reports_the_mode_and_the_permissions(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/v1/me")

    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == SPEECH_API_CONTRACT_VERSION
    assert body["auth_mode"] == "open"
    assert PERM_ADMIN in body["permissions"]


async def test_me_remembers_who_came(api: httpx.AsyncClient) -> None:
    """Администратор должен видеть, кто заходил, не открывая Keycloak."""
    await api.get("/api/v1/me")
    response = await api.get("/api/v1/me")

    assert response.status_code == 200


async def test_search_finds_a_call_and_counts_the_period(api: httpx.AsyncClient) -> None:
    response = await api.get(
        "/api/v1/search",
        params={"q": "интернет", "from": (DAY - timedelta(days=1)).isoformat()},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "any_order"
    assert body["total_calls"] == 1
    assert body["period_calls"] == 3
    assert body["share"] == pytest.approx(1 / 3)
    assert body["items"][0]["session_id"].endswith("found")
    assert body["items"][0]["quotes"], "цитата нужна карточке результата"
    assert body["items"][0]["ai_path"] == [
        "Физ.лицо",
        "ШПД",
        "Консультация",
        "Не работает",
        "Wi-Fi",
    ]


async def test_search_reads_the_transcript_as_it_is(api: httpx.AsyncClient) -> None:
    """Обезличивания нет: номер, прозвучавший в разговоре, находится поиском.

    В тексте двух звонков посеян номер. Он в индексе, и по нему находятся оба.
    """
    body = (await api.get("/api/v1/search", params={"q": "89991234567"})).json()

    assert body["total_calls"] == 2
    assert {item["session_id"][-5:] for item in body["items"]} == {"found", "other"}


async def test_an_empty_query_browses_the_period(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/v1/search", params={"q": ""})

    body = response.json()
    assert body["mode"] == "browse"
    assert body["total_calls"] == 3


async def test_stats_counts_as_transcribed_the_calls_that_have_a_transcript(
    api: httpx.AsyncClient,
) -> None:
    """Расшифрован тот звонок, у которого есть текст: с этого момента он находится."""
    response = await api.get("/api/v1/stats")

    assert response.status_code == 200
    body = response.json()
    assert body["processed"] == 3
    assert body["transcribed"] == 2, "у третьего звонка расшифровки ещё нет"
    assert body["queued"] == 1
    # «Основные темы» — ветки узлов уровня 4: звонок с уточнением «Wi-Fi» считается
    # у «Не работает».
    assert [(topic["path"][-1], topic["calls"]) for topic in body["topics"]] == [
        ("Не работает", 2),
        ("Тарифы", 1),
    ]
    assert body["topics"][0]["node_id"] == str(NODES["broken"])
    assert body["topics"][0]["path"] == ["Физ.лицо", "ШПД", "Консультация", "Не работает"]


def strip_marks(text: str) -> str:
    """Реплика без меток подсветки — когда проверяется сам текст, а не разметка."""
    return text.replace(HIGHLIGHT_START, "").replace(HIGHLIGHT_STOP, "")


async def test_a_call_card_carries_the_segments_as_they_are_and_hit_positions(
    api: httpx.AsyncClient,
) -> None:
    found = (await api.get("/api/v1/search", params={"q": "интернет"})).json()["items"][0]

    response = await api.get(f"/api/v1/calls/{found['call_id']}", params={"q": "интернет"})

    assert response.status_code == 200
    body = response.json()
    # Реплики отдаются целиком и в порядке разговора; найденное помечено, но
    # ничего не вырезано — человек пришёл сюда читать разговор, а не выдержку.
    assert [strip_marks(s["text"]) for s in body["segments"]] == [
        "не работает интернет 89991234567",
        "проверим оборудование 89991234567",
    ]
    assert body["hit_seqs"] == [0], "подсветка есть, но реплики не отфильтрованы"
    # SP-310: оба пути от корня и совпадение по уровням; предложение ИИ — словами.
    assert body["ai_path"][-1] == "Wi-Fi"
    assert body["operator_path"] == ["Физ.лицо", "ШПД", "Консультация"]
    assert body["level_match"] == [True, True, True, False, False]
    assert (body["ai_suggestion"], body["ai_suggestion_is_new"]) == (
        ["Физ.лицо", "ШПД", "Новая тема"],
        True,
    )


async def test_a_card_marks_the_words_that_matched(api: httpx.AsyncClient) -> None:
    """Подсветить нужные слова на клиенте нельзя (PLY-04).

    Для этого там пришлось бы завести русскую морфологию — второй поисковый
    движок рядом с настоящим. Поэтому метки ставит Postgres.
    """
    found = (await api.get("/api/v1/search", params={"q": "интернет"})).json()["items"][0]

    body = (await api.get(f"/api/v1/calls/{found['call_id']}", params={"q": "интернет"})).json()

    hit = body["segments"][0]["text"]
    assert HIGHLIGHT_START in hit and HIGHLIGHT_STOP in hit
    marked = hit.split(HIGHLIGHT_START)[1].split(HIGHLIGHT_STOP)[0]
    assert marked.lower().startswith("интернет"), f"подсвечено не то: {marked!r}"
    # Реплика без совпадений остаётся чистой: метки в ней означали бы, что
    # интерфейс подсветит случайное место.
    assert HIGHLIGHT_START not in body["segments"][1]["text"]


async def test_a_card_without_a_query_carries_no_marks(api: httpx.AsyncClient) -> None:
    """Открытый напрямую звонок подсвечивать нечем и незачем."""
    found = (await api.get("/api/v1/search", params={"q": "интернет"})).json()["items"][0]

    body = (await api.get(f"/api/v1/calls/{found['call_id']}")).json()

    assert all(HIGHLIGHT_START not in s["text"] for s in body["segments"])
    assert body["hit_seqs"] == []


def test_the_default_period_is_thirty_days_and_all_time_removes_it() -> None:
    """Три случая разведены явно, и третий появился не сразу.

    Кнопка «Искать за всё время» снимала период на клиенте, запрос уходил без
    границ — и сервер подставлял те же тридцать дней. Кнопка нажималась, ничего
    не менялось, и объяснить это по экрану было нельзя. «Всё время» невыразимо
    отсутствием параметров: отсутствие уже занято умолчанием.
    """
    default_start, default_end = resolve_period(None, None)
    assert default_end is None
    assert default_start is not None
    assert 29 <= (datetime.now(UTC) - default_start).days <= 30

    assert resolve_period(None, None, all_time=True) == (None, None)

    # Заданная граница важнее и умолчания, и флага: иначе фильтр периода нечем
    # было бы задать.
    edge = datetime(2026, 1, 1, tzinfo=UTC)
    assert resolve_period(edge, None) == (edge, None)


@pytest.mark.parametrize("value", ["", "ALL_TIME", "всё", "true", "1", None])
def test_only_the_word_all_removes_the_period(value: str | None) -> None:
    """Закрытый набор из одного слова: опечатка не должна открывать весь архив.

    Отказ в сторону умолчания, а не «показать всё»: запрос по всему архиву —
    это нагрузка на поиск и выдача, о которой никто не просил.
    """
    assert is_all_time(value) is False
    assert is_all_time("all") is True
    assert is_all_time("  ALL  ") is True, "регистр и пробелы — не опечатка"


async def test_a_call_outside_the_default_period_is_found_only_by_all_time(
    api: httpx.AsyncClient,
) -> None:
    """Проверка на живой базе: старый звонок невидим по умолчанию и виден с флагом.

    Ровно этот случай и был у владельца на экране: выгрузка за январь, а
    умолчание — тридцать дней.
    """
    engine = create_async_engine(_TEST_DATABASE_URL or "")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_calls (id, session_id, direction, started_at, ended_at, "
                "duration_s, audio_status, processing_status, analysis_status, "
                "source_snapshot, first_seen_at, created_at, updated_at) "
                "VALUES (gen_random_uuid(), 'node_1_old_call', 'inbound', :s, :s, 60, "
                "'stored', 'done', 'done', '{}', :s, :s, :s)"
            ),
            {"s": datetime.now(UTC) - timedelta(days=200)},
        )

    try:
        default_period = (await api.get("/api/v1/search")).json()
        all_time = (await api.get("/api/v1/search", params={"period": "all"})).json()
        assert all_time["period_calls"] == default_period["period_calls"] + 1
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM speech_calls WHERE session_id = 'node_1_old_call'")
            )
        await engine.dispose()


async def test_an_unknown_call_is_a_clean_404(api: httpx.AsyncClient) -> None:
    response = await api.get(f"/api/v1/calls/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "not_found"


async def test_the_classifier_is_served_flat_with_switched_off_nodes(
    api: httpx.AsyncClient,
) -> None:
    response = await api.get("/api/v1/classifier")

    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["items"]}
    assert len(items) == len(TREE)
    wifi = items[str(NODES["wifi"])]
    assert (wifi["level"], wifi["label"], wifi["parent_id"]) == (5, "Wi-Fi", str(NODES["broken"]))
    assert items[str(NODES["dash"])]["is_active"] is False, "у выключенного узла могут быть звонки"


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ("shpd", {"found", "other"}),
        ("wifi", {"found"}),
        ("mobile", {"pending"}),
        ("fiz", {"found", "other", "pending"}),
    ],
)
async def test_a_classifier_node_narrows_the_search_to_its_branch(
    api: httpx.AsyncClient, node: str, expected: set[str]
) -> None:
    body = (await api.get("/api/v1/search", params={"node_id": str(NODES[node])})).json()

    assert {item["session_id"].rsplit("_", 1)[-1] for item in body["items"]} == expected
    assert body["period_calls"] == len(expected), "доля считается от того же отбора"


async def test_filter_options_list_only_values_that_can_be_found(
    api: httpx.AsyncClient,
) -> None:
    """Список, растущий бывшими сотрудниками, предлагает выбрать пустоту."""
    assert (await api.get("/api/v1/operators")).json()["items"] == []
    assert (await api.get("/api/v1/projects")).json()["items"] == []


async def test_the_dialogs_screen_does_not_mix_ncc_and_telphin(api: httpx.AsyncClient) -> None:
    """S-35 на живой базе: вкладки «Диалоги» — разные списки, счётчики и фильтры.

    Звонок Телфина говорит о том же, что звонок NCC из корпуса («интернет»), и
    у него свои оператор и проект. Без `source` экран видит только NCC — так же,
    как до прихода Телфина; с `source=telphin` — только Телфин. Карточка по
    ссылке открывает звонок любого источника и называет его.
    """
    engine = create_async_engine(_TEST_DATABASE_URL or "")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    call_id, operator_id, project_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_operators (id, login, full_name, removed, created_at, "
                "updated_at) VALUES (:id, '13240*102', 'Иванова Мария Петровна', FALSE, "
                "now(), now())"
            ),
            {"id": operator_id},
        )
        await session.execute(
            text(
                "INSERT INTO speech_ncc_projects (id, source_key, title, direction, "
                "ingest_enabled, created_at, updated_at) VALUES (:id, 'telphin:test', "
                "'Телфин · исходящие ШПД', 'outbound', TRUE, now(), now())"
            ),
            {"id": project_id},
        )
        await session.execute(
            text(
                "INSERT INTO speech_calls (id, session_id, source, direction, started_at, "
                "ended_at, duration_s, audio_status, processing_status, analysis_status, "
                "source_snapshot, first_seen_at, created_at, updated_at, operator_id, "
                "project_id, ai_node_id) "
                "VALUES (:id, 'telphin_test_call', 'telphin', 'outbound', :s, :s, 90, "
                "'stored', 'done', 'done', '{}', :s, :s, :s, :operator, :project, :ai)"
            ),
            {
                "id": call_id,
                "s": DAY - timedelta(minutes=10),
                "operator": operator_id,
                "project": project_id,
                "ai": NODES["tariffs"],
            },
        )
        transcript_id = await session.scalar(
            text(
                "INSERT INTO speech_transcripts (id, call_id, engine, model_name, language, "
                "role_method, segments_count, created_at) "
                "VALUES (gen_random_uuid(), :c, 'test', 'large-v3', 'ru', 'stereo_energy', "
                "1, now()) RETURNING id"
            ),
            {"c": call_id},
        )
        await session.execute(
            text(
                "INSERT INTO speech_transcript_segments (transcript_id, call_id, seq, speaker, "
                "start_ms, end_ms, text_raw) "
                "VALUES (:t, :c, 0, 'operator', 0, 900, :text)"
            ),
            {"t": transcript_id, "c": call_id, "text": "подключим домашний интернет"},
        )

    try:
        ncc = (await api.get("/api/v1/search", params={"q": "интернет"})).json()
        telphin = (
            await api.get("/api/v1/search", params={"q": "интернет", "source": "telphin"})
        ).json()
        assert [item["session_id"][-5:] for item in ncc["items"]] == ["found"]
        assert (ncc["total_calls"], ncc["period_calls"]) == (1, 3), "как до прихода Телфина"
        assert [item["session_id"] for item in telphin["items"]] == ["telphin_test_call"]
        assert telphin["items"][0]["source"] == "telphin"
        assert (telphin["total_calls"], telphin["period_calls"]) == (1, 1)

        ncc_stats = (await api.get("/api/v1/stats")).json()
        telphin_stats = (await api.get("/api/v1/stats", params={"source": "telphin"})).json()
        assert ncc_stats["processed"] == 3
        assert [(t["path"][-1], t["calls"]) for t in ncc_stats["topics"]] == [
            ("Не работает", 2),
            ("Тарифы", 1),
        ]
        assert (telphin_stats["processed"], telphin_stats["transcribed"]) == (1, 1)
        assert [(t["path"][-1], t["calls"]) for t in telphin_stats["topics"]] == [("Тарифы", 1)]

        card = (await api.get(f"/api/v1/calls/{call_id}")).json()
        assert (card["source"], card["direction"]) == ("telphin", "outbound")
        found = ncc["items"][0]
        assert found["source"] == "ncc"
        assert (await api.get(f"/api/v1/calls/{found['call_id']}")).json()["source"] == "ncc"

        for path, label in (
            ("/api/v1/operators", "Иванова Мария Петровна"),
            ("/api/v1/projects", "Телфин · исходящие ШПД"),
        ):
            assert (await api.get(path)).json()["items"] == []
            options = (await api.get(path, params={"source": "telphin"})).json()["items"]
            assert [option["label"] for option in options] == [label]
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM speech_transcript_segments WHERE call_id = :c"), {"c": call_id}
            )
            await session.execute(
                text("DELETE FROM speech_transcripts WHERE call_id = :c"), {"c": call_id}
            )
            await session.execute(text("DELETE FROM speech_calls WHERE id = :c"), {"c": call_id})
            await session.execute(
                text("DELETE FROM speech_operators WHERE id = :o"), {"o": operator_id}
            )
            await session.execute(
                text("DELETE FROM speech_ncc_projects WHERE id = :p"), {"p": project_id}
            )
        await engine.dispose()
