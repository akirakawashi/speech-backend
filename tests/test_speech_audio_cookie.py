"""Кука для тега `<audio>` (SP-808).

Проверяется не «работает ли она», а то, ради чего её вообще можно было
завести: что она **не расширяет доступ**. Подписанная ссылка была отвергнута
именно потому, что действует сама по себе; кука, дающая больше заголовка, была
бы той же ошибкой под другим именем.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from src.di.http import create_app
from src.presentation.http.audio_cookie import (
    AUDIO_COOKIE_MAX_AGE,
    AUDIO_COOKIE_NAME,
    AUDIO_COOKIE_PATH,
    AudioCookieMiddleware,
    accepts_audio_cookie,
)
from src.presentation.http.auth import Principal, current_principal
from src.setting.config import Settings, get_settings

CALL = uuid.uuid4()


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "local",
        "speech_auth_mode": "open",
        "speech_storage_base_url": "http://minio.invalid:9000",
        "speech_storage_bucket": "speech-recordings",
        # Адрес, по которому никто не отвечает: к базе эти тесты не ходят, но
        # сессию на запрос сервис собирает всегда, а без адреса собирать её
        # отказывается. В монорепозитории пустое значение молча откатывалось на
        # базу основного приложения — здесь отката нет.
        "speech_ms_database_url": "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nowhere",
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


@pytest.fixture()
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    settings = _settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        yield http, app
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Где кука принимается, а где нет
# --------------------------------------------------------------------------- #


def test_the_cookie_is_accepted_on_exactly_one_path() -> None:
    """Не префикс `/api/v1/calls`: под ним живут карточка звонка и её оценка,
    и принимать там куку значило бы отдать по ней текст разговора."""
    assert accepts_audio_cookie("GET", f"/api/v1/calls/{CALL}/audio")

    assert not accepts_audio_cookie("GET", f"/api/v1/calls/{CALL}")
    assert not accepts_audio_cookie("GET", f"/api/v1/calls/{CALL}/quality")
    assert not accepts_audio_cookie("GET", "/api/v1/search")
    assert not accepts_audio_cookie("GET", "/api/v1/export")
    assert not accepts_audio_cookie("GET", "/api/v1/admin/audit")


def test_a_path_that_only_looks_like_the_audio_one_is_refused() -> None:
    """Идентификатор — uuid, а не «что угодно до /audio»."""
    assert not accepts_audio_cookie("GET", "/api/v1/calls/../audio")
    assert not accepts_audio_cookie("GET", "/api/v1/calls/1/audio")
    assert not accepts_audio_cookie("GET", f"/api/v1/calls/{CALL}/audio/extra")


def test_only_get_is_covered() -> None:
    """Метод, меняющий состояние, не должен опираться на куку: `SameSite` — не
    единственная линия, на которую тут стоит полагаться."""
    assert not accepts_audio_cookie("POST", f"/api/v1/calls/{CALL}/audio")


# --------------------------------------------------------------------------- #
# Слой: заголовок сильнее куки
# --------------------------------------------------------------------------- #


def _probe_app() -> tuple[Any, list[str | None]]:
    """Голое приложение: слой куки и одна ручка, помнящая дошедший заголовок.

    Отдельное, а не боевое: `add_api_route` поверх готового приложения не
    перекрывает уже объявленный маршрут — совпадает первый, и проверка молча
    измеряла бы не то.

    `FastAPI` и `Request` импортированы **на уровне модуля**. В файле включён
    `from __future__ import annotations`, и локальный импорт делал бы аннотацию
    `request: Request` неразрешимой: FastAPI считал бы `request` обычным
    параметром запроса и отвечал `422`. Ручка при этом не выполнялась вовсе, а
    проверка видела пустой список и падала на непонятном месте.
    """
    seen: list[str | None] = []
    app = FastAPI()
    app.add_middleware(AudioCookieMiddleware)

    @app.get("/api/v1/calls/{call_id}/audio")
    async def _probe(call_id: str, request: Request) -> dict[str, str]:
        seen.append(request.headers.get("authorization"))
        return {"call_id": call_id}

    @app.get("/api/v1/calls/{call_id}/quality")
    async def _quality(call_id: str, request: Request) -> dict[str, str]:
        seen.append(request.headers.get("authorization"))
        return {"call_id": call_id}

    return app, seen


async def _get(
    app: Any,
    path: str,
    *,
    cookie: str | None = None,
    header: str | None = None,
    method: str = "GET",
) -> None:
    transport = httpx.ASGITransport(app=app)
    cookies = {AUDIO_COOKIE_NAME: cookie} if cookie else None
    async with httpx.AsyncClient(
        transport=transport, base_url="http://speech", cookies=cookies
    ) as http:
        response = await http.request(
            method, path, headers={"Authorization": header} if header else None
        )
    assert response.status_code == 200, response.text


async def test_the_cookie_becomes_a_bearer_when_no_header_came() -> None:
    app, seen = _probe_app()

    await _get(app, f"/api/v1/calls/{CALL}/audio", cookie="from-cookie")

    # Тот же самый разбор токена и та же проверка роли: кука — способ доставки,
    # а не второй путь в обход авторизации.
    assert seen == ["Bearer from-cookie"]


async def test_the_header_wins_over_the_cookie() -> None:
    """Обратный порядок означал бы, что просроченная кука отменяет свежий
    токен, и разлогин выглядел бы как случайный."""
    app, seen = _probe_app()

    await _get(
        app, f"/api/v1/calls/{CALL}/audio", cookie="from-cookie", header="Bearer from-header"
    )

    assert seen == ["Bearer from-header"]


async def test_no_cookie_means_no_header_is_invented() -> None:
    app, seen = _probe_app()

    await _get(app, f"/api/v1/calls/{CALL}/audio")

    assert seen == [None], "пустая кука не превращается в пустой Bearer"


async def test_the_cookie_does_not_reach_other_routes() -> None:
    """Даже утекшая, кука не должна открывать ничего, кроме записи."""
    app, seen = _probe_app()

    await _get(app, f"/api/v1/calls/{CALL}/quality", cookie="from-cookie")

    assert seen == [None], "на оценку звонка слой не дописал ничего"


# --------------------------------------------------------------------------- #
# Выдача и отзыв
# --------------------------------------------------------------------------- #


async def test_the_cookie_is_scoped_and_hardened(client) -> None:
    http, app = client
    app.dependency_overrides[current_principal] = lambda: _principal("speech_reader")

    response = await http.post("/api/v1/session/audio", headers={"Authorization": "Bearer TOKEN"})

    assert response.status_code == 200
    assert response.json()["issued"] is True
    assert response.json()["expires_in"] == AUDIO_COOKIE_MAX_AGE
    raw = response.headers["set-cookie"]
    assert f"{AUDIO_COOKIE_NAME}=TOKEN" in raw
    assert f"Path={AUDIO_COOKIE_PATH}" in raw
    assert "HttpOnly" in raw, "скрипт на странице не должен её читать"
    assert "samesite=strict" in raw.lower(), "браузер не шлёт её с чужого сайта"
    assert f"Max-Age={AUDIO_COOKIE_MAX_AGE}" in raw
    app.dependency_overrides.pop(current_principal)


async def test_in_local_it_is_not_secure_over_plain_http(client) -> None:
    """Иначе браузер молча выбросил бы её на `http://127.0.0.1:3000`, и
    разработка сломалась бы без единого сообщения.

    Послабление осталось только для `APP_ENV=local` — фикстура `client`
    собрана именно на нём.
    """
    http, app = client
    app.dependency_overrides[current_principal] = lambda: _principal("speech_reader")

    response = await http.post("/api/v1/session/audio", headers={"Authorization": "Bearer TOKEN"})

    assert "Secure" not in response.headers["set-cookie"]
    app.dependency_overrides.pop(current_principal)


async def test_outside_local_it_is_secure_even_over_plain_http() -> None:
    """Отдельный баг той же находки: здесь `X-Forwarded-Proto` не читался вовсе.

    `secure=request.url.scheme == "https"` за обратным прокси не срабатывает
    никогда — до сервиса запрос всегда доходит по `http`. То есть эта кука не
    получала `Secure` даже в том случае, когда цепочка прокси настроена
    правильно, и починка nginx её бы не спасла.
    """
    settings = _settings(app_env="alpha")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[current_principal] = lambda: _principal("speech_reader")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        response = await http.post(
            "/api/v1/session/audio",
            headers={"Authorization": "Bearer TOKEN", "X-Forwarded-Proto": "http"},
        )

    assert "Secure" in response.headers["set-cookie"]
    app.dependency_overrides.clear()


async def test_over_https_it_is_secure(client) -> None:
    http, app = client
    app.dependency_overrides[current_principal] = lambda: _principal("speech_reader")

    response = await http.post(
        "https://speech/api/v1/session/audio", headers={"Authorization": "Bearer TOKEN"}
    )

    assert "Secure" in response.headers["set-cookie"]
    app.dependency_overrides.pop(current_principal)


async def test_the_ticket_needs_the_same_permission_as_listening(client) -> None:
    """Кука, выдаваемая с меньшим правом, чем требует прослушивание, была бы
    способом получить доступ, которого у человека нет."""
    http, app = client
    app.dependency_overrides[current_principal] = lambda: _principal("nobody")

    response = await http.post("/api/v1/session/audio", headers={"Authorization": "Bearer TOKEN"})

    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    app.dependency_overrides.pop(current_principal)


async def test_open_mode_issues_nothing(client) -> None:
    """Проверять на отдаче нечего — значит и доставлять нечего."""
    http, _app = client

    response = await http.post("/api/v1/session/audio")

    assert response.status_code == 200
    assert response.json()["issued"] is False
    assert "set-cookie" not in response.headers


async def test_logout_clears_it_without_a_live_token(client) -> None:
    """Требовать для выхода действующий токен значит не дать выйти тому, у кого
    он протух, — то есть именно тому, кому это нужнее всего."""
    http, _app = client

    response = await http.delete("/api/v1/session/audio")

    assert response.status_code == 200
    raw = response.headers["set-cookie"].lower()
    assert AUDIO_COOKIE_NAME in raw
    assert "max-age=0" in raw or "expires=thu, 01 jan 1970" in raw


async def test_the_cookie_actually_leaves_the_jar() -> None:
    """Гашение проверяется по СОДЕРЖИМОМУ банки, а не по виду заголовка.

    Повод сомневаться именно у этой куки самый сильный из пяти: выдаётся она с
    `SameSite=strict` и (вне `local`) с `Secure`, а гасится без обоих. По RFC
    6265bis кука опознаётся тройкой имя+домен+путь, и ни `Secure`, ни
    `SameSite` в неё не входят, так что гашение обязано доходить, — но
    «обязано» и «доходит» это разные утверждения, и проверяется второе.

    Окружение `alpha`, а не фикстура `client`: в `local` кука выдаётся без
    `Secure`, то есть расхождения, ради которого тест написан, там нет.
    """
    settings = _settings(app_env="alpha")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[current_principal] = lambda: _principal("speech_reader")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        bearer = {"Authorization": "Bearer TOKEN"}
        issued = await http.post("/api/v1/session/audio", headers=bearer)
        assert "Secure" in issued.headers["set-cookie"]
        assert AUDIO_COOKIE_NAME in http.cookies, "иначе гасить нечего и тест пуст"

        await http.delete("/api/v1/session/audio")

        assert AUDIO_COOKIE_NAME not in http.cookies
    app.dependency_overrides.clear()
