"""Аудио и выгрузка.

Два действия, каждое из которых выносит наружу больше, чем показывает поиск.
Поэтому проверяется не только «работает», но и три вещи, которые обязаны
случиться вместе с ним: право проверено, событие записано, лишнего не отдано.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.di import requests as request_di
from src.di.http import create_app
from src.di.requests import install_dependencies, session_dep
from src.domain.calls.contracts import SPEECH_AUDIT_EVENT_TYPES
from src.infrastructure.persistence.http_audit import UnknownAuditEvent, normalize_payload, record
from src.infrastructure.persistence.search import HIGHLIGHT_START, HIGHLIGHT_STOP
from src.infrastructure.storage.recordings import StorageError
from src.presentation.http import media_routes as media_routes
from src.presentation.http.auth import Principal, current_principal
from src.presentation.http.media_routes import CSV_BOM, CSV_DELIMITER
from src.setting.config import Settings, get_settings
from tests.speech_db_support import speech_test_database_url

_TEST_DATABASE_URL = speech_test_database_url()
NOW = datetime.now(UTC) - timedelta(hours=1)
RAW_NUMBER = "89991234567"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "local",
        "speech_auth_mode": "open",
        "speech_storage_base_url": "http://minio.invalid:9000",
        "speech_storage_bucket": "speech-recordings",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _principal(*roles: str) -> Principal:
    return Principal(
        sub="u1",
        username="u1",
        email=None,
        display_name="Тест",
        roles=tuple(roles),
        auth_mode="keycloak",
    )


# --------------------------------------------------------------------------- #
# Журнал — без базы
# --------------------------------------------------------------------------- #


def test_an_event_outside_the_closed_set_is_refused() -> None:
    """Событие с выдуманным типом не найдётся ни одним фильтром.

    То есть его как будто и нет — а журнал существует ровно для того, чтобы
    администратор мог найти, кто и что смотрел (AUT-03).
    """

    class _Session:
        async def execute(self, *_a: Any, **_k: Any) -> None:  # pragma: no cover
            raise AssertionError("не должно дойти до записи")

    with pytest.raises(UnknownAuditEvent):
        import asyncio

        asyncio.run(record(_Session(), _principal("speech_admin"), "что_то_своё"))  # type: ignore[arg-type]


def test_every_declared_event_type_is_accepted() -> None:
    # Раскрытия маски больше нет, и записать такое событие нечем.
    assert "pii.reveal" not in SPEECH_AUDIT_EVENT_TYPES
    assert "audio.download" in SPEECH_AUDIT_EVENT_TYPES
    assert "export.list" in SPEECH_AUDIT_EVENT_TYPES


def test_the_payload_never_carries_the_thing_it_guards() -> None:
    """Журнал, хранящий раскрытый текст, — вторая копия той же утечки.

    Списки и словари сворачиваются в размер: именно в них оказываются реплики,
    найденные сущности и сам разговор.
    """
    trimmed = normalize_payload(
        {
            "segments": [{"text_raw": f"мой номер {RAW_NUMBER}"}],
            "found": {"phone": RAW_NUMBER},
            "session_id": "node_1",
            "count": 3,
        }
    )

    assert trimmed["segments"] == {"count": 1}
    assert trimmed["found"] == {"keys": 1}
    assert trimmed["count"] == 3
    assert RAW_NUMBER not in str(trimmed)


def test_a_long_value_is_cut() -> None:
    trimmed = normalize_payload({"note": "а" * 5000})

    assert len(str(trimmed["note"])) <= 200


def test_too_many_keys_are_cut() -> None:
    trimmed = normalize_payload({f"k{i}": i for i in range(100)})

    assert len(trimmed) <= 20


# --------------------------------------------------------------------------- #
# Ручки — на живой базе
# --------------------------------------------------------------------------- #


@pytest.fixture()
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, Any, uuid.UUID]]:
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

    call_id = await _seed(sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as client:
        yield client, app, call_id
    await _clean(sessions)
    app.dependency_overrides.clear()
    await engine.dispose()


async def _clean(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await session.execute(text("DELETE FROM speech_audit_events"))
        await session.execute(text("DELETE FROM speech_transcript_segments"))
        await session.execute(text("DELETE FROM speech_transcripts"))
        await session.execute(text("DELETE FROM speech_jobs"))
        await session.execute(text("DELETE FROM speech_calls"))


async def _seed(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    await _clean(sessions)
    call_id = uuid.uuid4()
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_calls (id, session_id, direction, started_at, ended_at, "
                "duration_s, audio_status, audio_object_key, processing_status, "
                "analysis_status, source_snapshot, first_seen_at, created_at, updated_at) "
                "VALUES (:id, :sid, 'inbound', :s, :s, 100, 'stored', :key, 'done', 'done', "
                "'{}', :s, :s, :s)"
            ),
            {
                "id": call_id,
                "sid": "node_1_domain_1_nauss_1_1_media",
                "s": NOW,
                "key": "calls/2026/01/14/media.mp3",
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
                "VALUES (:t, :c, 0, 'client', 0, 900, :raw)"
            ),
            {"t": transcript_id, "c": call_id, "raw": f"мой номер {RAW_NUMBER}"},
        )
    return call_id


async def _audit(api_tuple: tuple[httpx.AsyncClient, Any, uuid.UUID]) -> list[dict[str, Any]]:
    _client, app, _call = api_tuple
    override = app.dependency_overrides[session_dep]
    async for session in override():
        rows = [
            dict(row)
            for row in (
                await session.execute(
                    text(
                        "SELECT event_type, actor_sub, actor_roles, call_id, payload "
                        "FROM speech_audit_events ORDER BY occurred_at"
                    )
                )
            ).mappings()
        ]
        return rows
    return []


async def test_the_reveal_route_is_gone(api) -> None:
    """Раскрывать нечего: текст в карточке и так без масок, ручки нет."""
    client, _app, call_id = api

    response = await client.post(f"/api/v1/calls/{call_id}/reveal")

    assert response.status_code == 404
    assert await _audit(api) == []


async def test_the_export_is_the_same_search_the_user_sees(api) -> None:
    """Выгрузка, собранная своим SQL, однажды разойдётся с экраном."""
    client, _app, _call = api

    response = await client.get("/api/v1/export", params={"q": "номер"})

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    body = response.text
    assert body.startswith(CSV_BOM), "BOM нужен Excel"
    header = body.splitlines()[0]
    assert CSV_DELIMITER in header
    assert "session_id" in header and "segment" in header and "product" in header


async def test_the_export_carries_the_same_quote_as_the_screen(api) -> None:
    """У выгрузки своё право, но не свои данные: цитата — та же, что в выдаче."""
    client, _app, _call = api
    found = (await client.get("/api/v1/search", params={"q": "номер"})).json()["items"][0]
    quote = found["quotes"][0]["text"].replace(HIGHLIGHT_START, "").replace(HIGHLIGHT_STOP, "")

    response = await client.get("/api/v1/export", params={"q": "номер"})

    assert quote and quote in response.text.replace(HIGHLIGHT_START, "").replace(HIGHLIGHT_STOP, "")


async def test_the_export_is_audited(api) -> None:
    client, _app, _call = api
    await client.get("/api/v1/export", params={"q": "номер"})

    events = await _audit(api)

    assert [e["event_type"] for e in events] == ["export.list"]
    assert events[0]["payload"]["rows"] == {"count": 1}, "число строк, а не сами строки"


async def test_a_reader_cannot_export(api) -> None:
    client, app, _call = api
    app.dependency_overrides[current_principal] = lambda: _principal("speech_reader")

    response = await client.get("/api/v1/export")

    assert response.status_code == 403
    app.dependency_overrides.pop(current_principal)


async def test_audio_of_a_call_without_a_recording_says_why(api) -> None:
    """«Файл не найден» там, где запись удалена по сроку, — это выдача
    соблюдения политики за поломку."""
    client, app, call_id = api
    override = app.dependency_overrides[session_dep]
    async for session in override():
        await session.execute(
            text(
                "UPDATE speech_calls SET audio_status = 'deleted_by_retention', "
                "audio_object_key = NULL WHERE id = :id"
            ),
            {"id": call_id},
        )
        await session.commit()
        break

    response = await client.get(f"/api/v1/calls/{call_id}/audio")

    assert response.status_code == 404
    assert response.json()["detail"] == "audio_deleted_by_retention"


async def test_a_reader_cannot_download(api) -> None:
    """Прослушивание разрешено читателю, явная загрузка — только редактору."""
    client, app, call_id = api
    app.dependency_overrides[current_principal] = lambda: _principal("speech_reader")

    response = await client.get(f"/api/v1/calls/{call_id}/audio", params={"download": 1})

    assert response.status_code == 403
    app.dependency_overrides.pop(current_principal)


class _AudioStorage:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str | None]] = []
        self.closed = 0

    @asynccontextmanager
    async def open(
        self, key: str, *, byte_range: str | None = None
    ) -> AsyncIterator[httpx.Response]:
        self.opened.append((key, byte_range))
        headers = {"Content-Type": "audio/mpeg"}
        if byte_range:
            headers["Content-Range"] = "bytes 1-3/5"
        response = httpx.Response(
            206 if byte_range else 200, headers=headers, content=b"123" if byte_range else b"01234"
        )
        try:
            yield response
        finally:
            await response.aclose()
            self.closed += 1


@pytest.fixture()
async def offline_audio_api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    """Real route and guards, no database engine or storage network calls."""
    session = AsyncMock(spec=AsyncSession)
    result = Mock()
    result.mappings.return_value.first.return_value = {
        "session_id": "test-call",
        "audio_status": "stored",
        "audio_object_key": "calls/test.mp3",
    }
    session.execute.return_value = result
    storage = _AudioStorage()
    factory = Mock(return_value=storage)
    audit = AsyncMock()
    monkeypatch.setattr(request_di, "build_storage", factory)
    monkeypatch.setattr("src.infrastructure.storage.audio_access.record", audit)
    app = FastAPI()
    install_dependencies(app)
    app.include_router(media_routes.router)
    settings = _settings(speech_auth_mode="keycloak")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[session_dep] = lambda: session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://speech"
    ) as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            session=session,
            storage=storage,
            factory=factory,
            audit=audit,
            call_id=uuid.uuid4(),
        )


@pytest.mark.parametrize("role", ["speech_reader", "speech_editor", "speech_admin", "unknown"])
@pytest.mark.parametrize("download", [None, 0, 1])
@pytest.mark.parametrize("byte_range", [None, "bytes=1-3"])
async def test_audio_playback_and_download_have_distinct_guards_and_audit(
    offline_audio_api,
    role,
    download,
    byte_range,
) -> None:
    api = offline_audio_api
    principal = _principal(role)
    api.app.dependency_overrides[current_principal] = lambda: principal
    response = await api.client.get(
        f"/api/v1/calls/{api.call_id}/audio",
        params={} if download is None else {"download": download},
        headers={} if byte_range is None else {"Range": byte_range},
    )
    denied = role == "unknown" or (role == "speech_reader" and download == 1)
    if denied:
        assert response.status_code == 403
        assert response.json()["detail"] == "forbidden"
        api.session.execute.assert_not_awaited()
        api.factory.assert_not_called()
        assert api.storage.opened == []
        api.audit.assert_not_awaited()
        api.session.commit.assert_not_awaited()
        return

    assert response.status_code == (206 if byte_range else 200)
    assert response.content == (b"123" if byte_range else b"01234")
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["content-length"] == ("3" if byte_range else "5")
    if byte_range:
        assert response.headers["content-range"] == "bytes 1-3/5"
    assert api.storage.opened == [("calls/test.mp3", byte_range)]
    assert api.storage.closed == 1
    api.factory.assert_called_once()
    if download == 1:
        assert response.headers["content-disposition"] == 'attachment; filename="test-call.mp3"'
        api.audit.assert_awaited_once_with(
            api.session,
            principal,
            "audio.download",
            call_id=api.call_id,
            payload={"session_id": "test-call"},
        )
        api.session.commit.assert_awaited_once()
    else:
        assert "content-disposition" not in response.headers
        api.audit.assert_not_awaited()
        api.session.commit.assert_not_awaited()


@pytest.mark.parametrize("download", [0, 1])
async def test_audio_still_requires_authentication(offline_audio_api, download: int) -> None:
    api = offline_audio_api
    response = await api.client.get(
        f"/api/v1/calls/{api.call_id}/audio", params={"download": download}
    )
    assert response.status_code == 401
    api.session.execute.assert_not_awaited()
    api.factory.assert_not_called()
    api.audit.assert_not_awaited()


class _FailingStorage:
    """Хранилище, отказывающее заданным классом причины."""

    def __init__(self, reason: str, status_code: int | None = None) -> None:
        self.reason = reason
        self.status_code = status_code

    def open(self, *_a: Any, **_k: Any) -> Any:
        raise StorageError("нет", reason=self.reason, status_code=self.status_code)


@pytest.mark.parametrize(
    ("reason", "expected_status", "expected_detail"),
    [
        ("not_found", 404, "audio_unavailable"),
        ("timeout", 502, "storage_timeout"),
        ("provider_error", 502, "storage_provider_error"),
    ],
)
async def test_a_storage_failure_is_reported_by_its_class(
    api, monkeypatch, reason: str, expected_status: int, expected_detail: str
) -> None:
    """`404` на всё подряд отправляет человека искать удалённую запись там, где
    на самом деле лежит хранилище или отозван доступ."""
    client, _app, call_id = api
    monkeypatch.setattr(
        "src.di.requests.build_storage",
        lambda _s: _FailingStorage(reason),
    )

    response = await client.get(f"/api/v1/calls/{call_id}/audio")

    assert response.status_code == expected_status
    assert response.json()["detail"] == expected_detail
